"""Which spaces a viewer may read.

Rule: a viewer may read a space's stored data iff their own OBO token can list the space's
conversations with `include_all=true` (Genie enforces CAN_MANAGE). Results are cached per
(user, space) in the state store for `permission_cache_ttl_seconds`.
"""

from evalops.clients.genie import GenieClient
from evalops.clients.lakebase import StateStore
from evalops.errors import NotFound


async def is_allowed(
    user: str, space_id: str, *, viewer_genie: GenieClient, store: StateStore, ttl_seconds: int
) -> bool:
    """Cached CAN_MANAGE check for one space, made with the viewer's own token."""
    cached = await store.get_permission(user, space_id)
    if cached is not None:
        return cached
    allowed = await viewer_genie.can_manage(space_id)
    await store.put_permission(user, space_id, allowed, ttl_seconds)
    return allowed


async def require_allowed(user: str, space_id: str, **kwargs) -> None:
    """Raise `NotFound` (not 403) for trace lookups, so a disallowed trace's existence never leaks.

    Space-level endpoints raise `Forbidden` themselves where revealing the space is fine.
    """
    if not await is_allowed(user, space_id, **kwargs):
        raise NotFound("Not found.")
