"""Tests for evalops.clients.genie: pagination and error mapping, with HTTP mocked by respx."""

import httpx
import pytest
import respx

from evalops.clients.genie import GenieClient
from evalops.errors import UpstreamError

SPACE, CONV = "space-1", "conv-1"
ITEMS = f"/api/2.0/genie/agents/{SPACE}/conversations/{CONV}/items"


def conv(cid: str) -> dict:
    return {"conversation_id": cid, "created_timestamp": 1, "agent_type": "GENIE_CONVERSATION_TYPE_AGENT"}


def item(i: int) -> dict:
    return {"id": f"item-{i}", "type": "reasoning", "metadata": {"message_id": "msg-1"}}


async def test_get_items_follows_after_until_has_more_is_false(http, auth):
    genie = GenieClient(http, auth)
    with respx.mock(base_url=auth.host, assert_all_called=True) as mock:
        route = mock.get(ITEMS)
        route.side_effect = [
            httpx.Response(200, json={"data": [item(0), item(1)], "has_more": True, "last_id": "item-1"}),
            httpx.Response(200, json={"data": [item(2)], "has_more": False, "last_id": "item-2"}),
        ]

        items = await genie.get_items(SPACE, CONV, page_size=2)

    assert [i.id for i in items] == ["item-0", "item-1", "item-2"]
    first, second = (call.request.url.params for call in route.calls)
    assert "after" not in first
    assert second["after"] == "item-1" and second["limit"] == "2"


async def test_get_items_surfaces_the_status_the_collector_maps(http, auth):
    """403 'does not own conversation' must reach the collector as status 403 (-> owner_only)."""
    genie = GenieClient(http, auth)
    with respx.mock(base_url=auth.host) as mock:
        mock.get(ITEMS).respond(
            403,
            json={"error_code": "PERMISSION_DENIED", "message": "User 1 does not own conversation conv-1"},
        )

        with pytest.raises(UpstreamError) as caught:
            await genie.get_items(SPACE, CONV)

    assert caught.value.status == 403
    assert "does not own conversation" in caught.value.message


async def test_list_conversations_yields_across_pages(http, auth):
    genie = GenieClient(http, auth)
    with respx.mock(base_url=auth.host) as mock:
        route = mock.get(f"/api/2.0/genie/spaces/{SPACE}/conversations")
        route.side_effect = [
            httpx.Response(200, json={"conversations": [conv("a"), conv("b")], "next_page_token": "t2"}),
            httpx.Response(200, json={"conversations": [conv("c")]}),
        ]

        ids = [c.conversation_id async for c in genie.list_conversations(SPACE)]

    assert ids == ["a", "b", "c"]
    assert route.calls[0].request.url.params["include_all"] == "true"
    assert route.calls[1].request.url.params["page_token"] == "t2"


@pytest.mark.parametrize(("status", "expected"), [(200, True), (403, False), (404, False)])
async def test_can_manage_maps_status_to_a_boolean(http, auth, status, expected):
    genie = GenieClient(http, auth)
    with respx.mock(base_url=auth.host) as mock:
        mock.get(f"/api/2.0/genie/spaces/{SPACE}/conversations").respond(status, json={"conversations": []})

        assert await genie.can_manage(SPACE) is expected
