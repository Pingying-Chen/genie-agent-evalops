"""Tests for evalops.clients.lakebase: the in-memory store, the token provider and open_store.

PostgresStore's SQL is exercised against a real Lakebase in the deploy milestone; here we
test everything that does not need a database.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evalops.clients.lakebase import InMemoryStore, TokenProvider, ddl, open_store, resolve_user
from evalops.errors import ConfigError


class FakeClock:
    """A clock tests can move forward by hand, instead of sleeping."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


# ------------------------------------------------------------------ InMemoryStore


async def test_cursor_is_none_until_set_then_round_trips():
    store = InMemoryStore()
    ts = datetime(2025, 7, 30, 23, 59, tzinfo=UTC)

    assert await store.get_cursor("space-1") is None
    await store.set_cursor("space-1", ts, run_id="run-1")

    assert await store.get_cursor("space-1") == ts
    assert await store.get_cursor("space-2") is None  # cursors are per space


async def test_permission_is_cached_until_the_ttl_expires():
    clock = FakeClock()
    store = InMemoryStore(clock=clock)
    await store.put_permission("jane@example.com", "space-1", True, ttl_seconds=300)

    clock.now += 299
    assert await store.get_permission("jane@example.com", "space-1") is True

    clock.now += 1  # exactly at expiry
    assert await store.get_permission("jane@example.com", "space-1") is None


async def test_a_cached_denial_is_returned_as_false_not_as_missing():
    """`False` (known: not allowed) and `None` (unknown: ask Genie) must stay different."""
    store = InMemoryStore()
    await store.put_permission("jane@example.com", "space-1", False, ttl_seconds=300)

    assert await store.get_permission("jane@example.com", "space-1") is False
    assert await store.get_permission("other@example.com", "space-1") is None


# ------------------------------------------------------------------ TokenProvider


class FakePostgresAPI:
    """Stands in for `w.postgres`: counts credential calls and returns numbered tokens."""

    def __init__(self, delay_s: float = 0.0):
        self.calls = 0
        self.delay_s = delay_s

    def generate_database_credential(self, endpoint: str):
        # Synchronous, like the real SDK; TokenProvider runs it in a worker thread.
        if self.delay_s:
            import time

            time.sleep(self.delay_s)
        self.calls += 1
        return SimpleNamespace(token=f"token-{self.calls}")


def fake_workspace(postgres: FakePostgresAPI):
    return SimpleNamespace(postgres=postgres)


async def test_token_is_reused_while_fresh_and_reminted_when_stale():
    postgres = FakePostgresAPI()
    clock = FakeClock()
    provider = TokenProvider(
        fake_workspace(postgres), "projects/p/branches/b/endpoints/e", max_age_s=60, clock=clock
    )

    assert await provider() == "token-1"
    clock.now += 59
    assert await provider() == "token-1"  # still fresh: no new credential call
    clock.now += 1
    assert await provider() == "token-2"  # stale: minted again
    assert postgres.calls == 2


async def test_concurrent_connections_share_one_refresh():
    """Ten new connections at once must not mint ten tokens: the lock makes them wait for one."""
    postgres = FakePostgresAPI(delay_s=0.05)
    provider = TokenProvider(fake_workspace(postgres), "e", clock=FakeClock())

    tokens = await asyncio.gather(*(provider() for _ in range(10)))

    assert set(tokens) == {"token-1"}
    assert postgres.calls == 1


async def test_an_empty_credential_is_a_config_error():
    class EmptyPostgres(FakePostgresAPI):
        def generate_database_credential(self, endpoint: str):
            return SimpleNamespace(token="")

    provider = TokenProvider(fake_workspace(EmptyPostgres()), "e")

    with pytest.raises(ConfigError):
        await provider()


# ------------------------------------------------------------------ open_store and DDL


async def test_open_store_falls_back_to_memory_for_the_app():
    store = await open_store("", "databricks_postgres", "evalops", require_durable=False)

    assert isinstance(store, InMemoryStore)


async def test_open_store_refuses_memory_for_the_collector():
    with pytest.raises(ConfigError, match="Lakebase is required"):
        await open_store("", "databricks_postgres", "evalops", require_durable=True)


