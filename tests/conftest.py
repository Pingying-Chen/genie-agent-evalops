"""Shared pytest fixtures. A fixture is a function pytest runs before a test and passes in by name."""

import httpx
import pytest

from evalops.clients.auth import Auth

HOST = "https://test.cloud.databricks.com"


@pytest.fixture
def auth() -> Auth:
    """A fake identity: no real token, so nothing can reach a real workspace."""
    return Auth(host=HOST, headers=lambda: {"Authorization": "Bearer test"}, principal="tester")


@pytest.fixture
async def http():
    """One real httpx client per test; respx intercepts its requests before they leave."""
    async with httpx.AsyncClient() as client:
        yield client
