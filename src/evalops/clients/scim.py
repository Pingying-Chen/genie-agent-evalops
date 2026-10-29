"""Async client for SCIM `/Me`: who the caller is and which groups they belong to.

Only `/Me` is used, on purpose: a non-admin cannot read other principals' group memberships
(group `members` is omitted, `Groups/{id}` is admin-only), but anyone can read their own.
So membership is always checked with the person's own token, like Genie permissions.
"""

import httpx

from evalops.clients.auth import Auth
from evalops.clients.http import request_json

SERVICE = "scim"


class ScimClient:
    def __init__(self, http: httpx.AsyncClient, auth: Auth):
        self._http = http
        self._auth = auth

    async def my_groups(self) -> list[str]:
        """Display names of the groups the caller belongs to."""
        body = await request_json(
            self._http,
            self._auth,
            "GET",
            "/api/2.0/preview/scim/v2/Me",
            service=SERVICE,
            params={"attributes": "userName,groups"},
        )
        return [g["display"] for g in body.get("groups", []) if g.get("display")]