def test_ddl_is_idempotent_and_covers_every_operational_table():
    statements = ddl("evalops")
    joined = "\n".join(statements)

    assert all("IF NOT EXISTS" in s or "IF EXISTS" in s or "ON CONFLICT DO NOTHING" in s for s in statements)
    # Never an unconditional ALTER: grant.sh runs the DDL as someone who does not own the tables.
    assert not [s for s in statements if s.lstrip().startswith("ALTER")]
    for table in (
        "collector_cursor",
        "collector_runs",
        "space_status",
        "permission_cache",
        "daily_rollup",
        "reviewers",
        "review_sessions",
        "failure_categories",
        "observation_tags",
        "disagreement_causes",
    ):
        assert f"evalops.{table}" in joined


# ------------------------------------------------------------------ resolve_user


def fake_identity(client_id: str | None, user_name: str | None):
    return SimpleNamespace(
        config=SimpleNamespace(client_id=client_id),
        current_user=SimpleNamespace(me=lambda: SimpleNamespace(user_name=user_name)),
    )


@pytest.mark.parametrize(
    ("configured", "client_id", "user_name", "expected"),
    [
        ("evalops_app", "sp-uuid", "jane@example.com", "evalops_app"),  # explicit setting wins
        ("", "sp-uuid", "jane@example.com", "sp-uuid"),  # service principal: its client id
        ("", None, "jane@example.com", "jane@example.com"),  # user login (e.g. local dev)
    ],
)
async def test_resolve_user_precedence(configured, client_id, user_name, expected):
    assert await resolve_user(fake_identity(client_id, user_name), configured) == expected


async def test_resolve_user_without_any_identity_is_a_config_error():
    with pytest.raises(ConfigError, match="EVALOPS_LAKEBASE_USER"):
        await resolve_user(fake_identity(None, None), "")


async def test_space_status_keeps_the_title_when_a_revoked_space_reports_none():
    """A revoked space is invisible to the collector, so it is recorded with title=None."""
    store = InMemoryStore()
    await store.put_space_status("space-1", "Bookings", True)
    await store.put_space_status("space-1", None, False)

    status = (await store.get_space_status(["space-1"]))["space-1"]
    assert status["title"] == "Bookings" and status["collector_has_manage"] is False


async def test_a_reviewer_who_left_a_group_is_dropped_from_that_space_only():
    """The app re-syncs a reviewer on every /me: their current groups replace the old ones."""
    store = InMemoryStore()
    await store.sync_reviewer("ann@example.com", ["space-1", "space-2"])
    await store.sync_reviewer("bob@example.com", ["space-1"])

    await store.sync_reviewer("ann@example.com", ["space-2"])

    assert await store.get_reviewers("space-1") == ["bob@example.com"]
    assert await store.get_reviewers("space-2") == ["ann@example.com"]


def test_both_stores_implement_every_method_of_the_protocol():
    # Only the in-memory store runs in tests, so a Postgres method that is missing (or defined
    # outside the class) would otherwise surface as a 500 in the deployed app.
    from evalops.clients.lakebase import InMemoryStore, PostgresStore, StateStore

    wanted = {name for name in vars(StateStore) if not name.startswith("_")}
    for store in (InMemoryStore, PostgresStore):
        assert wanted <= set(dir(store)), f"{store.__name__} lacks {sorted(wanted - set(dir(store)))}"


def test_an_eval_case_reaches_postgres_with_a_real_timestamp():
    # asyncpg rejects an ISO string for TIMESTAMPTZ; the service must pass Python values.
    import asyncio
    from datetime import UTC, datetime

    from evalops.clients.lakebase import PostgresStore
    from evalops.schemas.evalset import EvalCase

    sent = []

    class Pool:
        async def execute(self, statement, *args):
            sent.append(args)

    store = PostgresStore.__new__(PostgresStore)
    store._pool, store._s = Pool(), "evalops"
    case = EvalCase(
        case_id="c1",
        space_id="s",
        version=1,
        title="t",
        turns=["q"],
        updated_by="u",
        updated_at=datetime.now(UTC),
    )
    asyncio.run(store.add_eval_case_version("s", case.model_dump()))

    assert isinstance(sent[0][5], datetime)
    assert '"turns": ["q"]' in sent[0][3]
