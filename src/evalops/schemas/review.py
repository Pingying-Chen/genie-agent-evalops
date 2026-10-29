"""Human review (mechanism A): what is queued for labeling, the labels, and judge-human agreement.

The unit is (trace, rubric criterion) with a label `pass` / `fail` / `na` and a reason.
Human labels do not depend on any judge version, so every new judge version is measured
against the same labels.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class QueueReason(StrEnum):
    """Why a trace was queued. Only `random` gives an unbiased agreement estimate."""

    JUDGE_UNCERTAIN = "judge_uncertain"  # split votes or a judge error: most informative
    JUDGE_FAIL = "judge_fail"  # fills the fail class faster (biased towards judge-fail)
    RANDOM = "random"
    EXPECTATION_CHECK = "expectation_check"  # a regression answer, to calibrate expectation_judge


class ReviewItem(BaseModel):
    """One trace placed in a space's labeling session."""

    space_id: str
    trace_id: str
    mlflow_trace_id: str
    session_id: str
    reason: QueueReason
    queued_at: datetime


class HumanLabel(BaseModel):
    """One reviewer's label for one (trace, criterion), read back from MLflow."""

    space_id: str
    trace_id: str
    mlflow_trace_id: str
    criterion_id: str
    criterion_version: int = Field(
        description="Rule version the reviewer was shown (from the label schema name)."
    )
    reviewer: str
    label: Literal["pass", "fail", "na"]
    rationale: str | None = None
    assessment_id: str
    labeled_at: datetime


class TraceReview(BaseModel):
    """A reviewer's verdict on the whole turn, asked before the rubric rules.

    System-level task success (did the answer solve what was asked?) sits above every
    criterion: the criteria are component-level diagnostics of it. `findings` is free text
    that new criteria are drawn from.
    """

    space_id: str
    trace_id: str
    mlflow_trace_id: str
    reviewer: str
    task_success: Literal["pass", "fail"] | None = None
    task_rationale: str | None = None
    findings: str | None = None
    guideline: str | None = Field(
        default=None, description="The task-success question it was answered under, e.g. `task_success__v3`."
    )
    updated_at: datetime


class Agreement(BaseModel):
    """Judge vs human for one criterion and one judge version. Positive class = `fail`."""

    computed_at: datetime
    space_id: str
    criterion_id: str
    criterion_version: int
    judge: str = Field(description="`<skill>@<version>|<model>|<sampling settings>`.")
    n: int = Field(description="Labels paired with a judge verdict (judge errors excluded).")
    n_judge_error: int
    tp: int
    fn: int
    fp: int
    tn: int
    exact: int = Field(description="Pairs where judge and human gave the same of pass/fail/na.")
    n_applies: int = Field(
        default=0, description="Pairs where the human said the rule applies: what TPR, TNR and kappa count."
    )
    applicability_agree: int = Field(
        default=0, description="Pairs where judge and human agree on whether the rule applies (na or not)."
    )
    tpr: float | None
    tnr: float | None
    tpr_lb: float | None = Field(description="Wilson 95% lower bound.")
    tnr_lb: float | None
    calibrated: bool = Field(description="Both lower bounds reach the threshold.")
    positive_class: str = Field(
        default="pass", description="TPR counts this class: good answers judged pass."
    )
    kappa: float | None = Field(
        default=None,
        description="Cohen's kappa on fail vs not fail: agreement beyond chance. None when undefined "
        "(every pair in one class).",
    )
    n_by_reason: dict[str, int]


class CategorySuggestion(BaseModel):
    """An LLM's suggested failure category for one observation.

    Shown to the owner to accept or ignore; never applied on its own. `input_hash` covers the
    observation, the category list and the skill, so a suggestion is redone when any changes.
    """

    space_id: str
    trace_id: str
    category: str
    is_new: bool
    description: str = ""
    reason: str = ""
    input_hash: str
    skill: str
    model: str
    created_at: datetime
