"""`/api/spaces/{space_id}/changes`: configuration changes, tested before they are applied
. Owners only."""

from typing import Annotated

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from evalops.api.deps import (
    Viewer,
    get_app_auth,
    get_effective,
    get_http,
    get_sql,
    get_store,
    get_viewer,
    get_viewer_genie,
    require_space,
)
from evalops.clients.auth import Auth
from evalops.clients.genie import GenieClient
from evalops.clients.jobs import JobsClient
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.errors import ConfigError, Conflict
from evalops.schemas.changes import ChangeBody, ChangeProposal, SpaceChange
from evalops.schemas.common import ErrorResponse
from evalops.schemas.evalset import Leak
from evalops.schemas.evaluation import ChangeTest, EvalStarted, scope_param
from evalops.services import evalset, space_changes, splits
from evalops.services.llm.runner import SkillRunner
from evalops.services.profiles import Effective
from evalops.services.regression.changes import describe
from evalops.services.regression.leakage import added_examples, leaks

router = APIRouter(prefix="/spaces", tags=["changes"])

ERRORS = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


class ChangeView(SpaceChange):
    steps: list[str] = Field(description="One line per operation.")
    leaks: list[Leak] = Field(
        default_factory=list,
        description="SQL examples it adds that match an active eval case's question. On a test case it "
        "is train/test leakage (the case cannot count as fixed); on a dev case it is allowed and listed.",
    )
    test_targets: list[str] = Field(
        default_factory=list,
        description="Target cases in the test split: the change is tuned on the test set.",
    )
    gate_runs: int | None = Field(
        default=None, description="Finished gate runs (in the list and on one change; not after an action)."
    )


async def _view(store: StateStore, change: SpaceChange, gate_runs: int | None = None) -> ChangeView:
    cases = await evalset.cases(store, change.space_id)
    split_of = {cid: s.split for cid, s in (await splits.stored(store, change.space_id)).items()}
    return ChangeView(
        **change.model_dump(),
        steps=describe(change.operations),
        leaks=leaks(added_examples(change.operations), cases, split_of),
        test_targets=[cid for cid in change.target_case_ids if split_of.get(cid) == "test"],
        gate_runs=gate_runs,
    )


