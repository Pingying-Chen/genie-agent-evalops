"""Async client for the SQL Statement Execution REST API (Delta reads and writes).

Protocol:
    POST /api/2.0/sql/statements            submit; waits up to `wait_timeout` (max 50s)
      -> SUCCEEDED with the first result chunk, or PENDING/RUNNING with a `statement_id`
    GET  /api/2.0/sql/statements/{id}        poll until SUCCEEDED / FAILED / CANCELED / CLOSED
    GET  .../{id}/result/chunks/{index}      further chunks while `next_chunk_index` is set
    POST .../{id}/cancel                     on our own timeout, so no orphan query keeps running

Result cells arrive as JSON strings (or null); `execute` casts them with the manifest's
column `type_name`, so callers get int / float / Decimal / date / datetime / bool.

A semaphore caps concurrent statements per client: a 2X-Small warehouse queues a burst
and eventually rejects it, and one slow page should not starve the others.
"""

import asyncio
import json
import time
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from evalops.clients.auth import Auth
from evalops.clients.http import request_json
from evalops.errors import UpstreamError

SERVICE = "sql"
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}

_INT_TYPES = {"BYTE", "SHORT", "INT", "LONG"}
_FLOAT_TYPES = {"FLOAT", "DOUBLE"}
_JSON_TYPES = {"ARRAY", "MAP", "STRUCT"}


class SqlClient:
    def __init__(self, http: httpx.AsyncClient, auth: Auth, warehouse_id: str, *, max_concurrent: int = 4):
        self._http = http
        self._auth = auth
        self._warehouse_id = warehouse_id
        self._slots = asyncio.Semaphore(max_concurrent)

    async def execute(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        *,
        timeout_s: float = 300,
        poll_initial_s: float = 0.5,
        poll_max_s: float = 5.0,
    ) -> list[dict[str, Any]]:
        """Run one statement and return its rows as dicts with typed values.

        Use named parameters (`WHERE space_id = :space_id`) for every value; never format
        values into SQL. The API has no array parameters: pass a list as a comma-joined
        string and test membership with `array_contains(split(:ids, ','), space_id)`.

        Raises `UpstreamError` when the statement FAILS or is CANCELED, and after cancelling
        it when `timeout_s` elapses.
        """
        async with self._slots:
            deadline = time.monotonic() + timeout_s
            body = {
                "warehouse_id": self._warehouse_id,
                "statement": statement,
                "parameters": to_parameters(parameters or {}),
                "wait_timeout": "30s",
                "on_wait_timeout": "CONTINUE",
                "format": "JSON_ARRAY",
                "disposition": "INLINE",
            }
            response = await request_json(
                self._http, self._auth, "POST", "/api/2.0/sql/statements", service=SERVICE, json=body
            )
            statement_id = response.get("statement_id")
            delay = poll_initial_s
            while _state(response) not in TERMINAL:
                if time.monotonic() >= deadline:
                    await self._cancel(statement_id)
                    raise UpstreamError(
                        SERVICE, f"statement timed out after {timeout_s:.0f}s and was cancelled"
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, poll_max_s)
                response = await request_json(
                    self._http, self._auth, "GET", f"/api/2.0/sql/statements/{statement_id}", service=SERVICE
                )

            state = _state(response)
            if state != "SUCCEEDED":
                error = response.get("status", {}).get("error", {})
                raise UpstreamError(
                    SERVICE,
                    error.get("message", f"statement {state}"),
                    details={
                        "state": state,
                        "error_code": error.get("error_code"),
                        "statement_id": statement_id,
                    },
                )

            columns = response.get("manifest", {}).get("schema", {}).get("columns", [])
            rows = list(response.get("result", {}).get("data_array") or [])
            next_chunk = response.get("result", {}).get("next_chunk_index")
            while next_chunk is not None:
                chunk = await request_json(
                    self._http,
                    self._auth,
                    "GET",
                    f"/api/2.0/sql/statements/{statement_id}/result/chunks/{next_chunk}",
                    service=SERVICE,
                )
                rows.extend(chunk.get("data_array") or [])
                next_chunk = chunk.get("next_chunk_index")

            names = [c["name"] for c in columns]
            types = [c.get("type_name", "STRING") for c in columns]
            return [
                {name: cast_value(cell, type_) for name, cell, type_ in zip(names, row, types, strict=True)}
                for row in rows
            ]

    async def _cancel(self, statement_id: str | None) -> None:
        """Best effort: a failed cancel must not hide the timeout error."""
        if not statement_id:
            return
        try:
            await request_json(
                self._http,
                self._auth,
                "POST",
                f"/api/2.0/sql/statements/{statement_id}/cancel",
                service=SERVICE,
                max_attempts=1,
            )
        except UpstreamError:
            pass


def _state(response: dict[str, Any]) -> str:
    return response.get("status", {}).get("state", "PENDING")


def to_parameters(values: dict[str, Any]) -> list[dict[str, Any]]:
    """Python values -> Statement API parameters (`value` is always a string; None = SQL NULL)."""
    params = []
    for name, value in values.items():
        if value is None:
            params.append({"name": name, "type": "STRING"})
        elif isinstance(value, bool):
            params.append({"name": name, "value": str(value).lower(), "type": "BOOLEAN"})
        elif isinstance(value, int):
            params.append({"name": name, "value": str(value), "type": "BIGINT"})
        elif isinstance(value, float):
            params.append({"name": name, "value": repr(value), "type": "DOUBLE"})
        elif isinstance(value, Decimal):
            params.append({"name": name, "value": str(value), "type": "DECIMAL"})
        elif isinstance(value, datetime):
            params.append({"name": name, "value": value.isoformat(), "type": "TIMESTAMP"})
        elif isinstance(value, date):
            params.append({"name": name, "value": value.isoformat(), "type": "DATE"})
        elif isinstance(value, dict | list):
            params.append({"name": name, "value": json.dumps(value), "type": "STRING"})
        else:
            params.append({"name": name, "value": str(value), "type": "STRING"})
    return params


def cast_value(value: str | None, type_name: str) -> Any:
    """Cast one JSON-string cell to Python by its manifest `type_name`."""
    if value is None:
        return None
    type_name = type_name.upper()
    if type_name in _INT_TYPES:
        return int(value)
    if type_name in _FLOAT_TYPES:
        return float(value)
    if type_name == "DECIMAL":
        return Decimal(value)
    if type_name == "BOOLEAN":
        return value.lower() == "true"
    if type_name == "DATE":
        return date.fromisoformat(value)
    if type_name in ("TIMESTAMP", "TIMESTAMP_NTZ"):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if type_name in _JSON_TYPES:
        return json.loads(value)
    return value
