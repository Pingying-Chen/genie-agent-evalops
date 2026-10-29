"""`/api/traces/{trace_id}`: one trace with its steps and check results."""

from typing import Annotated

from fastapi import APIRouter, Depends

from evalops.api.deps import Viewer, get_sql, get_store, get_viewer, get_viewer_genie
from evalops.clients.genie import GenieClient
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.config import Settings, get_settings
from evalops.schemas.api import TraceDetail
from evalops.schemas.common import ErrorResponse
from evalops.services import permissions, traces

router = APIRouter(prefix="/traces", tags=["traces"])


@router.get(
    "/{trace_id}",
    response_model=TraceDetail,
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="One trace, its steps and its verdicts",
)
async def get_trace(
    trace_id: str,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    viewer_genie: Annotated[GenieClient, Depends(get_viewer_genie)],
    sql: Annotated[SqlClient, Depends(get_sql)],
    store: Annotated[StateStore, Depends(get_store)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TraceDetail:
    """Returns 404 both when the trace does not exist and when it belongs to a space the viewer
    does not manage, so a trace's existence never leaks."""
    space_id = await traces.trace_space(trace_id, sql=sql, prefix=settings.table_prefix)
    await permissions.require_allowed(
        viewer.email,
        space_id,
        viewer_genie=viewer_genie,
        store=store,
        ttl_seconds=settings.permission_cache_ttl_seconds,
    )
    return await traces.get_trace(trace_id, space_id, sql=sql, prefix=settings.table_prefix)
