"""`/api/spaces/{space_id}/profile`: the space's rules, models, thresholds and gate, as versions
. Owners only."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from evalops.api.deps import Viewer, get_effective, get_library, get_store, get_viewer, require_space
from evalops.clients.lakebase import StateStore
from evalops.errors import NotFound
from evalops.schemas.common import ErrorResponse
from evalops.schemas.profile import EffectiveProfile, ProfileDiff, ProfileSave, SpaceProfile
from evalops.services import profiles
from evalops.services.profiles import SKILLS, Effective, Library

router = APIRouter(prefix="/spaces", tags=["profile"])

ERRORS = {
    401: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


class LibraryRule(BaseModel):
    id: str
    title: str | None
    family: str
    gate: str
    description: str
    params: dict[str, Any]


class LibrarySkill(BaseModel):
    name: str
    version: int
    purpose: str
    model_params: dict[str, Any]
    template: str


class LibraryView(BaseModel):
    """What the deployment ships, for the profile editor: the defaults a profile changes."""

    default_model: str
    rules: list[LibraryRule]
    skills: list[LibrarySkill]
    thresholds: dict[str, Any]


@router.get(
    "/{space_id}/profile", response_model=SpaceProfile, responses=ERRORS, summary="The space's profile"
)
async def get_profile(
    space_id: Annotated[str, Depends(require_space)], store: Annotated[StateStore, Depends(get_store)]
) -> SpaceProfile:
    """The latest version (version 0 = nothing saved yet: the library defaults apply)."""
    return await profiles.current(store, space_id)


@router.put(
    "/{space_id}/profile", response_model=SpaceProfile, responses=ERRORS, summary="Save a new version"
)
async def save_profile(
    space_id: Annotated[str, Depends(require_space)],
    body: ProfileSave,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
    library: Annotated[Library, Depends(get_library)],
) -> SpaceProfile:
    """409 if someone saved after `base_version`; 422 if a skill or custom rule changed what it
    returns without a new version, or the profile names rules or skills the library lacks."""
    return await profiles.save(store, library, space_id, body, viewer.email)


@router.get(
    "/{space_id}/profile/history",
    response_model=list[SpaceProfile],
    responses=ERRORS,
    summary="Every version",
)
async def profile_history(
    space_id: Annotated[str, Depends(require_space)], store: Annotated[StateStore, Depends(get_store)]
) -> list[SpaceProfile]:
    """Newest first."""
    return await profiles.history(store, space_id)


@router.get(
    "/{space_id}/profile/diff", response_model=ProfileDiff, responses=ERRORS, summary="Compare two versions"
)
async def profile_diff(
    space_id: Annotated[str, Depends(require_space)],
    a: int,
    b: int,
    store: Annotated[StateStore, Depends(get_store)],
) -> ProfileDiff:
    """Every setting that differs from version a to version b (0 = the library defaults)."""
    by_version = {p.version: p for p in await profiles.history(store, space_id)}
    by_version[0] = SpaceProfile(space_id=space_id, version=0)
    if a not in by_version or b not in by_version:
        raise NotFound("No such profile version.")
    return profiles.diff(by_version[a], by_version[b])


@router.post(
    "/{space_id}/profile/restore/{version}",
    response_model=SpaceProfile,
    responses=ERRORS,
    summary="Restore an older version",
)
async def restore_profile(
    space_id: Annotated[str, Depends(require_space)],
    version: int,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
    library: Annotated[Library, Depends(get_library)],
) -> SpaceProfile:
    """Saves that version's settings as a new version: history only grows."""
    return await profiles.restore(store, library, space_id, version, viewer.email)


@router.get(
    "/{space_id}/profile/export",
    response_class=PlainTextResponse,
    responses={**ERRORS, 200: {"content": {"application/yaml": {}}}},
    summary="The profile as YAML",
)
async def export_profile(
    space_id: Annotated[str, Depends(require_space)], store: Annotated[StateStore, Depends(get_store)]
) -> PlainTextResponse:
    """For review in git; the import reads it back."""
    return PlainTextResponse(
        profiles.export_yaml(await profiles.current(store, space_id)), media_type="application/yaml"
    )


@router.post(
    "/{space_id}/profile/import",
    response_model=SpaceProfile,
    responses=ERRORS,
    summary="Save a YAML profile as a new version",
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/yaml": {"schema": {"type": "string"}}}}
    },
)
async def import_profile(
    space_id: Annotated[str, Depends(require_space)],
    request: Request,
    viewer: Annotated[Viewer, Depends(get_viewer)],
    store: Annotated[StateStore, Depends(get_store)],
    library: Annotated[Library, Depends(get_library)],
) -> SpaceProfile:
    """Validated like any save; saved on top of the latest version."""
    body = profiles.parse_yaml((await request.body()).decode("utf-8", errors="replace"))
    latest = await profiles.current(store, space_id)
    save = ProfileSave(**body.model_dump(), base_version=latest.version, note="Imported from YAML")
    return await profiles.save(store, library, space_id, save, viewer.email)


@router.get(
    "/{space_id}/profile/effective",
    response_model=EffectiveProfile,
    responses=ERRORS,
    summary="What applies to the space now",
)
async def effective_profile(
    effective: Annotated[Effective, Depends(get_effective)],
    library: Annotated[Library, Depends(get_library)],
) -> EffectiveProfile:
    """Each rule (library, overridden or custom), each skill with its model and settings, the
    thresholds and the gate."""
    return profiles.effective_view(effective, library)


@router.get(
    "/{space_id}/profile/library", response_model=LibraryView, responses=ERRORS, summary="The defaults"
)
async def library_view(
    space_id: Annotated[str, Depends(require_space)],
    library: Annotated[Library, Depends(get_library)],
) -> LibraryView:
    """The deployment's rules, skills (prompt and settings) and default thresholds."""
    return LibraryView(
        default_model=library.settings.judge_model,
        rules=[
            LibraryRule(
                id=c.id,
                title=c.title,
                family=c.family.value,
                gate=c.gate.value,
                description=c.description.strip(),
                params=c.params,
            )
            for c in library.criteria
        ],
        skills=[
            LibrarySkill(
                name=s.name,
                version=s.version,
                purpose=s.purpose,
                model_params=s.model_params,
                template=s.template,
            )
            for s in (library.skill(n) for n in SKILLS)
        ],
        thresholds={name: getattr(library.settings, name) for name in profiles.THRESHOLDS},
    )
