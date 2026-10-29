"""FastAPI dependencies: identity, shared clients, and the per-request permission filter.

The Databricks Apps proxy forwards the viewer's identity in headers:
- `x-forwarded-access-token`: OBO token (scopes from app.yaml `user_api_scopes`)
- `x-forwarded-email`: the viewer's email
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated

import httpx
from fastapi import Depends, Header, Request

from evalops.clients.auth import Auth
from evalops.clients.genie import GenieClient
from evalops.clients.lakebase import StateStore
from evalops.clients.scim import ScimClient
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.errors import Forbidden, Unauthorized
from evalops.services import permissions, profiles
from evalops.services.profiles import Effective, Library


@dataclass(frozen=True)
class Viewer:
    email: str
    auth: Auth


def get_http(request: Request) -> httpx.AsyncClient:
    """The process-wide HTTP client, created in `main.lifespan`."""
    return request.app.state.http


def get_store(request: Request) -> StateStore:
    return request.app.state.store


def get_viewer(
    request: Request,
    token: Annotated[str | None, Header(alias="x-forwarded-access-token")] = None,
    email: Annotated[str | None, Header(alias="x-forwarded-email")] = None,
) -> Viewer:
    """The signed-in viewer, or 401. Never falls back to the service principal."""
    if not token or not email:
        raise Unauthorized("Missing on-behalf-of identity headers.")
    return Viewer(email=email, auth=Auth.from_obo(request.app.state.host, token, email))


def get_viewer_genie(
    viewer: Annotated[Viewer, Depends(get_viewer)], http: Annotated[httpx.AsyncClient, Depends(get_http)]
) -> GenieClient:
    """Genie client acting as the viewer (for permission checks)."""
    return GenieClient(http, viewer.auth)


def get_viewer_scim(
    viewer: Annotated[Viewer, Depends(get_viewer)], http: Annotated[httpx.AsyncClient, Depends(get_http)]
) -> ScimClient:
    """SCIM `/Me` as the viewer (their own group memberships)."""
    return ScimClient(http, viewer.auth)


def get_sql(request: Request) -> SqlClient:
    """SQL client acting as the **app's service principal** (created once in `main.lifespan`).

    Viewers get no grants on the evalops tables; the space filter is applied by the services
    after `require_space` / `allowed_spaces` decided with the viewer's own token.
    """
    return request.app.state.sql


def get_app_auth(request: Request) -> Auth:
    """The app's own identity (service principal), e.g. for triggering jobs."""
    return request.app.state.app_auth


async def allowed_spaces(
    viewer: Annotated[Viewer, Depends(get_viewer)],
    viewer_genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[tuple[str, str | None]]:
    """`(space_id, title)` for every space the viewer can see AND manage (checks run concurrently)."""
    spaces = await viewer_genie.list_spaces()
    checks = await asyncio.gather(
        *(
            permissions.is_allowed(
                viewer.email,
                s.space_id,
                viewer_genie=viewer_genie,
                store=store,
                ttl_seconds=settings.permission_cache_ttl_seconds,
            )
            for s in spaces
        )
    )
    return [(s.space_id, s.title) for s, ok in zip(spaces, checks, strict=True) if ok]


async def require_space(
    space_id: str,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    viewer_genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """Path dependency for `/spaces/{space_id}/...`: 403 unless the viewer holds CAN_MANAGE."""
    allowed = await permissions.is_allowed(
        viewer.email,
        space_id,
        viewer_genie=viewer_genie,
        store=store,
        ttl_seconds=settings.permission_cache_ttl_seconds,
    )
    if not allowed:
        raise Forbidden("You need CAN_MANAGE on this Genie space.", details={"space_id": space_id})
    return space_id


def get_library(request: Request) -> Library:
    """The rules, skills and defaults this deployment ships."""
    return request.app.state.library


async def get_effective(
    space_id: Annotated[str, Depends(require_space)],
    store: Annotated[StateStore, Depends(get_store)],
    library: Annotated[Library, Depends(get_library)],
) -> Effective:
    """The space's effective configuration: the library with its profile applied."""
    return await profiles.effective(store, library, space_id)
