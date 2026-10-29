"""Domain models for captured traces: what the collector writes and the API reads.

A **trace** is one agent turn (user message + response), keyed by the Genie message id.
A **step** is one tool call inside a trace.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AccessStatus(StrEnum):
    """Whether a conversation could be read, and so whether it can be graded."""

    OK = "ok"
    OWNER_ONLY = "owner_only"  # PERMISSION_DENIED: does not own conversation
    PRE_AGENT_MODE = "pre_agent_mode"  # NOT_FOUND: created before agent mode existed
    CHAT_MODE = "chat_mode"  # chat-mode conversation: counted, not graded in the MVP
    ERROR = "error"


class TraceSource(StrEnum):
    COLLECTOR = "collector"
    SEED = "seed"


class ToolName(StrEnum):
    EXECUTE_SQL = "execute_sql"
    GENERATE_VISUALIZATION = "generate_visualization"
    OTHER = "other"


class StepKind(StrEnum):
    """`explore` = schema probe; `answer` = query that feeds the report; `viz` = chart."""

    ANSWER = "answer"
    EXPLORE = "explore"
    VIZ = "viz"


class Step(BaseModel):
    """One tool call within a trace."""

    trace_id: str
    step_index: int = Field(ge=0, description="0-based order within the trace.")
    call_id: str
    tool_name: ToolName
    step_kind: StepKind
    title: str | None = None
    sql: str | None = Field(default=None, description="SQL text for `execute_sql` steps.")
    statement_id: str | None = None
    status: str | None = None
    row_count: int | None = None
    result_schema: list[dict[str, Any]] | None = Field(
        default=None, description="Columns as `[{name, type_name}]` from the statement manifest."
    )
    result_rows: list[list[Any]] | None = Field(default=None, description="First 200 rows, typed.")
    error: str | None = None
    source_call_id: str | None = Field(
        default=None, description="For a chart: `call_id` of the `execute_sql` step it plots."
    )


class Trace(BaseModel):
    """One agent turn."""

    trace_id: str = Field(description="Genie message id.")
    space_id: str
    conversation_id: str
    turn_index: int = Field(ge=0, description="0-based turn number within the conversation.")
    user_id: str | None = None
    asked_at: datetime | None = None
    completed_at: datetime | None = None
    question: str | None = None
    report_text: str | None = None
    genie_error: str | None = Field(
        default=None, description="Genie's own error message when the turn ended without an answer."
    )
    n_sql_steps: int = 0
    n_tool_calls: int = 0
    access_status: AccessStatus
    source: TraceSource = TraceSource.COLLECTOR
    parser_version: str
    collected_at: datetime
