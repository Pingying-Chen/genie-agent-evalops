"""`/api/health`, `/api/me`, `/api/criteria`, `/api/runs/{id}`."""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Request

from evalops.api.deps import (
    Viewer,
    allowed_spaces,
    get_app_auth,
    get_http,
    get_store,
    get_viewer,
    get_viewer_scim,
)
from evalops.clients.auth import Auth
from evalops.clients.jobs import JobsClient, run_state
from evalops.clients.lakebase import StateStore
from evalops.clients.scim import ScimClient
from evalops.config import Settings, get_settings
from evalops.errors import NotFound, UpstreamError
from evalops.schemas.api import CriteriaList, Me, RunStatus
from evalops.schemas.common import ErrorResponse
from evalops.services.reviewers import my_review_spaces

logger = logging.getLogger(__name__)
router = APIRouter(tags=["meta"])


@router.get("/health", summary="Liveness and dependency status")
async def health(request: Request) -> dict:
    """No auth. Reports whether Lakebase is durable or the in-memory fallback is active."""
    return {
        "status": "ok",
        "state_store": type(request.app.state.store).__name__,
        "criteria_version": request.app.state.criteria_version,
    }


@router.get("/me", response_model=Me, responses={401: {"model": ErrorResponse}}, summary="Who am I")
async def me(
    viewer: Annotated[Viewer, Depends(get_viewer)],
    spaces: Annotated[list[tuple[str, str | None]], Depends(allowed_spaces)],
    scim: Annotated[ScimClient, Depends(get_viewer_scim)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Me:
    """The viewer's email, the spaces they manage, and the spaces they review.

    Also registers the viewer as a reviewer of exactly those spaces: the review job assigns
    labeling sessions to registered reviewers, because it cannot list a group's members.
    """
    review_ids = await my_review_spaces(scim_as_viewer=scim, pattern=settings.reviewer_group_pattern)
    sessions: dict[str, str] = {}
    titles: dict[str, str] = {}
    try:
        await store.sync_reviewer(viewer.email, review_ids)
        if review_ids:
            sessions = await store.get_review_sessions(review_ids)
            status = await store.get_space_status(review_ids)
            titles = {s: v["title"] for s, v in status.items() if v.get("title")}
    except Exception:  # state store trouble must not lock anyone out of the app
        logger.warning("could not sync reviewer %s", viewer.email, exc_info=True)
    return Me(
        email=viewer.email,
        allowed_space_ids=[space_id for space_id, _ in spaces],
        review_space_ids=review_ids,
        review_titles=titles,
        review_sessions=sessions,
    )


@router.get("/criteria", response_model=CriteriaList, summary="Loaded criteria")
async def criteria(request: Request) -> CriteriaList:
    """Criteria loaded at startup and their content-hash version."""
    return CriteriaList(version=request.app.state.criteria_version, criteria=request.app.state.criteria)


@router.get(
    "/runs/{job_run_id}",
    response_model=RunStatus,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Collect run status",
)
async def run_status(
    job_run_id: int,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    http: Annotated[httpx.AsyncClient, Depends(get_http)],
    app_auth: Annotated[Auth, Depends(get_app_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> RunStatus:
    """State of a `collect` job run. Runs of any other job are reported as not found."""
    try:
        run = await JobsClient(http, app_auth).get_run(job_run_id)
    except UpstreamError as exc:
        if exc.status in (400, 403, 404):
            raise NotFound("Not found.") from exc
        raise
    if run.get("job_id") != settings.collect_job_id:
        raise NotFound("Not found.")
    return RunStatus(
        job_run_id=job_run_id, state=run_state(run), error=run.get("state", {}).get("state_message") or None
    )
