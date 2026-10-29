"""Criteria (loaded from `criteria/*.yaml`) and the results of running them."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Family(StrEnum):
    """Method family; families 1-4 need no human calibration. Rubric judges come last."""

    REFERENCE = "reference"
    PROGRAMMATIC = "programmatic"
    FAITHFULNESS = "faithfulness"
    METAMORPHIC = "metamorphic"
    PAIRWISE = "pairwise"
    IMPLICIT = "implicit"
    RUBRIC = "rubric"


class Layer(StrEnum):
    OUTCOME = "outcome"  # the report
    PROCESS = "process"  # each step, including tool selection
    BEHAVIOUR = "behaviour"  # clarify / refuse


class Gate(StrEnum):
    """`hard` failures fail the trace; `soft` and `monitor` are reported separately."""

    HARD = "hard"
    SOFT = "soft"
    MONITOR = "monitor"


class Verdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NA = "na"  # criterion does not apply to this trace/step
    ERROR = "error"  # the check itself crashed


class AppliesTo(BaseModel):
    level: Literal["step", "report"]
    tool: str | None = Field(default=None, description="For step-level criteria, e.g. `execute_sql`.")


class Measurement(BaseModel):
    """How well a code criterion's detector was measured against hand labels.
    Metadata: it does not change what the criterion checks."""

    false_alarms: int = Field(ge=0, description="Correct items it failed.")
    negatives: int = Field(ge=1, description="Correct items it was run on.")
    caught: int = Field(ge=0, description="Real (or injected) errors it failed.")
    positives: int = Field(ge=1, description="Real (or injected) errors it was run on.")
    method: str
    measured_on: str = Field(description="ISO date.")


class Criterion(BaseModel):
    """One criterion as written in `criteria/<id>.yaml`."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$", examples=["report.figure_sourced"])
    title: str | None = Field(default=None, description="Short name for people, shown in the UI.")
    version: int = Field(ge=1)
    family: Family
    layer: Layer
    gate: Gate
    applies_to: AppliesTo
    description: str
    params: dict[str, Any] = {}
    measured: Measurement | None = Field(default=None, description="Code criteria: measured accuracy.")


class CheckResult(BaseModel):
    """One (trace, step-or-report, criterion) verdict."""

    run_id: str
    trace_id: str
    step_index: int | None = Field(default=None, description="Null for report-level criteria.")
    criterion_id: str
    criterion_version: int
    family: Family
    layer: Layer
    gate: Gate
    verdict: Verdict
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description="Why: e.g. `{figure: '19.5%', matched: false, confidence: 'low'}`.",
    )
    checked_at: datetime


class CheckRun(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    space_id: str
    criteria_version: str = Field(description="Content hash of the criteria files this run used.")
    n_traces: int = 0
    status: Literal["running", "succeeded", "failed"]
    deferred: list[str] = Field(
        default_factory=list,
        description="Traces whose judging was over the run's cap: the next run judges them.",
    )
