"""Tests for evalops.services.collector: orchestration, incrementality, access statuses, cursor.

Uses fakes for Genie (backed by the real fixtures) and for the Delta writer, plus the real
InMemoryStore, so the whole collect flow runs without any network.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evalops.clients.lakebase import InMemoryStore
from evalops.config import Settings
from evalops.errors import UpstreamError
from evalops.schemas.genie import Conversation, GenieSpace, ItemsPage, QueryResultResponse, SpaceConfig
from evalops.services.collector import collect_space, monitored_spaces, record_space_version
from evalops.services.delta_store import MergeCounts

FIXTURES = Path(__file__).parent / "fixtures" / "genie"
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
SPACE = "space-1"


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


class FakeGenie:
    """Serves fixture conversations; `errors` maps conversation id -> HTTP status to fail with."""

    def __init__(self, conversations: dict[str, tuple[str, datetime]], errors: dict[str, int] | None = None):
        self.conversations = conversations  # id -> (fixture name, created)
        self.errors = errors or {}
        self.items_calls: list[str] = []
        self.result_calls = 0

    async def list_spaces(self):
        return [GenieSpace(space_id=s) for s in ("space-1", "space-2", "space-3")]

    async def can_manage(self, space_id):
        return space_id != "space-2"

    async def list_conversations(self, space_id):
        for cid, (name, created) in self.conversations.items():
            agent_type = "GENIE_CONVERSATION_TYPE_CHAT" if name == "chat" else "GENIE_CONVERSATION_TYPE_AGENT"
            yield Conversation(conversation_id=cid, created_timestamp=ms(created), agent_type=agent_type)

    async def get_items(self, space_id, conversation_id):
        self.items_calls.append(conversation_id)
        if conversation_id in self.errors:
            raise UpstreamError("genie", "denied", status=self.errors[conversation_id])
        name = self.conversations[conversation_id][0]
        return ItemsPage.model_validate(json.loads((FIXTURES / f"items_{name}.json").read_text())).data

    async def get_query_result(self, space_id, conversation_id, message_id, call_id):
        self.result_calls += 1
        return QueryResultResponse.model_validate(
            json.loads((FIXTURES / "query_result_single_sql.json").read_text())
        )


class FakeWriter:
    """Records what would be written; MERGE semantics simulated with dicts keyed like Delta."""

    def __init__(self):
        self.traces: dict[str, object] = {}
        self.steps: dict[tuple[str, int], object] = {}
        self.snapshots: list = []

    async def write_snapshots(self, snapshots):
        self.snapshots.extend(snapshots)

    async def merge_traces(self, traces):
        counts = MergeCounts()
        for t in traces:
            if t.trace_id in self.traces:
                counts.updated += 1
            else:
                counts.inserted += 1
            self.traces[t.trace_id] = t
        return counts

    async def merge_steps(self, steps, space_id):
        for s in steps:
            self.steps[(s.trace_id, s.step_index)] = s
        return MergeCounts()


def settings(**overrides) -> Settings:
    return Settings(lookback_days=7, genie_concurrency_per_space=2, **overrides)


async def collect(genie, writer, store, now=NOW):
    return await collect_space(
        SPACE, genie=genie, writer=writer, store=store, settings=settings(), run_id="run", now=now
    )


async def test_first_run_collects_every_turn_and_its_step_results():
    genie = FakeGenie(
        {
            "c1": ("single_sql", NOW - timedelta(days=3)),
            "c2": ("multi_step_viz", NOW - timedelta(days=2)),
            "c3": ("multi_turn", NOW - timedelta(days=1)),
        }
    )
    writer, store = FakeWriter(), InMemoryStore()

    stats = await collect(genie, writer, store)

    assert stats.n_conversations == 3
    assert stats.n_traces == stats.n_traces_new == 1 + 1 + 3
    assert stats.by_status == {"ok": 5}
    sql_steps = [s for s in writer.steps.values() if s.tool_name == "execute_sql"]
    assert genie.result_calls == len(sql_steps)  # one result fetch per SQL step, none for charts
    assert all(s.result_rows == [[9678]] for s in sql_steps)  # typed by the manifest (fixture result)
    assert await store.get_cursor(SPACE) == (NOW - timedelta(days=1)).replace(microsecond=0)


async def test_an_expired_query_result_leaves_the_rows_empty_instead_of_failing_the_run():
    """Regression: Genie answers `{}` for a day-old turn's result, and the collector crashed on
    it. The step keeps no rows here; `merge_steps` keeps the rows stored on an earlier run."""

    class ExpiredGenie(FakeGenie):
        async def get_query_result(self, *args):
            self.result_calls += 1
            return QueryResultResponse.model_validate({})

    genie = ExpiredGenie({"c1": ("single_sql", NOW - timedelta(days=1))})
    writer, store = FakeWriter(), InMemoryStore()

    stats = await collect(genie, writer, store)

    assert stats.by_status == {"ok": 1}
    [step] = writer.steps.values()
    assert step.result_rows is None and step.error is None
    assert not any(s.endpoint.startswith("query-result") for s in writer.snapshots)


async def test_rerun_is_idempotent():
    genie = FakeGenie({"c1": ("single_sql", NOW - timedelta(days=1))})
    writer, store = FakeWriter(), InMemoryStore()

    await collect(genie, writer, store)
    second = await collect(genie, writer, store)

    assert second.n_traces_new == 0 and second.n_traces_updated == 1
    assert len(writer.traces) == 1


async def test_the_lookback_window_rereads_recent_conversations_and_skips_old_ones():
    """Cursor at T: conversations created after T-7d are re-read (they may have new turns)."""
    store = InMemoryStore()
    await store.set_cursor(SPACE, NOW, run_id="previous")
    genie = FakeGenie(
        {
            "old": ("single_sql", NOW - timedelta(days=30)),
            "recent": ("single_sql", NOW - timedelta(days=2)),
        }
    )

    stats = await collect(genie, FakeWriter(), store)

    assert genie.items_calls == ["recent"]
    assert stats.n_conversations == 1
    assert await store.get_cursor(SPACE) == NOW  # never moves backwards


async def test_unreadable_and_chat_conversations_become_placeholder_traces():
    genie = FakeGenie(
        {
            "mine": ("single_sql", NOW),
            "theirs": ("single_sql", NOW),
            "ancient": ("single_sql", NOW),
            "broken": ("single_sql", NOW),
            "chatty": ("chat", NOW),
        },
        errors={"theirs": 403, "ancient": 404, "broken": 500},
    )
    writer = FakeWriter()

    stats = await collect(genie, writer, InMemoryStore())

    assert stats.by_status == {"ok": 1, "owner_only": 1, "pre_agent_mode": 1, "error": 1, "chat_mode": 1}
    assert stats.n_errors == 1  # only the unexpected failure counts as an error
    assert "chatty" not in genie.items_calls  # chat conversations are counted, not fetched


async def test_an_empty_space_does_not_touch_the_cursor():
    store = InMemoryStore()

    stats = await collect(FakeGenie({}), FakeWriter(), store)

    assert stats.n_conversations == 0
    assert await store.get_cursor(SPACE) is None


async def test_monitored_spaces_keeps_managed_ones_minus_exclusions():
    genie = FakeGenie({})

    assert await monitored_spaces(genie, settings()) == ["space-1", "space-3"]
    assert await monitored_spaces(genie, settings(exclude_spaces=["space-3"])) == ["space-1"]


async def test_conversations_are_fetched_concurrently_but_never_above_the_cap():
    """Found by deleting `async with slots:` — no test turned red until this one existed."""
    import asyncio

    class SlowGenie(FakeGenie):
        in_flight = peak = 0

        async def get_items(self, space_id, conversation_id):
            SlowGenie.in_flight += 1
            SlowGenie.peak = max(SlowGenie.peak, SlowGenie.in_flight)
            await asyncio.sleep(0.02)
            SlowGenie.in_flight -= 1
            return await super().get_items(space_id, conversation_id)

    genie = SlowGenie({f"c{i}": ("single_sql", NOW) for i in range(6)})

    await collect(genie, FakeWriter(), InMemoryStore())  # settings(): genie_concurrency_per_space=2

    assert SlowGenie.peak == 2


# ------------------------------------------------------------------ space versions


class FakeConfigs:
    def __init__(self, serialized: str):
        self.serialized = serialized

    async def get_space_config(self, space_id: str) -> SpaceConfig:
        return SpaceConfig(title="T", warehouse_id="wh", serialized_space=self.serialized)


class FakeVersions:
    def __init__(self):
        self.rows: list[dict] = []

    async def latest_space_version(self, space_id: str) -> str | None:
        return self.rows[-1]["version"] if self.rows else None

    async def write_space_version(self, row: dict) -> None:
        self.rows.append(row)


CONFIG = {
    "version": 1,
    "instructions": {
        "text_instructions": [{"id": "a", "content": ["R1 one\n", "R2 two\n"]}],
        "example_question_sqls": [{"id": "e", "question": ["q"], "sql": ["SELECT 1"]}],
    },
    "data_sources": {"tables": [{"identifier": "c.s.t", "column_configs": [{"column_name": "x"}]}]},
}


async def test_a_space_version_is_stored_only_when_the_configuration_changes():
    versions = FakeVersions()
    now = datetime(2026, 10, 22, tzinfo=UTC)

    first = await record_space_version("s", genie=FakeConfigs(json.dumps(CONFIG)), writer=versions, now=now)
    reordered = json.dumps(CONFIG, indent=2, sort_keys=True)  # same content, different text
    again = await record_space_version("s", genie=FakeConfigs(reordered), writer=versions, now=now)
    changed = {**CONFIG, "instructions": {**CONFIG["instructions"], "text_instructions": []}}
    second = await record_space_version("s", genie=FakeConfigs(json.dumps(changed)), writer=versions, now=now)

    assert first and again is None and second and second != first
    assert [r["previous_version"] for r in versions.rows] == [None, first]
    assert json.loads(versions.rows[0]["counts"]) == {
        "instructions": 2,
        "sql_examples": 1,
        "snippets": 0,
        "tables": 1,
        "column_configs": 1,
    }
