"""A space's profile: which rules apply and how strictly, which
model and settings each LLM skill uses, the thresholds, the dev/test split and the release gate.

The deployment ships the library (rule definitions, skills, defaults); a profile only says what
differs for one space. Every save is a new version, so any number can be traced back to the
settings that produced it, and an older version can be restored.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from evalops.schemas.checks import Criterion, Family, Gate
from evalops.schemas.evalset import CaseType, SplitSettings


class RuleSetting(BaseModel):
    enabled: bool = True
    gate: Gate | None = Field(default=None, description="None = the library's gate level.")
    params: dict[str, Any] | None = Field(
        default=None,
        description="Code rules only: parameters merged over the library's (e.g. restricted columns). "
        "A rubric rule's wording is changed by adding a custom rule with its own version.",
    )


class SkillSetting(BaseModel):
    model: str | None = Field(default=None, description="Serving endpoint; None = the deployment's.")
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=100, le=16000)
    samples: int | None = Field(default=None, ge=1, le=7, description="More than one = majority vote.")
    reasoning_effort: Literal["low", "medium", "high"] | None = Field(
        default=None, description="For reasoning models that read it from the system prompt (gpt-oss)."
    )
    prompt: str | None = Field(
        default=None, max_length=40000, description="Replaces the library prompt; keep its {placeholders}."
    )
    version: int | None = Field(
        default=None,
        ge=1,
        description="The override's own version, needed with a prompt or sampling change. Raise it on "
        "every such change: calibration is measured per version.",
    )

    def changes_output(self) -> bool:
        """A prompt or sampling change alters what the skill returns, so it needs a version."""
        return any(
            v is not None
            for v in (self.prompt, self.temperature, self.max_tokens, self.samples, self.reasoning_effort)
        )


class Thresholds(BaseModel):
    """None = the deployment's default (its settings)."""

    eval_k: int | None = Field(default=None, ge=1, le=10)
    calibration_min_lower_bound: float | None = Field(default=None, gt=0, le=1)
    review_budget: int | None = Field(default=None, ge=1, le=500)
    expectation_review_budget: int | None = Field(default=None, ge=0, le=200)
    drift_window_days: int | None = Field(default=None, ge=1, le=365)
    drift_min_questions: int | None = Field(default=None, ge=1, le=100)
    judge_sample_rate: float | None = Field(default=None, gt=0, le=1)
    max_judged_per_run: int | None = Field(default=None, ge=1, le=100000)
    recheck_days: int | None = Field(default=None, ge=1, le=365)


class GateConfig(BaseModel):
    """What a gate run must show, beyond "nothing broke and every target is fixed" (always on).
    Each limit is optional; unset = not checked."""

    min_test_pass_rate: float | None = Field(
        default=None, ge=0, le=1, description="Absolute floor: the candidate's test pass^k rate."
    )
    max_drop: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Relative: candidate test pass^k at most this far below baseline.",
    )
    type_floors: dict[CaseType, float] = Field(
        default_factory=dict, description="Per-slice floors: the candidate's pass^k rate per case type."
    )
    guardrail_zero_tolerance: bool = Field(
        default=True, description="Any failed attempt of a guardrail case on the candidate rejects."
    )
    max_over_refusal: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Share of cases expecting an answer that the candidate declined; also never above baseline.",
    )
    max_p95_latency_increase: float | None = Field(
        default=None,
        ge=0,
        le=5,
        description="Candidate p95 time to answer at most this share above baseline.",
    )

    @field_validator("type_floors")
    @classmethod
    def _rates(cls, value: dict) -> dict:
        if any(not 0 <= v <= 1 for v in value.values()):
            raise ValueError("a type floor is a rate between 0 and 1")
        return value


class ProfileBody(BaseModel):
    rules: dict[str, RuleSetting] = Field(default_factory=dict, description="By library rule id.")
    custom_rules: list[Criterion] = Field(
        default_factory=list, description="This space's own LLM-judged rules (family rubric)."
    )
    skills: dict[str, SkillSetting] = Field(default_factory=dict, description="By skill name.")
    thresholds: Thresholds = Field(default_factory=Thresholds)
    split: SplitSettings | None = Field(default=None, description="None = the stored split settings.")
    gate: GateConfig = Field(default_factory=GateConfig)

    @field_validator("custom_rules")
    @classmethod
    def _rubric_only(cls, value: list[Criterion]) -> list[Criterion]:
        ids = [c.id for c in value]
        if len(set(ids)) != len(ids):
            raise ValueError("two custom rules share an id")
        if any(c.family != Family.RUBRIC for c in value):
            raise ValueError(
                "custom rules are LLM-judged rules (family: rubric); code rules come from the library"
            )
        return value


class ProfileSave(ProfileBody):
    base_version: int = Field(ge=0, description="The version edited (0 = none yet); a newer save is a 409.")
    note: str = Field(default="", max_length=500, description="Why: shown in the history.")


class SpaceProfile(ProfileBody):
    space_id: str
    version: int = Field(description="0 = no profile saved yet (library defaults).")
    note: str = ""
    updated_by: str | None = None
    updated_at: datetime | None = None


class EffectiveRule(BaseModel):
    id: str
    title: str | None
    family: str
    gate: str
    source: Literal["library", "override", "custom"]
    enabled: bool


class EffectiveSkill(BaseModel):
    name: str
    ref: str = Field(description="`name@version` or `name@version~override`.")
    model: str
    model_params: dict[str, Any]
    overridden: bool


class EffectiveProfile(BaseModel):
    """`GET /spaces/{id}/profile/effective`: what actually applies to the space."""

    space_id: str
    profile_version: int
    criteria_set: str
    rules: list[EffectiveRule]
    skills: list[EffectiveSkill]
    thresholds: dict[str, Any]
    gate: GateConfig


class ProfileDiff(BaseModel):
    a: int
    b: int
    changes: list[dict[str, Any]] = Field(description="`{path, before, after}` per changed setting.")
