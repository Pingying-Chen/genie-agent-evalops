"""`/api/spaces/{space_id}/evalset`: the space's eval cases. Owners only."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from evalops.api.deps import Viewer, get_effective, get_library, get_sql, get_store, get_viewer, require_space
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.schemas.common import ErrorResponse
from evalops.schemas.evalset import (
    EvalCase,
    EvalCaseBody,
    EvalSet,
    EvalSetSuggestions,
    SplitAssign,
    SplitOverride,
    SplitSettings,
)
from evalops.services import drift, evalset, splits
from evalops.services.profiles import Effective, Library

router = APIRouter(prefix="/spaces", tags=["eval set"])

ERRORS = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


class EvalCaseUpdate(EvalCaseBody):
    base_version: int  # the version the owner edited: a newer one saved meanwhile is a 409


class ImportResult(BaseModel):
    created: int
    updated: int
    unchanged: int


def _criteria(effective: Effective) -> set[str]:
    """The rules that apply to this space (its profile over the library)."""
    return {c.id for c in effective.criteria}


@router.get("/{space_id}/evalset", response_model=EvalSet, responses=ERRORS, summary="The eval set")
async def get_evalset(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalSet:
    """The current version of every case, and per failure category how many active cases cover it."""
    return await evalset.evalset(space_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.post(
    "/{space_id}/evalset", response_model=EvalCase, status_code=201, responses=ERRORS, summary="Add a case"
)
async def create_case(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    body: EvalCaseBody,
    request: Request,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
) -> EvalCase:
    """422 if `expect.criteria` names a rule that is not loaded or `category_ids` one not in the space."""
    return await evalset.create_case(store, space_id, body, viewer.email, _criteria(effective))


@router.get(
    "/{space_id}/evalset/draft",
    response_model=EvalCaseBody,
    responses=ERRORS,
    summary="A case prefilled from a collected answer",
)
async def draft_case(
    space_id: Annotated[str, Depends(require_space)],
    trace_id: str,
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalCaseBody:
    """The conversation's questions up to that answer, the rules people said apply to it, the
    reviewer's reason and the answer's failure categories. Nothing is saved."""
    return await evalset.draft_from_trace(
        space_id, trace_id, sql=sql, store=store, prefix=settings.table_prefix
    )


@router.get(
    "/{space_id}/evalset/suggestions",
    response_model=EvalSetSuggestions,
    responses=ERRORS,
    summary="What to add to or retire from the eval set",
)
async def suggest_changes(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalSetSuggestions:
    """From production questions' intents (tagged by the review job) against the cases': intents
    users ask that no case covers, cases nobody's questions match, flaky and duplicate cases."""
    return await drift.suggestions(
        space_id,
        sql=sql,
        store=store,
        prefix=settings.table_prefix,
        window_days=effective.thresholds["drift_window_days"],
        min_questions=effective.thresholds["drift_min_questions"],
    )


@router.get(
    "/{space_id}/evalset/export",
    response_class=PlainTextResponse,
    responses={**ERRORS, 200: {"content": {"application/yaml": {}}}},
    summary="The eval set as YAML",
)
async def export_evalset(
    space_id: Annotated[str, Depends(require_space)],
    store: Annotated[StateStore, Depends(get_store)],
) -> PlainTextResponse:
    """The current version of every case, readable back by the import."""
    text = evalset.export_yaml(await evalset.cases(store, space_id))
    return PlainTextResponse(text, media_type="application/yaml")


@router.post(
    "/{space_id}/evalset/import",
    response_model=ImportResult,
    responses=ERRORS,
    summary="Import YAML",
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/yaml": {"schema": {"type": "string"}}}}
    },
)
async def import_evalset(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    request: Request,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
) -> ImportResult:
    """The body is the YAML file as the export writes it. New cases are created, changed ones
    get a new version, the rest are left alone. The whole file is validated before anything is saved."""
    text = (await request.body()).decode("utf-8", errors="replace")
    counts = await evalset.import_yaml(store, space_id, text, viewer.email, _criteria(effective))
    return ImportResult(**counts)


@router.put(
    "/{space_id}/evalset/split-settings",
    response_model=SplitSettings,
    responses=ERRORS,
    summary="How cases are split into dev and test",
)
async def put_split_settings(
    space_id: Annotated[str, Depends(require_space)],
    body: SplitSettings,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
    library: Annotated[Library, Depends(get_library)],
) -> SplitSettings:
    """Applies to cases assigned from now on; existing splits stay until a re-split. Saved as a
    new version of the space's profile."""
    return await splits.put_settings(store, library, space_id, body, viewer.email)


@router.post(
    "/{space_id}/evalset/split", response_model=EvalSet, responses=ERRORS, summary="Assign dev/test splits"
)
async def assign_splits(
    space_id: Annotated[str, Depends(require_space)],
    body: SplitAssign,
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalSet:
    """Assign every active case without a split (runs do this too); with `reset`, re-split all
    automatically assigned cases first."""
    if body.reset:
        await splits.reset(store, space_id)
    await splits.ensure_assigned(
        space_id,
        await evalset.cases(store, space_id),
        sql=sql,
        store=store,
        prefix=settings.table_prefix,
        now=datetime.now(UTC),
    )
    return await evalset.evalset(space_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.put(
    "/{space_id}/evalset/{case_id}/split",
    response_model=EvalSet,
    responses=ERRORS,
    summary="Put one case in dev or test",
)
async def override_split(
    space_id: Annotated[str, Depends(require_space)],
    case_id: str,
    body: SplitOverride,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> EvalSet:
    """The owner's call, kept through re-splits; `split: null` hands the case back to automatic assignment."""
    cases = await evalset.cases(store, space_id)
    await splits.override(store, space_id, cases, case_id, body.split, viewer.email, datetime.now(UTC))
    return await evalset.evalset(space_id, sql=sql, store=store, prefix=settings.table_prefix)


@router.put("/{space_id}/evalset/{case_id}", response_model=EvalCase, responses=ERRORS, summary="Edit a case")
async def update_case(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    case_id: str,
    body: EvalCaseUpdate,
    request: Request,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
) -> EvalCase:
    """Saves a new version (retire a case by saving it with `status: retired`). 409 if someone
    saved a newer version since `base_version`."""
    data = EvalCaseBody.model_validate(body.model_dump(exclude={"base_version"}))
    return await evalset.update_case(
        store, space_id, case_id, data, body.base_version, viewer.email, _criteria(effective)
    )


@router.get(
    "/{space_id}/evalset/{case_id}/history",
    response_model=list[EvalCase],
    responses=ERRORS,
    summary="Every version of a case",
)
async def case_history(
    space_id: Annotated[str, Depends(require_space)],
    case_id: str,
    store: Annotated[StateStore, Depends(get_store)],
) -> list[EvalCase]:
    return await evalset.history(store, space_id, case_id)
