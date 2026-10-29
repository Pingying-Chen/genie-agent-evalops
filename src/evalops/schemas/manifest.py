"""Version manifests: what exactly produced a run's numbers.

Every check, eval and review run records one: the agent's configuration version, every rule,
prompt and reviewer guideline in force (by content hash, the content itself kept once in
Delta), the judge model, the eval cases and their versions, the settings, and the platform's
git commit. Two runs' manifests can then be diffed: "what changed between these two numbers".
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ArtifactKind = Literal["criterion", "skill", "guideline"]
RunKind = Literal["check", "eval", "review"]


class ArtifactRef(BaseModel):
    """One rule, prompt or guideline, by content hash; its text is in Delta `artifacts`."""

    kind: ArtifactKind
    name: str = Field(description="Criterion id, skill name, or label schema name (e.g. `task_success__v3`).")
    version: str
    hash: str


class Manifest(BaseModel):
    run_id: str
    run_kind: RunKind
    space_id: str
    created_at: datetime
    platform_commit: str | None = None
    config_versions: dict[str, str] = Field(
        default_factory=dict,
        description="Configuration hash per arm (eval), or `live`: the latest snapshot (check, review).",
    )
    criteria_set: str | None = Field(default=None, description="Hash of the whole rule set loaded.")
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    judge_model: str | None = None
    cases: list[dict[str, Any]] = Field(
        default_factory=list, description="Eval runs: `{case_id, version, split}` asked."
    )
    settings: dict[str, Any] = Field(
        default_factory=dict, description="What else shaped the numbers: k, split, split settings, budgets."
    )


class ManifestRow(BaseModel):
    """`GET /spaces/{space_id}/versions`: one run, newest first."""

    run_id: str
    run_kind: RunKind
    created_at: datetime
    label: str | None = None
    platform_commit: str | None = None
    config_versions: dict[str, str]
    criteria_set: str | None = None
    judge_model: str | None = None
    artifacts: int
    cases: int


class DriftFlag(BaseModel):
    """Two scheduled gate runs on an identical manifest whose results differ case by case: the
    agent changed although nothing we control did (Genie's platform or model)."""

    earlier_run: str
    later_run: str
    flipped: list[str] = Field(
        description="Cases that passed k of k in one run and failed k of k in the other."
    )
    note: str = ""


class SpaceVersionRow(BaseModel):
    version: str
    captured_at: datetime
    previous_version: str | None = None


class Versions(BaseModel):
    space_id: str
    runs: list[ManifestRow]
    config_history: list[SpaceVersionRow] = Field(
        description="The agent's configuration versions, newest first."
    )
    drift: list[DriftFlag] = Field(default_factory=list)


class ArtifactChange(BaseModel):
    kind: ArtifactKind
    name: str
    change: Literal["added", "removed", "changed"]
    before_version: str | None = None
    after_version: str | None = None
    before: str | None = Field(default=None, description="Stored text before (changed or removed).")
    after: str | None = None


class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class ManifestDiff(BaseModel):
    """`GET /spaces/{space_id}/versions/diff?a=&b=`: everything that differs between two runs."""

    a: Manifest
    b: Manifest
    fields: list[FieldChange] = Field(description="Commit, configuration, rule set, model, settings.")
    artifacts: list[ArtifactChange]
    cases_added: list[str]
    cases_removed: list[str]
    cases_changed: list[str] = Field(description="Asked in both, at a different version or split.")
