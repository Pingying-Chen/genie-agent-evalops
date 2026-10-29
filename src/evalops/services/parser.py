"""Turn raw Genie agent items into `Trace` and `Step` rows.

Pure functions, no I/O, so they are easy to unit test against saved API responses
(`tests/fixtures/genie/`).

Facts the parser relies on (verified 2026-10):
- items arrive in order: user message, reasoning, function_call / function_call_output
  pairs, assistant message; a multi-turn conversation repeats this per turn;
- every item's `metadata.message_id` identifies its turn (= `trace_id`);
- item `id`s are not unique (a chart call and its output share one id), so a call is paired
  with its output by `call_id`, and a step's key is (trace_id, step_index);
- `function_call.name` is `execute_sql` or `generate_visualization`; only `execute_sql`
  outputs carry `metadata.statement_id`;
- item timestamps are epoch seconds (fractional);
- report citations do NOT map to steps by position (`[1]` may cite the second SQL step),
  so the parser does not try to link them;
- when Genie fails (e.g. the asker cannot read the data), the turn ends with a `system`
  message ("An internal error occurred") instead of an assistant report.
"""

import json
import re
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from evalops.schemas.genie import AgentItem, ItemType
from evalops.schemas.trace import AccessStatus, Step, StepKind, ToolName, Trace, TraceSource

PARSER_VERSION = "2"  # 2: charts keep the query they plot (`source_call_id`)

_EXPLORE_TITLE = re.compile(r"\b(sample|check|available|explore|preview|schema|columns?)\b", re.IGNORECASE)
_SELECT_STAR = re.compile(r"\bselect\s+\*", re.IGNORECASE)
_LIMIT = re.compile(r"\blimit\s+(\d+)\b", re.IGNORECASE)
_AGGREGATE = re.compile(r"\b(measure|sum|count|avg|min|max)\s*\(", re.IGNORECASE)


def split_turns(items: list[AgentItem]) -> "OrderedDict[str, list[AgentItem]]":
    """Group items by `metadata.message_id`, preserving first-seen order. Items without one are dropped."""
    turns: OrderedDict[str, list[AgentItem]] = OrderedDict()
    for item in items:
        message_id = item.metadata.message_id
        if message_id:
            turns.setdefault(message_id, []).append(item)
    return turns


def parse_turn(
    space_id: str,
    conversation_id: str,
    turn_index: int,
    items: list[AgentItem],
    *,
    collected_at: datetime,
    source: TraceSource = TraceSource.COLLECTOR,
) -> tuple[Trace, list[Step]]:
    """Build one trace and its steps from one turn's items. Step results are filled in later
    by the collector (they need another API call per SQL step)."""
    trace_id = items[0].metadata.message_id or ""
    user_messages = [i for i in items if i.type == ItemType.MESSAGE and i.role == "user"]
    assistant_messages = [i for i in items if i.type == ItemType.MESSAGE and i.role == "assistant"]
    # Genie reports its own failures as a `system` message ("An internal error occurred").
    system_messages = [i for i in items if i.type == ItemType.MESSAGE and i.role == "system"]
    outputs = {i.call_id: i for i in items if i.type == ItemType.FUNCTION_CALL_OUTPUT and i.call_id}

    steps: list[Step] = []
    for call in (i for i in items if i.type == ItemType.FUNCTION_CALL):
        args = _arguments(call.arguments)
        tool = _tool_name(call.name)
        output = outputs.get(call.call_id)
        title, sql = args.get("title"), args.get("sql")
        steps.append(
            Step(
                trace_id=trace_id,
                step_index=len(steps),
                call_id=call.call_id or "",
                tool_name=tool,
                step_kind=classify_step(title, sql, tool),
                title=title,
                sql=sql.strip() if isinstance(sql, str) else None,
                statement_id=output.metadata.statement_id if output else None,
                status=(output.status if output else None) or call.status,
                source_call_id=args.get("query_attachment_id")
                if tool == ToolName.GENERATE_VISUALIZATION
                else None,
            )
        )

    trace = Trace(
        trace_id=trace_id,
        space_id=space_id,
        conversation_id=conversation_id,
        turn_index=turn_index,
        asked_at=_ts(user_messages[0].created_at) if user_messages else None,
        completed_at=_ts(assistant_messages[-1].completed_at) if assistant_messages else None,
        question=_text(user_messages[0]) if user_messages else None,
        report_text="\n\n".join(t for t in (_text(m) for m in assistant_messages) if t) or None,
        genie_error="; ".join(t for t in (_text(m) for m in system_messages) if t) or None,
        n_sql_steps=sum(1 for s in steps if s.tool_name == ToolName.EXECUTE_SQL),
        n_tool_calls=len(steps),
        access_status=AccessStatus.OK,
        source=source,
        parser_version=PARSER_VERSION,
        collected_at=collected_at,
    )
    return trace, steps


def parse_conversation(
    space_id: str, conversation_id: str, items: list[AgentItem], *, collected_at: datetime
) -> list[tuple[Trace, list[Step]]]:
    """All turns of one conversation, `turn_index` 0..n-1 in order."""
    return [
        parse_turn(space_id, conversation_id, index, turn_items, collected_at=collected_at)
        for index, turn_items in enumerate(split_turns(items).values())
    ]


def classify_step(title: str | None, sql: str | None, tool_name: str) -> StepKind:
    """`viz` for charts; `explore` for schema probes; otherwise `answer`.

    A probe is `SELECT *`, or a small `LIMIT` (<= 10) with no aggregate, or a title that says
    "sample / check / available ..." — e.g. "Sample booking data to check available fields".
    """
    if tool_name == ToolName.GENERATE_VISUALIZATION:
        return StepKind.VIZ
    if tool_name != ToolName.EXECUTE_SQL:
        return StepKind.ANSWER
    sql = sql or ""
    if _SELECT_STAR.search(sql):
        return StepKind.EXPLORE
    limit = _LIMIT.search(sql)
    if limit and int(limit.group(1)) <= 10 and not _AGGREGATE.search(sql):
        return StepKind.EXPLORE
    if title and _EXPLORE_TITLE.search(title) and (not limit or int(limit.group(1)) <= 10):
        return StepKind.EXPLORE
    return StepKind.ANSWER


def access_failure_trace(
    space_id: str,
    conversation_id: str,
    status: AccessStatus,
    *,
    asked_at: datetime | None,
    collected_at: datetime,
) -> Trace:
    """A placeholder trace for a conversation that could not be read, so it counts in coverage.

    Its id is `<conversation_id>:unreadable` because no message id is known.
    """
    return Trace(
        trace_id=f"{conversation_id}:unreadable",
        space_id=space_id,
        conversation_id=conversation_id,
        turn_index=0,
        asked_at=asked_at,
        access_status=status,
        parser_version=PARSER_VERSION,
        collected_at=collected_at,
    )


def _arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _tool_name(name: str | None) -> ToolName:
    try:
        return ToolName(name or "")
    except ValueError:
        return ToolName.OTHER


def _text(message: AgentItem) -> str | None:
    parts = [
        c.get("text", "") for c in (message.content or []) if c.get("type") in ("input_text", "output_text")
    ]
    text = "".join(parts).strip()
    return text or None


def _ts(seconds: float | None) -> datetime | None:
    return datetime.fromtimestamp(seconds, tz=UTC) if seconds else None
