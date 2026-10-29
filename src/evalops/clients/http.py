"""Shared async HTTP plumbing: one `httpx.AsyncClient`, retries with backoff, error mapping.

Why async: every call here waits on the network (Genie, SQL warehouse, Jobs). While one
request waits, the event loop serves others, so one process handles many concurrent
requests without one thread per request.
"""

import asyncio
import logging
import random
from typing import Any

import httpx

from evalops.clients.auth import Auth
from evalops.errors import RateLimited, UpstreamError

logger = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


async def request_json(
    http: httpx.AsyncClient,
    auth: Auth,
    method: str,
    path: str,
    *,
    service: str,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    max_attempts: int = 5,
    base_delay: float = 0.5,
) -> dict[str, Any]:
    """Send one Databricks REST call and return the parsed JSON body.

    Retries 429/5xx with exponential backoff plus jitter (0.5s, 1s, 2s, ... capped at 8s).
    Raises `RateLimited` if 429 persists, `UpstreamError` for other failures; 4xx other
    than 429 are not retried (the caller decides, e.g. PERMISSION_DENIED -> owner_only).
    """
    url = f"{auth.host}{path}"
    for attempt in range(1, max_attempts + 1):
        try:
            response = await http.request(method, url, params=params, json=json, headers=auth.headers())
        except httpx.TransportError as exc:
            if attempt == max_attempts:
                raise UpstreamError(service, f"transport error: {exc}") from exc
        else:
            if response.status_code < 400:
                return response.json() if response.content else {}
            if response.status_code not in RETRY_STATUSES or attempt == max_attempts:
                body = _safe_body(response)
                error_cls = RateLimited if response.status_code == 429 else UpstreamError
                raise error_cls(
                    service,
                    body.get("message", response.reason_phrase),
                    status=response.status_code,
                    details=body,
                )
        delay = min(base_delay * 2 ** (attempt - 1), 8.0) * (0.5 + random.random())
        logger.info("%s %s retry %d in %.1fs", method, path, attempt, delay)
        await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _safe_body(response: httpx.Response) -> dict[str, Any]:
    """Databricks errors look like `{"error_code": ..., "message": ...}` (sometimes nested under `error`)."""
    try:
        body = response.json()
    except ValueError:
        return {"message": response.text[:500]}
    if isinstance(body.get("error"), dict):
        body = {**body["error"], **{k: v for k, v in body.items() if k != "error"}}
    return body
