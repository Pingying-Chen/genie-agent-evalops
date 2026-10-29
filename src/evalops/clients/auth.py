"""Who a client call runs as.

Two identities exist:
- the **viewer**, via the on-behalf-of (OBO) token the Databricks Apps proxy forwards;
- the **collector service principal**, via the Databricks SDK's default auth (jobs, app SP).

Clients take an `Auth` and ask it for headers on every request, so the same client code
serves both identities and tokens can refresh underneath.
"""

from collections.abc import Callable
from dataclasses import dataclass

from databricks.sdk import WorkspaceClient


@dataclass(frozen=True)
class Auth:
    """Host plus a header provider. `headers()` is called per request (tokens may rotate)."""

    host: str
    headers: Callable[[], dict[str, str]]
    principal: str  # email or SP application id, for logs and cache keys

    @classmethod
    def from_obo(cls, host: str, token: str, email: str) -> "Auth":
        """The viewer's identity; the token comes from `x-forwarded-access-token`."""
        return cls(
            host=host.rstrip("/"), headers=lambda: {"Authorization": f"Bearer {token}"}, principal=email
        )

    @classmethod
    def from_sdk(cls, w: WorkspaceClient | None = None) -> "Auth":
        """The process identity (the app's or the job's service principal)."""
        w = w or WorkspaceClient()
        return cls(
            host=w.config.host.rstrip("/"),
            headers=w.config.authenticate,
            principal=w.config.client_id or "sdk",
        )
