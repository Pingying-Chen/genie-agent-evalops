"""Models for the raw Genie REST API payloads the collector reads.

These mirror what the API returns (verified against live responses, 2026-10). They are
lenient (`extra="allow"`) because the API is public but not documented as a trace format
and may add fields; the raw JSON is also stored verbatim in `raw_snapshots`.

Endpoints:
- `GET /api/2.0/genie/spaces/{space}/conversations?include_all=true` -> `ConversationsPage`
- `GET /api/2.0/genie/spaces` -> `SpacesPage`
- `GET /api/2.0/genie/spaces/{space}?include_serialized_space=true` -> `SpaceDefinition`
- `GET /api/2.0/genie/agents/{space}/conversations/{conv}/items?limit=&after=` -> `ItemsPage` (page with `after=<last_id>`)
- `GET /api/2.0/genie/spaces/{space}/conversations/{conv}/messages/{msg}/attachments/{call_id}/query-result`
  -> `QueryResultResponse`
"""

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Lenient(BaseModel):
    """Base for API payloads: keep unknown fields instead of failing."""

    model_config = ConfigDict(extra="allow")


class ConversationType(StrEnum):
    AGENT = "GENIE_CONVERSATION_TYPE_AGENT"
    CHAT = "GENIE_CONVERSATION_TYPE_CHAT"


class Conversation(Lenient):
    """One entry of the space's conversation list. The list has no privacy or owner field."""

    conversation_id: str
    title: str | None = None
    created_timestamp: int = Field(description="Epoch milliseconds.")
    agent_type: ConversationType | str


class ConversationsPage(Lenient):
    conversations: list[Conversation] = []
    next_page_token: str | None = None


class ItemType(StrEnum):
    MESSAGE = "message"
    REASONING = "reasoning"
    FUNCTION_CALL = "function_call"
    FUNCTION_CALL_OUTPUT = "function_call_output"


class ItemMetadata(Lenient):
    message_id: str | None = Field(default=None, description="Needed for the query-result endpoint.")
    statement_id: str | None = Field(default=None, description="Present on `execute_sql` outputs.")


class AgentItem(Lenient):
    """One item of an agent conversation, in order: user message, reasoning, tool calls and
    their outputs, assistant report.

    `id` is NOT a unique key: a chart call and its output share one id, and SQL outputs use a
    differently-cased id. Pair a call with its output by `call_id`.
    """

    id: str
    type: ItemType | str
    role: str | None = Field(default=None, description="`user` or `assistant` on message items.")
    content: list[dict[str, Any]] | None = Field(
        default=None,
        description="Message: `[{type: input_text|output_text, text}]`; reasoning: `[{type: reasoning_text, text}]`.",
    )
    name: str | None = Field(
        default=None, description="Tool name on function calls: `execute_sql`, `generate_visualization`."
    )
    call_id: str | None = Field(
        default=None, description="Links a function call to its output and to report citations."
    )
    arguments: str | None = Field(
        default=None, description="JSON string; for `execute_sql` it holds `title` and `sql`."
    )
    output: str | None = Field(
        default=None, description="Function output, a markdown table for `execute_sql`."
    )
    status: str | None = None
    created_at: float | None = Field(
        default=None,
        description="Epoch **seconds** (fractional), unlike the conversation list's milliseconds.",
    )
    completed_at: float | None = None
    metadata: ItemMetadata = ItemMetadata()


class ItemsPage(Lenient):
    data: list[AgentItem] = []
    has_more: bool = False
    first_id: str | None = None
    last_id: str | None = None


class GenieSpace(Lenient):
    space_id: str
    title: str | None = None


class SpaceDefinition(Lenient):
    """What a space tells its users and Genie: from `GET /genie/spaces/{id}?include_serialized_space=true`
    (needs CAN_MANAGE). `rules` are the text instructions; `tables` the data source identifiers."""

    title: str | None = None
    description: str | None = None
    rules: list[str] = []
    tables: list[str] = []

    @classmethod
    def from_api(cls, body: dict) -> "SpaceDefinition":
        serialized = json.loads(body.get("serialized_space") or "{}")
        instructions = serialized.get("instructions", {}).get("text_instructions", [])
        rules = [line.strip() for i in instructions for line in "".join(i.get("content", [])).splitlines()]
        tables = [t.get("identifier") for t in serialized.get("data_sources", {}).get("tables", [])]
        return cls(
            title=body.get("title"),
            description=body.get("description"),
            rules=[r for r in rules if r],
            tables=[t for t in tables if t],
        )


class SpaceConfig(Lenient):
    """A space's whole configuration, as `GET /genie/spaces/{id}?include_serialized_space=true`
    returns it (needs CAN_MANAGE). `serialized_space` is the JSON text a scratch copy is made from."""

    title: str | None = None
    description: str | None = None
    warehouse_id: str | None = None
    serialized_space: str = "{}"

    @property
    def version(self) -> str:
        """Content hash of the configuration (key order and whitespace do not count)."""
        canonical = json.dumps(
            json.loads(self.serialized_space or "{}"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def counts(self) -> dict[str, int]:
        """How much of each kind of configuration it holds, to summarise a change between versions."""
        s = json.loads(self.serialized_space or "{}")
        instructions = s.get("instructions", {})
        snippets = instructions.get("sql_snippets", {})
        tables = s.get("data_sources", {}).get("tables", [])
        return {
            "instructions": sum(
                1
                for i in instructions.get("text_instructions", [])
                for line in "".join(i.get("content", [])).splitlines()
                if line.strip()
            ),
            "sql_examples": len(instructions.get("example_question_sqls", [])),
            "snippets": sum(len(v) for v in snippets.values() if isinstance(v, list)),
            "tables": len(tables),
            "column_configs": sum(len(t.get("column_configs", [])) for t in tables),
        }


class SpacesPage(Lenient):
    spaces: list[GenieSpace] = []
    next_page_token: str | None = None


class QueryResultResponse(Lenient):
    """`statement_response` follows the SQL Statement Execution API shape (manifest + result)."""

    # `{}` once Genie no longer keeps the result (seen for day-old turns on Free Edition).
    statement_response: dict[str, Any] = Field(default_factory=dict)
