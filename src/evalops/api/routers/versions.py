"""`/api/spaces/{space_id}/versions`: what produced each run's numbers. Owners
only."""

from typing import Annotated

from fastapi import APIRouter, Depends

from evalops.api.deps import get_sql, require_space
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.schemas.common import ErrorResponse
from evalops.schemas.manifest import ManifestDiff, Versions
from evalops.services import versions

router = APIRouter(prefix="/spaces", tags=["versions"])

ERRORS = {401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}}


@router.get(
    "/{space_id}/versions", response_model=Versions, responses=ERRORS, summary="Runs and their versions"
)
async def list_versions(
    space_id: Annotated[str, Depends(require_space)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Versions:
    """The latest 50 check, eval and review runs with what produced them, the agent's
    configuration history, and runs where the agent changed although nothing we control did."""
    return await versions.versions(space_id, sql=sql, prefix=settings.table_prefix)


@router.get(
    "/{space_id}/versions/diff",
    response_model=ManifestDiff,
    responses=ERRORS,
    summary="What changed between two runs",
)
async def diff_versions(
    space_id: Annotated[str, Depends(require_space)],
    a: str,
    b: str,
    sql: Annotated[SqlClient, Depends(get_sql)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ManifestDiff:
    """Every field, rule, prompt, guideline and case that differs from run `a` to run `b`, with
    the texts before and after. 404 unless both runs belong to the space."""
    return await versions.diff(space_id, a, b, sql=sql, prefix=settings.table_prefix)
