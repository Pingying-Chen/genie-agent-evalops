"""Tests for evalops.clients.sql (SqlClient, cast_value, to_parameters).

No test touches a real workspace: respx intercepts every HTTP request the client makes.
"""

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
import pytest
import respx

from evalops.clients import http as http_module
from evalops.clients.sql import SqlClient, cast_value, to_parameters
from evalops.errors import UpstreamError

SUBMIT = "/api/2.0/sql/statements"
POLL = "/api/2.0/sql/statements/stmt-1"
CANCEL = "/api/2.0/sql/statements/stmt-1/cancel"
CHUNK_1 = "/api/2.0/sql/statements/stmt-1/result/chunks/1"


def statement_response(state: str, *, columns=None, rows=None, next_chunk=None, error=None) -> dict:
    """Build a Statement Execution API body, so each test only states what differs."""
    body = {"statement_id": "stmt-1", "status": {"state": state}}
    if error is not None:
        body["status"]["error"] = error
    if columns is not None:
        body["manifest"] = {"schema": {"columns": [{"name": n, "type_name": t} for n, t in columns]}}
        body["result"] = {"data_array": rows or []}
        if next_chunk is not None:
            body["result"]["next_chunk_index"] = next_chunk
    return body


ONE_INT_COLUMN = [("n", "INT")]


@pytest.fixture
def client(http, auth) -> SqlClient:
    """Fast polling so tests that poll finish in milliseconds."""
    return SqlClient(http, auth, warehouse_id="wh-1")


# ---------------------------------------------------------------- 1. happy path


async def test_execute_returns_typed_rows_when_statement_succeeds_immediately(client, auth):
    with respx.mock(base_url=auth.host, assert_all_called=True) as mock:
        submit = mock.post(SUBMIT).respond(
            200,
            json=statement_response(
                "SUCCEEDED",
                columns=[("continent", "STRING"), ("bookings", "LONG"), ("rate", "DOUBLE")],
                rows=[["Europe", "9131", "0.1987"], ["Asia", "8590", None]],
            ),
        )

        rows = await client.execute("SELECT ... WHERE year = :year", {"year": 2024})

    assert rows == [
        {"continent": "Europe", "bookings": 9131, "rate": 0.1987},
        {"continent": "Asia", "bookings": 8590, "rate": None},
    ]
    assert submit.call_count == 1
    sent = submit.calls.last.request
    assert sent.headers["Authorization"] == "Bearer test"
    payload = json.loads(sent.content)
    assert payload["warehouse_id"] == "wh-1"
    assert payload["parameters"] == [{"name": "year", "value": "2024", "type": "BIGINT"}]


# ---------------------------------------------------------------- 2. polling


async def test_execute_polls_until_the_statement_succeeds(client, auth):
    with respx.mock(base_url=auth.host, assert_all_called=True) as mock:
        mock.post(SUBMIT).respond(200, json=statement_response("PENDING"))
        poll = mock.get(POLL)
        poll.side_effect = [
            httpx.Response(200, json=statement_response("RUNNING")),
            httpx.Response(200, json=statement_response("RUNNING")),
            httpx.Response(200, json=statement_response("SUCCEEDED", columns=ONE_INT_COLUMN, rows=[["7"]])),
        ]

        rows = await client.execute("SELECT 7 AS n", poll_initial_s=0.001)

    assert rows == [{"n": 7}]
    assert poll.call_count == 3


# ---------------------------------------------------------------- 3. chunked results


async def test_execute_reads_every_result_chunk(client, auth):
    with respx.mock(base_url=auth.host, assert_all_called=True) as mock:
        mock.post(SUBMIT).respond(
            200,
            json=statement_response("SUCCEEDED", columns=ONE_INT_COLUMN, rows=[["1"], ["2"]], next_chunk=1),
        )
        mock.get(CHUNK_1).respond(200, json={"data_array": [["3"]]})

        rows = await client.execute("SELECT n FROM big")

    assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]


# ---------------------------------------------------------------- 4-5. failures


async def test_execute_raises_with_the_sql_error_when_the_statement_fails(client, auth):
    with respx.mock(base_url=auth.host) as mock:
        mock.post(SUBMIT).respond(200, json=statement_response("PENDING"))
        mock.get(POLL).respond(
            200,
            json=statement_response(
                "FAILED",
                error={"error_code": "DIVIDE_BY_ZERO", "message": "[DIVIDE_BY_ZERO] Division by zero."},
            ),
        )

        with pytest.raises(UpstreamError) as caught:
            await client.execute("SELECT 1/0", poll_initial_s=0.001)

    assert caught.value.details["error_code"] == "DIVIDE_BY_ZERO"
    assert caught.value.details["state"] == "FAILED"
    assert "Division by zero" in caught.value.message


