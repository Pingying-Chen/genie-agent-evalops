"""`/api/spaces`: space cards, per-space summary, trace list, and triggering collection."""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, Query, Request

from evalops.api.deps import (
    Viewer,
    allowed_spaces,
    get_app_auth,
    get_effective,
    get_http,
    get_sql,
    get_store,
    get_viewer,
    require_space,
)
from evalops.clients.auth import Auth
from evalops.clients.jobs import JobsClient
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.errors import ConfigError
from evalops.schemas.api import (
    AgentPerformance,
    CategoryCreate,
    CategoryUpdate,
    CauseUpdate,
    CollectResponse,
    CriterionDraft,
    Disagreement,
    Disagreements,
    FailureCategory,
    JudgeCalibration,
    JudgePerformance,
    Observations,
    ReviewerPerformance,
    SpaceCard,
    SpaceSummary,
    TagsUpdate,
    TraceListItem,
)
from evalops.schemas.common import ErrorResponse, Page
from evalops.services import disagreements, failures, performance, traces
from evalops.services.profiles import Effective

router = APIRouter(prefix="/spaces", tags=["spaces"])

ERRORS = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 502: {"model": ErrorResponse}}


@router.get("", response_model=list[SpaceCard], responses=ERRORS, summary="Spaces the viewer manages")
async def list_spaces(
    spaces: Annotated[list[tuple[str, str | None]], Depends(allowed_spaces)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[SpaceCard]:
    """Every space where the viewer holds CAN_MANAGE, with 30-day coverage, hard-gate pass rate and
    the last collection. Spaces the collector cannot read show `monitored: false`."""
    return await traces.list_space_cards(
        spaces, sql=sql, store=store, prefix=settings.table_prefix, today=date.today()
    )


@router.get(
    "/{space_id}/summary",
    response_model=SpaceSummary,
    responses=ERRORS,
    summary="Quality summary of one space",
)
async def get_summary(
    request: Request,
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
    window_from: Annotated[
        date | None, Query(alias="from", description="Inclusive; default 30 days ago.")
    ] = None,
    window_to: Annotated[date | None, Query(alias="to", description="Inclusive; default today.")] = None,
) -> SpaceSummary:
    """Coverage breakdown, pass rate per criterion and the daily trend for the window."""
    window_to = window_to or date.today()
    window_from = window_from or window_to - timedelta(days=30)
    summary = await traces.space_summary(
        space_id,
        window_from,
        window_to,
        sql=sql,
        store=store,
        prefix=settings.table_prefix,
        criteria=effective.criteria,
    )
    return summary.model_copy(update={"judge_sample_rate": effective.thresholds["judge_sample_rate"]})


@router.get(
    "/{space_id}/traces", response_model=Page[TraceListItem], responses=ERRORS, summary="Traces of one space"
)
async def list_traces(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
    window_from: Annotated[date | None, Query(alias="from")] = None,
    window_to: Annotated[date | None, Query(alias="to")] = None,
    verdict: Annotated[Literal["pass", "fail", "not_gradable"] | None, Query()] = None,
    criterion: Annotated[str | None, Query(description="Only traces failing this criterion id.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(description="`next_cursor` from the previous page.")] = None,
) -> Page[TraceListItem]:
    """Newest first. Filter by verdict or by a failing criterion."""
    return await traces.list_traces(
        space_id,
        window_from=window_from,
        window_to=window_to,
        verdict=verdict,
        criterion=criterion,
        limit=limit,
        cursor=cursor,
        sql=sql,
        prefix=settings.table_prefix,
    )


@router.get(
    "/{space_id}/calibration",
    response_model=JudgeCalibration,
    responses=ERRORS,
    summary="Human review progress and judge agreement",
)
async def get_calibration(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JudgeCalibration:
    """How many traces of the current session are labelled, and each judge's TPR / TNR on `fail`
    with Wilson lower bounds. Space owners only, like every route here."""
    return await traces.judge_calibration(
        space_id,
        sql=sql,
        prefix=settings.table_prefix,
        min_lower_bound=effective.thresholds["calibration_min_lower_bound"],
    )


@router.get(
    "/{space_id}/performance/agent",
    response_model=AgentPerformance,
    responses=ERRORS,
    summary="The agent's quality and operations by week",
)
async def get_agent_performance(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
    weeks: Annotated[int, Query(ge=1, le=52)] = 12,
) -> AgentPerformance:
    """Per UTC week asked: answers, the must-pass rate under the latest rules, task success as
    judged by people with its 95% interval, each judge's raw and corrected fail rate, and time to
    answer."""
    return await performance.agent(
        space_id, weeks=weeks, today=date.today(), sql=sql, prefix=settings.table_prefix
    )


@router.get(
    "/{space_id}/performance/judges",
    response_model=JudgePerformance,
    responses=ERRORS,
    summary="Each judge's agreement history and latest-run behaviour",
)
async def get_judge_performance(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JudgePerformance:
    """Per (criterion version, judge): TPR / TNR lower bounds at every review run, and in the
    current verdicts its `na` share, vote stability, errors and tokens."""
    return await performance.judges(
        space_id,
        sql=sql,
        prefix=settings.table_prefix,
        min_lower_bound=effective.thresholds["calibration_min_lower_bound"],
    )


@router.get(
    "/{space_id}/performance/reviewers",
    response_model=ReviewerPerformance,
    responses=ERRORS,
    summary="Each reviewer's activity, strictness and agreement",
)
async def get_reviewer_performance(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ReviewerPerformance:
    """Per reviewer: turns and labels, turnaround, fail rates beside the space's, reasons given,
    agreement with the judges; and Cohen's kappa between every two reviewers on the turns both
    reviewed."""
    return await performance.reviewers(space_id, now=datetime.now(UTC), sql=sql, prefix=settings.table_prefix)


@router.get(
    "/{space_id}/observations",
    response_model=Observations,
    responses=ERRORS,
    summary="Reviewed turns that failed or have findings",
)
async def get_observations(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Observations:
    """What reviewers found: each failed turn or turn with findings, the criteria that caught
    it (none = a blind spot) and its failure categories, plus the categories ranked by
    frequency x impact."""
    return await failures.analysis(space_id, sql=sql, store=store, prefix=settings.table_prefix)


CATEGORY_ERRORS = {**ERRORS, 404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}}


@router.post(
    "/{space_id}/categories",
    response_model=FailureCategory,
    status_code=201,
    responses=CATEGORY_ERRORS,
    summary="Name a kind of failure",
)
async def create_category(
    space_id: Annotated[str, Depends(require_space)],
    body: CategoryCreate,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
) -> FailureCategory:
    """409 if the space already has a category of that name (case-insensitive)."""
    return await failures.create_category(store, space_id, body, viewer.email)


@router.patch(
    "/{space_id}/categories/{category_id}",
    response_model=FailureCategory,
    responses=CATEGORY_ERRORS,
    summary="Rename, describe, rate or link a category",
)
async def update_category(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    category_id: str,
    body: CategoryUpdate,
    request: Request,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
) -> FailureCategory:
    """`criterion_id` must be a loaded criterion: the one that now covers this failure."""
    loaded = {c.id for c in effective.criteria}
    return await failures.update_category(store, space_id, category_id, body, viewer.email, loaded)


@router.get(
    "/{space_id}/categories/{category_id}/draft",
    response_model=CriterionDraft,
    responses=CATEGORY_ERRORS,
    summary="Draft a criterion from a category",
)
async def draft_criterion(
    space_id: Annotated[str, Depends(require_space)],
    category_id: str,
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CriterionDraft:
    """A criterion YAML to edit and add to the criteria folder, with the category's reviewed
    answers as examples. Nothing is written: criteria stay files under review."""
    return await failures.draft(space_id, category_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.delete(
    "/{space_id}/categories/{category_id}",
    status_code=204,
    responses=CATEGORY_ERRORS,
    summary="Delete a category and its tags",
)
async def delete_category(
    space_id: Annotated[str, Depends(require_space)],
    category_id: str,
    store: Annotated[StateStore, Depends(get_store)],
) -> None:
    await failures.delete_category(store, space_id, category_id)


@router.put(
    "/{space_id}/observations/{trace_id}/categories",
    response_model=TagsUpdate,
    responses=CATEGORY_ERRORS,
    summary="Set an observation's categories",
)
async def tag_observation(
    space_id: Annotated[str, Depends(require_space)],
    trace_id: str,
    body: TagsUpdate,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
) -> TagsUpdate:
    """Replaces the list; an empty list untags. Every id must be a category of this space."""
    ids = await failures.tag(store, space_id, trace_id, body.category_ids, viewer.email)
    return TagsUpdate(category_ids=ids)


@router.get(
    "/{space_id}/disagreements",
    response_model=Disagreements,
    responses=ERRORS,
    summary="Where the judges and the reviewers disagree",
)
async def get_disagreements(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Disagreements:
    """Every labelled turn whose current judge verdict differs from the reviewer's label, with
    both reasons and the owner's diagnosis. Owners only: it shows
    judge verdicts, which reviewers must not see before they label."""
    return await disagreements.disagreements(space_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.put(
    "/{space_id}/disagreements/{trace_id}/{criterion_id}",
    response_model=Disagreement,
    responses=CATEGORY_ERRORS,
    summary="Mark why a judge and a reviewer disagree",
)
async def mark_disagreement(
    space_id: Annotated[str, Depends(require_space)],
    trace_id: str,
    criterion_id: str,
    body: CauseUpdate,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Disagreement:
    """404 unless it is a current disagreement in this space."""
    return await disagreements.mark(
        space_id,
        trace_id,
        criterion_id,
        body,
        viewer.email,
        sql=sql,
        store=store,
        prefix=settings.table_prefix,
    )


@router.post(
    "/{space_id}/collect",
    response_model=CollectResponse,
    status_code=202,
    responses=ERRORS,
    summary="Collect now",
)
async def collect(
    space_id: Annotated[str, Depends(require_space)],
    http: Annotated[httpx.AsyncClient, Depends(get_http)],
    app_auth: Annotated[Auth, Depends(get_app_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CollectResponse:
    """Trigger the `collect` job for this space (as the app's identity, after the viewer's
    CAN_MANAGE was checked). Returns the job run id to poll at `GET /runs/{job_run_id}`."""
    if not settings.collect_job_id:
        raise ConfigError("EVALOPS_COLLECT_JOB_ID is not set.")
    run_id = await JobsClient(http, app_auth).run_now(settings.collect_job_id, {"space": space_id})
    return CollectResponse(job_run_id=run_id)
