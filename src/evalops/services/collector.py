"""The `collect` job: incremental, idempotent capture of agent conversations.

Per monitored space:
1. read the cursor; list conversations created at or after `cursor - lookback`
   (the list gives only `created_timestamp`, and old conversations can get new turns);
2. per conversation, with at most `genie_concurrency_per_space` in flight:
   fetch items -> parse turns -> fetch each SQL step's result rows;
3. write raw snapshots, then MERGE traces and steps (idempotent on their keys);
4. advance the cursor only after the writes returned.

One conversation failing never fails the run: unreadable ones become placeholder traces
with an `access_status`, so they still count in coverage.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import cast_value
from evalops.config import Settings
from evalops.errors import UpstreamError
from evalops.schemas.genie import (
    AgentItem,
    Conversation,
    ConversationType,
    GenieSpace,
    QueryResultResponse,
    SpaceConfig,
)
from evalops.schemas.trace import AccessStatus, Step, ToolName, Trace
from evalops.services.delta_store import MAX_RESULT_ROWS, Snapshot, TraceWriter
from evalops.services.parser import access_failure_trace, parse_conversation

logger = logging.getLogger(__name__)


class GenieReader(Protocol):
    """The part of `GenieClient` the collector uses (so tests can pass a fake)."""

    async def list_spaces(self) -> list[GenieSpace]: ...

    async def can_manage(self, space_id: str) -> bool: ...

    def list_conversations(self, space_id: str): ...

    async def get_items(self, space_id: str, conversation_id: str) -> list[AgentItem]: ...

    async def get_query_result(
        self, space_id: str, conversation_id: str, message_id: str, call_id: str
    ) -> QueryResultResponse: ...


@dataclass
class CollectStats:
    space_id: str
    n_conversations: int = 0
    n_traces: int = 0
    n_traces_new: int = 0
    n_traces_updated: int = 0
    n_steps: int = 0
    n_errors: int = 0
    by_status: dict[str, int] = field(default_factory=dict)


@dataclass
class _Fetched:
    traces: list[Trace]
    steps: list[Step]
    snapshots: list[Snapshot]
    error: bool = False


async def monitored_spaces(
    genie: GenieReader, settings: Settings, store: StateStore | None = None
) -> list[str]:
    """Spaces where this identity (the collector SP) holds CAN_MANAGE, minus `exclude_spaces`.

    With a store, also records each visible space's status, so the app can show
    "monitored" or "grant CAN_MANAGE to the collector" per space.
    """
    spaces = [s for s in await genie.list_spaces() if s.space_id not in settings.exclude_spaces]
    allowed = await asyncio.gather(*(genie.can_manage(s.space_id) for s in spaces))
    if store is not None:
        for space, ok in zip(spaces, allowed, strict=True):
            await store.put_space_status(space.space_id, space.title, ok)
    return [space.space_id for space, ok in zip(spaces, allowed, strict=True) if ok]


async def collect_space(
    space_id: str,
    *,
    genie: GenieReader,
    writer: TraceWriter,
    store: StateStore,
    settings: Settings,
    run_id: str,
    now: datetime | None = None,
) -> CollectStats:
    """Collect one space and return its stats."""
    collected_at = now or datetime.now(UTC)
    stats = CollectStats(space_id=space_id)

    cursor = await store.get_cursor(space_id)
    since = cursor - timedelta(days=settings.lookback_days) if cursor else None
    conversations = [
        c async for c in genie.list_conversations(space_id) if since is None or _created(c) >= since
    ]
    stats.n_conversations = len(conversations)

    slots = asyncio.Semaphore(settings.genie_concurrency_per_space)

    async def bounded(conversation: Conversation) -> _Fetched:
        async with slots:
            return await fetch_conversation(genie, space_id, conversation, collected_at)

    fetched = await asyncio.gather(*(bounded(c) for c in conversations))

    traces = [t for f in fetched for t in f.traces]
    steps = [s for f in fetched for s in f.steps]
    await writer.write_snapshots([s for f in fetched for s in f.snapshots])
    trace_counts = await writer.merge_traces(traces)
    await writer.merge_steps(steps, space_id)

    if conversations:
        newest = max(_created(c) for c in conversations)
        await store.set_cursor(space_id, max(newest, cursor) if cursor else newest, run_id)

    stats.n_traces = len(traces)
    stats.n_traces_new = trace_counts.inserted
    stats.n_traces_updated = trace_counts.updated
    stats.n_steps = len(steps)
    stats.n_errors = sum(f.error for f in fetched)
    for trace in traces:
        stats.by_status[trace.access_status.value] = stats.by_status.get(trace.access_status.value, 0) + 1
    return stats


async def fetch_conversation(
    genie: GenieReader, space_id: str, conversation: Conversation, collected_at: datetime
) -> _Fetched:
    conversation_id = conversation.conversation_id
    created = _created(conversation)

    if conversation.agent_type != ConversationType.AGENT:
        trace = access_failure_trace(
            space_id, conversation_id, AccessStatus.CHAT_MODE, asked_at=created, collected_at=collected_at
        )
        return _Fetched([trace], [], [])

    try:
        items = await genie.get_items(space_id, conversation_id)
    except UpstreamError as exc:
        status = {403: AccessStatus.OWNER_ONLY, 404: AccessStatus.PRE_AGENT_MODE}.get(
            exc.status, AccessStatus.ERROR
        )
        if status == AccessStatus.ERROR:
            logger.warning("conversation %s/%s failed: %s", space_id, conversation_id, exc)
        trace = access_failure_trace(
            space_id, conversation_id, status, asked_at=created, collected_at=collected_at
        )
        return _Fetched([trace], [], [], error=status == AccessStatus.ERROR)

    snapshots = [
        Snapshot(space_id, conversation_id, "items", collected_at, [i.model_dump(mode="json") for i in items])
    ]
    traces: list[Trace] = []
    steps: list[Step] = []
    for trace, trace_steps in parse_conversation(space_id, conversation_id, items, collected_at=collected_at):
        for step in trace_steps:
            if step.tool_name == ToolName.EXECUTE_SQL and step.statement_id:
                await _fill_result(
                    genie, space_id, conversation_id, trace.trace_id, step, snapshots, collected_at
                )
        traces.append(trace)
        steps.extend(trace_steps)
    return _Fetched(traces, steps, snapshots)


async def _fill_result(
    genie: GenieReader,
    space_id: str,
    conversation_id: str,
    message_id: str,
    step: Step,
    snapshots: list[Snapshot],
    collected_at: datetime,
) -> None:
    """Attach a SQL step's result rows (typed) in place; record an error instead of failing."""
    try:
        result = await genie.get_query_result(space_id, conversation_id, message_id, step.call_id)
    except UpstreamError as exc:
        step.error = f"query-result: {exc.message}"
        return
    response = result.statement_response
    if not response:
        # Expired: leave the result fields empty; merge_steps keeps the rows stored earlier.
        logger.info("query result of %s/%s is no longer available", message_id, step.call_id)
        return
    snapshots.append(
        Snapshot(space_id, conversation_id, f"query-result:{step.call_id}", collected_at, response)
    )
    manifest = response.get("manifest", {})
    columns = manifest.get("schema", {}).get("columns", [])
    rows = (response.get("result") or {}).get("data_array") or []
    step.result_schema = [{"name": c["name"], "type_name": c.get("type_name", "STRING")} for c in columns]
    step.result_rows = [
        [cast_value(cell, c.get("type_name", "STRING")) for cell, c in zip(row, columns, strict=False)]
        for row in rows[:MAX_RESULT_ROWS]
    ]
    step.row_count = manifest.get("total_row_count", len(rows))


def _created(conversation: Conversation) -> datetime:
    return datetime.fromtimestamp(conversation.created_timestamp / 1000, tz=UTC)


class SpaceConfigReader(Protocol):
    async def get_space_config(self, space_id: str) -> SpaceConfig: ...


class SpaceVersionStore(Protocol):
    async def latest_space_version(self, space_id: str) -> str | None: ...

    async def write_space_version(self, row: dict) -> None: ...


async def record_space_version(
    space_id: str, *, genie: SpaceConfigReader, writer: SpaceVersionStore, now: datetime
) -> str | None:
    """Store the space's configuration when its content changed since the last stored version.
    Returns the new version, or None when nothing changed."""
    config = await genie.get_space_config(space_id)
    previous = await writer.latest_space_version(space_id)
    if config.version == previous:
        return None
    await writer.write_space_version(
        {
            "space_id": space_id,
            "version": config.version,
            "captured_at": now.isoformat(),
            "previous_version": previous,
            "title": config.title,
            "description": config.description,
            "warehouse_id": config.warehouse_id,
            "serialized_space": config.serialized_space,
            "counts": json.dumps(config.counts()),
        }
    )
    return config.version