async def test_execute_does_not_retry_a_bad_request_at_submit(client, auth):
    """A 400 (e.g. table not found) will not fix itself, so it must fail at once, not be retried."""
    with respx.mock(base_url=auth.host) as mock:
        submit = mock.post(SUBMIT).respond(
            400, json={"error_code": "BAD_REQUEST", "message": "[TABLE_OR_VIEW_NOT_FOUND] no_such_table"}
        )

        with pytest.raises(UpstreamError) as caught:
            await client.execute("SELECT * FROM no_such_table")

    assert caught.value.status == 400
    assert submit.call_count == 1


async def test_execute_retries_a_rate_limited_submit_then_succeeds(client, auth, monkeypatch):
    """429 is retried with backoff. monkeypatch removes the real sleeps so the test stays fast."""

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(http_module.asyncio, "sleep", no_sleep)
    with respx.mock(base_url=auth.host) as mock:
        submit = mock.post(SUBMIT)
        submit.side_effect = [
            httpx.Response(429, json={"error_code": "TOO_MANY_REQUESTS", "message": "slow down"}),
            httpx.Response(200, json=statement_response("SUCCEEDED", columns=ONE_INT_COLUMN, rows=[["1"]])),
        ]

        rows = await client.execute("SELECT 1 AS n")

    assert rows == [{"n": 1}]
    assert submit.call_count == 2


# ---------------------------------------------------------------- 6. timeout


async def test_execute_cancels_the_statement_when_it_times_out(client, auth):
    with respx.mock(base_url=auth.host) as mock:
        mock.post(SUBMIT).respond(200, json=statement_response("PENDING"))
        mock.get(POLL).respond(200, json=statement_response("RUNNING"))
        cancel = mock.post(CANCEL).respond(200, json={})

        with pytest.raises(UpstreamError, match="timed out"):
            await client.execute("SELECT slow()", timeout_s=0.05, poll_initial_s=0.01)

    assert cancel.called


# ---------------------------------------------------------------- 7. concurrency cap


async def test_execute_never_runs_more_statements_than_the_cap(http, auth):
    in_flight = 0
    peak = 0

    async def slow_submit(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)  # hold the slot long enough for the others to queue
        in_flight -= 1
        return httpx.Response(200, json=statement_response("SUCCEEDED", columns=ONE_INT_COLUMN, rows=[["1"]]))

    client = SqlClient(http, auth, warehouse_id="wh-1", max_concurrent=2)
    with respx.mock(base_url=auth.host) as mock:
        mock.post(SUBMIT).mock(side_effect=slow_submit)

        results = await asyncio.gather(*(client.execute("SELECT 1 AS n") for _ in range(5)))

    assert len(results) == 5
    assert peak == 2


# ---------------------------------------------------------------- 8. pure helpers


@pytest.mark.parametrize(
    ("value", "type_name", "expected"),
    [
        (None, "INT", None),
        ("42", "INT", 42),
        ("42", "long", 42),
        ("0.5", "DOUBLE", 0.5),
        ("12.30", "DECIMAL", Decimal("12.30")),
        ("true", "BOOLEAN", True),
        ("false", "BOOLEAN", False),
        ("2025-06-01", "DATE", date(2025, 6, 1)),
        ("2025-07-30T23:59:59Z", "TIMESTAMP", datetime(2025, 7, 30, 23, 59, 59, tzinfo=UTC)),
        ('["a","b"]', "ARRAY", ["a", "b"]),
        ("Paris", "STRING", "Paris"),
    ],
)
def test_cast_value(value, type_name, expected):
    assert cast_value(value, type_name) == expected


def test_to_parameters_types_and_stringifies_every_value():
    params = to_parameters(
        {"n": 3, "flag": True, "x": 0.25, "day": date(2025, 6, 1), "ids": "a,b", "missing": None}
    )

    assert params == [
        {"name": "n", "value": "3", "type": "BIGINT"},
        {"name": "flag", "value": "true", "type": "BOOLEAN"},
        {"name": "x", "value": "0.25", "type": "DOUBLE"},
        {"name": "day", "value": "2025-06-01", "type": "DATE"},
        {"name": "ids", "value": "a,b", "type": "STRING"},
        {"name": "missing", "type": "STRING"},
    ]
