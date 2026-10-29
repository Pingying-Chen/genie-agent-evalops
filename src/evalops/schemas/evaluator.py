"""Evaluators: who or what judges, and whether it has earned
the right to decide a gate (Playbook: Metric = Evaluator + Rubric + Test set + Pass/Fail gate)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from evalops.schemas.checks import Measurement

EvaluatorKind = Literal["code", "llm_judge", "human"]
EvaluatorStatus = Literal["deterministic", "calibrated", "uncalibrated", "human"]


class Calibration(BaseModel):
    """An LLM judge against people, on the labels where the reviewer said the rule applies.
    Positive class = fail (the minority class: what a gate must catch)."""

    computed_at: datetime | None = None
    judge: str | None = Field(
        default=None, description="The judge configuration measured (skill, model, settings)."
    )
    n: int = 0
    tp: int = 0
    fn: int = 0
    fp: int = 0
    tn: int = 0
    tpr: float | None = Field(
        default=None, description="Good answers the judge passed (positive class = pass)."
    )
    tpr_lb: float | None = None
    tnr: float | None = Field(
        default=None, description="Bad answers the judge failed: recall of the fail class."
    )
    tnr_lb: float | None = None
    precision: float | None = Field(
        default=None, description="Fail precision: of the answers the judge failed, those people failed."
    )
    precision_lb: float | None = None
    kappa: float | None = None


class Evaluator(BaseModel):
    id: str = Field(
        description="`criterion:<id>`, `expectation_judge`, `case_check:<kind>` or `human:review`."
    )
    kind: EvaluatorKind
    name: str
    version: str | None = None
    rubric: str = Field(description="What it judges, in words.")
    status: EvaluatorStatus
    can_decide: bool = Field(description="May fail a case in a gate run. Uncalibrated judges only report.")
    gate: str | None = Field(default=None, description="Criteria only: hard, soft or monitor.")
    calibration: Calibration | None = None
    measured: Measurement | None = None
    note: str = ""


class Evaluators(BaseModel):
    """`GET /spaces/{space_id}/evaluators`."""

    space_id: str
    min_lower_bound: float
    evaluators: list[Evaluator]
