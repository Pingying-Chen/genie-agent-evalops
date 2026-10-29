"""FastAPI application: lifespan-managed shared resources, routers, errors, static UI.

Run locally (no Databricks identity headers, so most endpoints return 401):
    uv run uvicorn evalops.main:app --reload
Swagger UI: /docs   OpenAPI JSON: /openapi.json
"""

import logging
import os
import pathlib
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from evalops import __version__
from evalops.api.routers import (
    changes,
    evalset,
    evaluations,
    evaluators,
    meta,
    profile,
    spaces,
    traces,
    versions,
)
from evalops.clients.auth import Auth
from evalops.clients.lakebase import open_store
from evalops.clients.sql import SqlClient
from evalops.config import get_settings
from evalops.errors import register_exception_handlers
from evalops.services.criteria import criteria_folders, load_criteria
from evalops.services.llm.skills import skills_dir
from evalops.services.profiles import Library

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

FRONTEND_DIST = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create one HTTP client and one state store per process; close them on shutdown."""
    settings = get_settings()
    app.state.host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    if app.state.host and not app.state.host.startswith("http"):
        app.state.host = f"https://{app.state.host}"
    app.state.http = httpx.AsyncClient(timeout=settings.http_timeout_seconds)
    app.state.criteria, app.state.criteria_version = load_criteria(
        *criteria_folders(settings.criteria_dir, settings.extra_criteria_dir, settings.root_dir)
    )
    app.state.library = Library(
        app.state.criteria, app.state.criteria_version, skills_dir(settings.skills_dir), settings
    )
    app.state.app_auth = _app_auth()
    app.state.sql = SqlClient(
        app.state.http,
        app.state.app_auth,
        settings.warehouse_id,
        max_concurrent=settings.max_concurrent_statements,
    )
    app.state.store = await open_store(
        settings.lakebase_endpoint,
        settings.lakebase_database,
        settings.lakebase_schema,
        user=settings.lakebase_user,
        require_durable=False,
        bootstrap=False,  # the jobs own and create the tables; the app only reads and caches
    )
    try:
        yield
    finally:
        await app.state.http.aclose()
        close = getattr(app.state.store, "close", None)
        if close:
            await close()


def _app_auth() -> Auth:
    """The app's service principal (Databricks Apps injects its credentials); a placeholder
    without credentials (tests, local runs with no profile) so the app still starts."""
    try:
        return Auth.from_sdk()
    except Exception:  # no Databricks auth configured
        logging.getLogger(__name__).warning("no Databricks credentials; app-identity calls will fail")
        return Auth(host="", headers=lambda: {}, principal="none")


def create_app() -> FastAPI:
    app = FastAPI(
        title="genie-agent-evalops",
        version=__version__,
        description="EvalOps for Databricks Genie Agents. All endpoints act as the signed-in viewer (on-behalf-of).",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    register_exception_handlers(app)
    for router in (
        meta.router,
        spaces.router,
        evalset.router,
        evaluations.router,
        changes.router,
        traces.router,
        versions.router,
        evaluators.router,
        profile.router,
    ):
        app.include_router(router, prefix="/api")
    if FRONTEND_DIST.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="ui")
    return app


app = create_app()