@router.get(
    "/{space_id}/changes", response_model=list[ChangeView], responses=ERRORS, summary="Configuration changes"
)
async def list_changes(
    space_id: Annotated[str, Depends(require_space)],
    store: Annotated[StateStore, Depends(get_store)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[ChangeView]:
    runs = await space_changes.gate_runs(space_id, sql=sql, prefix=settings.table_prefix)
    return [
        await _view(store, c, runs.get(c.change_id, 0)) for c in await space_changes.changes(store, space_id)
    ]


@router.post(
    "/{space_id}/changes",
    response_model=ChangeView,
    status_code=201,
    responses=ERRORS,
    summary="Write a change",
)
async def create_change(
    space_id: Annotated[str, Depends(require_space)],
    body: ChangeBody,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    store: Annotated[StateStore, Depends(get_store)],
) -> ChangeView:
    """Checked against the space's configuration as it is now, read with the viewer's token:
    422 if an operation does not fit it (a line that is not there, a table the space lacks)."""
    config = await genie.get_space_config(space_id)
    return await _view(store, await space_changes.create_change(store, space_id, body, viewer.email, config))


@router.get(
    "/{space_id}/changes/{change_id}", response_model=ChangeView, responses=ERRORS, summary="One change"
)
async def get_change(
    space_id: Annotated[str, Depends(require_space)],
    change_id: str,
    store: Annotated[StateStore, Depends(get_store)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChangeView:
    runs = await space_changes.gate_runs(space_id, sql=sql, prefix=settings.table_prefix)
    return await _view(
        store, await space_changes.get_change(store, space_id, change_id), runs.get(change_id, 0)
    )


@router.post(
    "/{space_id}/changes/{change_id}/test",
    response_model=EvalStarted,
    status_code=202,
    responses=ERRORS,
    summary="Run the eval set on the current configuration and on it with this change",
)
async def test_change(
    space_id: Annotated[str, Depends(require_space)],
    change_id: str,
    store: Annotated[StateStore, Depends(get_store)],
    http: Annotated[httpx.AsyncClient, Depends(get_http)],
    app_auth: Annotated[Auth, Depends(get_app_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: ChangeTest | None = None,
) -> EvalStarted:
    change = await space_changes.get_change(store, space_id, change_id)
    if change.status not in ("draft", "tested"):
        raise Conflict(f"The change is {change.status}; only a draft or tested change can be tested.")
    if not settings.evaluate_job_id:
        raise ConfigError("EVALOPS_EVALUATE_JOB_ID is not set.")
    run_id = await JobsClient(http, app_auth).run_now(
        settings.evaluate_job_id,
        {
            "space": space_id,
            "change": change_id,
            "label": change.title,
            "split": (body or ChangeTest()).split,
            "scope": scope_param((body or ChangeTest()).scope),
        },
    )
    return EvalStarted(job_run_id=run_id)


@router.post(
    "/{space_id}/changes/{change_id}/discard",
    response_model=ChangeView,
    responses=ERRORS,
    summary="Discard a change",
)
async def discard_change(
    space_id: Annotated[str, Depends(require_space)],
    change_id: str,
    store: Annotated[StateStore, Depends(get_store)],
) -> ChangeView:
    change = await space_changes.get_change(store, space_id, change_id)
    if change.status == "applied":
        raise Conflict("An applied change is undone by rolling it back, not discarded.")
    return await _view(store, await space_changes.set_status(store, space_id, change_id, "discarded"))


class ApplyBody(BaseModel):
    override: bool = Field(
        default=False,
        description="Apply although the latest test was not accepted (the owner's explicit call).",
    )


@router.post(
    "/{space_id}/changes/{change_id}/apply",
    response_model=ChangeView,
    responses=ERRORS,
    summary="Apply a tested change to the space, with your own permission",
)
async def apply_change(
    space_id: Annotated[str, Depends(require_space)],
    change_id: str,
    body: ApplyBody,
    genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChangeView:
    """Written to the space with the viewer's token, so the space's history shows who did it.
    409 unless the latest test was accepted (or `override`) and the space is still the
    configuration the change was written against."""
    return await _view(
        store,
        await space_changes.apply_change(
            space_id,
            change_id,
            genie=genie,
            sql=sql,
            store=store,
            prefix=settings.table_prefix,
            override=body.override,
        ),
    )


@router.post(
    "/{space_id}/changes/{change_id}/rollback",
    response_model=ChangeView,
    responses=ERRORS,
    summary="Put back the configuration an applied change replaced",
)
async def rollback_change(
    space_id: Annotated[str, Depends(require_space)],
    change_id: str,
    genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ChangeView:
    """409 if the space was edited after the change was applied (rolling back would undo that too)."""
    return await _view(
        store,
        await space_changes.rollback_change(
            space_id, change_id, genie=genie, sql=sql, store=store, prefix=settings.table_prefix
        ),
    )


@router.post(
    "/{space_id}/categories/{category_id}/propose-change",
    response_model=ChangeProposal,
    responses={**ERRORS, 502: {"model": ErrorResponse}},
    summary="Draft a change that should fix a failure category",
)
async def propose_change(
    space_id: Annotated[str, Depends(require_space)],
    category_id: str,
    genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    http: Annotated[httpx.AsyncClient, Depends(get_http)],
    app_auth: Annotated[Auth, Depends(get_app_auth)],
    settings: Annotated[Settings, Depends(get_settings)],
    effective: Annotated[Effective, Depends(get_effective)],
) -> ChangeProposal:
    """An LLM draft from the category's reviewed answers, its eval cases and the configuration as
    it is now. Nothing is saved: the owner edits the draft, saves it as a change and tests it."""
    config = await genie.get_space_config(space_id)
    # The space's own model and settings for the proposer; it runs in the app, so the app's
    # identity needs CAN_QUERY on that endpoint.
    skill = effective.skill("space_change_proposer")
    runner = SkillRunner(http, app_auth, settings.judge_model, max_concurrent=1)
    return await space_changes.propose(
        space_id,
        category_id,
        config=config,
        sql=sql,
        store=store,
        prefix=settings.table_prefix,
        runner=runner,
        skill=skill,
    )
