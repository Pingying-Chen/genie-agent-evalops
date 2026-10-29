"""`/api/spaces/{space_id}/evaluations`: regression runs of the eval set.
Owners only."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends

from evalops.api.deps import get_app_auth, get_http, get_sql, get_store, require_space
from evalops.clients.auth import Auth
from evalops.clients.jobs import JobsClient
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.errors import ConfigError
from evalops.schemas.common import ErrorResponse
from evalops.schemas.evaluation import EvalRunDetail, EvalRuns, EvalStart, EvalStarted, scope_param
from evalops.services.regression import results

router = APIRouter(prefix="/spaces", tags=["regression runs"])

ERRORS = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get("/{space_id}/evaluations", response_model=EvalRuns, responses=ERRORS, summary="Regression runs")
async def list_runs(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalRuns:
    """The latest 20 runs, newest first, each with pass^k / pass@k / flaky counts per arm."""
    return await results.runs(space_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.get(
    "/{space_id}/evaluations/{run_id}",
    response_model=EvalRunDetail,
    responses=ERRORS,
    summary="One run, per case",
)
async def get_run(
    space_id: Annotated[str, Depends(require_space)],
    run_id: str,
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalRunDetail:
    """Every case with each arm's outcome over its k attempts, and every graded answer."""
    return await results.run_detail(space_id, run_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.post(
    "/{space_id}/evaluations",
    response_model=EvalStarted,
    status_code=202,
    responses=ERRORS,
    summary="Run the eval set on the current configuration",
)
async def start_run(
    space_id: Annotated[str, Depends(require_space)],
    body: EvalStart,
    http: Annotated[httpx.AsyncClient, Depends(get_http)],
    app_auth: Annotated[Auth, Depends(get_app_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalStarted:
    """Start the `evaluate` job (as the app, after the viewer's CAN_MANAGE was checked). The run
    appears in the list when the job starts; poll the job at `GET /runs/{job_run_id}`."""
    if not settings.evaluate_job_id:
        raise ConfigError("EVALOPS_EVALUATE_JOB_ID is not set.")
    run_id = await JobsClient(http, app_auth).run_now(
        settings.evaluate_job_id,
        {"space": space_id, "label": body.label, "split": body.split, "scope": scope_param(body.scope)},
    )
    return EvalStarted(job_run_id=run_id)
