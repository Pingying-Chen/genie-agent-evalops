"""`/api/spaces/{space_id}/evaluators`: who or what judges this space's answers, and which of
them may decide a gate. Owners only."""

from typing import Annotated

from fastapi import APIRouter, Depends

from evalops.api.deps import get_effective, get_sql, get_store, require_space
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.schemas.common import ErrorResponse
from evalops.schemas.evaluator import Evaluators
from evalops.services import evaluators
from evalops.services.profiles import Effective

router = APIRouter(prefix="/spaces", tags=["evaluators"])

ERRORS = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get("/{space_id}/evaluators", response_model=Evaluators, responses=ERRORS, summary="Evaluators")
async def list_evaluators(
    space_id: Annotated[str, Depends(require_space)],
    effective: Annotated[Effective, Depends(get_effective)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Evaluators:
    """Every rule, judge, case check and the human reviewers, with each one's calibration or
    measurement and whether it may decide a gate run (the space's own rules and judges)."""
    judge = effective.skill(evaluators.EXPECTATION_JUDGE)
    return await evaluators.evaluators(
        space_id,
        effective.criteria,
        sql=sql,
        store=store,
        prefix=settings.table_prefix,
        judges=effective.judges(),
        min_lower_bound=effective.thresholds["calibration_min_lower_bound"],
        expectation_judge_version=str(judge.version),
    )
