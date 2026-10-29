"""Async client for the Genie REST endpoints the collector, the permission check and the tester use.

Paths and response shapes are listed in `evalops.schemas.genie`.
"""

import json
from collections.abc import AsyncIterator

import httpx

from evalops.clients.auth import Auth
from evalops.clients.http import request_json
from evalops.errors import UpstreamError
from evalops.schemas.genie import (
    AgentItem,
    Conversation,
    ConversationsPage,
    GenieSpace,
    ItemsPage,
    QueryResultResponse,
    SpaceConfig,
    SpaceDefinition,
    SpacesPage,
)

SERVICE = "genie"


class GenieClient:
    """Thin wrapper: one method per endpoint, typed results, no business logic."""

    def __init__(self, http: httpx.AsyncClient, auth: Auth):
        self._http = http
        self._auth = auth

    async def list_spaces(self) -> list[GenieSpace]:
        """Every space this identity can see (any permission level), following `next_page_token`."""
        spaces: list[GenieSpace] = []
        token: str | None = None
        while True:
            params: dict = {"page_size": 100}
            if token:
                params["page_token"] = token
            body = await request_json(
                self._http, self._auth, "GET", "/api/2.0/genie/spaces", service=SERVICE, params=params
            )
            page = SpacesPage.model_validate(body)
            spaces.extend(page.spaces)
            token = page.next_page_token
            if not token:
                return spaces

    async def list_conversations(
        self, space_id: str, *, include_all: bool = True
    ) -> AsyncIterator[Conversation]:
        """Yield every conversation in the space, following `next_page_token`.

        `include_all=true` returns all users' conversations and requires CAN_MANAGE.
        """
        token: str | None = None
        while True:
            params = {"include_all": str(include_all).lower(), "page_size": 100}
            if token:
                params["page_token"] = token
            body = await request_json(
                self._http,
                self._auth,
                "GET",
                f"/api/2.0/genie/spaces/{space_id}/conversations",
                service=SERVICE,
                params=params,
            )
            page = ConversationsPage.model_validate(body)
            for conversation in page.conversations:
                yield conversation
            token = page.next_page_token
            if not token:
                return

    async def get_items(
        self, space_id: str, conversation_id: str, *, page_size: int = 100
    ) -> list[AgentItem]:
        """All items of an agent conversation, in order (pages with `after=<last_id>` while `has_more`).

        Raises `UpstreamError` with status 403 (`does not own conversation`) or 404 (created
        before agent mode); the collector maps those to access statuses.
        """
        items: list[AgentItem] = []
        after: str | None = None
        path = f"/api/2.0/genie/agents/{space_id}/conversations/{conversation_id}/items"
        while True:
            params: dict = {"limit": page_size}
            if after:
                params["after"] = after
            body = await request_json(self._http, self._auth, "GET", path, service=SERVICE, params=params)
            page = ItemsPage.model_validate(body)
            items.extend(page.data)
            if not page.has_more or not page.last_id:
                return items
            after = page.last_id

    async def get_query_result(
        self, space_id: str, conversation_id: str, message_id: str, call_id: str
    ) -> QueryResultResponse:
        """Result rows of one `execute_sql` step (`attachment_id` = the function call's `call_id`)."""
        body = await request_json(
            self._http,
            self._auth,
            "GET",
            f"/api/2.0/genie/spaces/{space_id}/conversations/{conversation_id}"
            f"/messages/{message_id}/attachments/{call_id}/query-result",
            service=SERVICE,
        )
        return QueryResultResponse.model_validate(body)

    async def get_space_definition(self, space_id: str) -> SpaceDefinition:
        """The space's title, description, text instructions and tables (needs CAN_MANAGE)."""
        body = await request_json(
            self._http,
            self._auth,
            "GET",
            f"/api/2.0/genie/spaces/{space_id}",
            service=SERVICE,
            params={"include_serialized_space": "true"},
        )
        return SpaceDefinition.from_api(body)

    async def get_space_config(self, space_id: str) -> SpaceConfig:
        """The whole configuration, to version it and to copy it into a scratch space (needs CAN_MANAGE)."""
        body = await request_json(
            self._http,
            self._auth,
            "GET",
            f"/api/2.0/genie/spaces/{space_id}",
            service=SERVICE,
            params={"include_serialized_space": "true"},
        )
        return SpaceConfig.model_validate(body)

    async def create_space(
        self, *, title: str, description: str, warehouse_id: str, serialized_space: str, parent_path: str
    ) -> str:
        """A new space from a configuration; returns its id. The caller needs SELECT on its tables."""
        body = await request_json(
            self._http,
            self._auth,
            "POST",
            "/api/2.0/genie/spaces",
            service=SERVICE,
            json={
                "title": title,
                "description": description,
                "warehouse_id": warehouse_id,
                "serialized_space": serialized_space,
                "parent_path": parent_path,
            },
            max_attempts=2,  # not idempotent: a retried create can leave a second space behind
        )
        return body["space_id"]

    async def update_space(self, space_id: str, config: SpaceConfig) -> None:
        """Replace the space's configuration. A full replace: title, description, warehouse and the
        whole `serialized_space` are always sent (anything left out may be cleared)."""
        await request_json(
            self._http,
            self._auth,
            "PATCH",
            f"/api/2.0/genie/spaces/{space_id}",
            service=SERVICE,
            json={
                "title": config.title,
                "description": config.description or "",
                "warehouse_id": config.warehouse_id,
                "serialized_space": config.serialized_space,
            },
            max_attempts=2,
        )

    async def delete_space(self, space_id: str) -> None:
        await request_json(
            self._http, self._auth, "DELETE", f"/api/2.0/genie/spaces/{space_id}", service=SERVICE
        )

    async def ask_agent(self, space_id: str, text: str, conversation_id: str | None = None) -> str:
        """Ask in agent mode and wait for the whole answer; returns the conversation id.

        The answer streams as server-sent events; the items are read back afterwards with
        `get_items`, the same way the collector reads any conversation.
        """
        body: dict = {
            "input": [{"role": "user", "type": "message", "content": [{"type": "input_text", "text": text}]}],
            "enable_viz": True,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        url = f"{self._auth.host}/api/2.0/genie/agents/{space_id}/responses"
        headers = {**self._auth.headers(), "Accept": "text/event-stream"}
        failure: str | None = None
        async with self._http.stream("POST", url, json=body, headers=headers, timeout=900) as response:
            if response.status_code >= 400:
                detail = (await response.aread()).decode(errors="replace")[:1000]
                raise UpstreamError(SERVICE, detail or response.reason_phrase, status=response.status_code)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:])
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                answer = event.get("response") or {}
                conversation_id = conversation_id or answer.get("conversation_id")
                if event.get("type") in ("response.failed", "error"):
                    failure = json.dumps(answer.get("error") or event.get("error") or event)[:1000]
        if failure:
            raise UpstreamError(SERVICE, f"agent answer failed: {failure}")
        if not conversation_id:
            raise UpstreamError(SERVICE, "agent answer carried no conversation id")
        return conversation_id

    async def can_manage(self, space_id: str) -> bool:
        """True when this client's identity holds CAN_MANAGE on the space.

        Implemented as `conversations?include_all=true&page_size=1`: Genie enforces CAN_MANAGE
        on `include_all`, so a success means allowed and a 403 means not allowed.
        """
        try:
            await request_json(
                self._http,
                self._auth,
                "GET",
                f"/api/2.0/genie/spaces/{space_id}/conversations",
                service=SERVICE,
                params={"include_all": "true", "page_size": 1},
                max_attempts=2,
            )
        except UpstreamError as exc:
            if exc.status in (403, 404):
                return False
            raise
        return True
