"""Regression runs: what the `evaluate` job (as the collector) hands the
`ask` job (as the tester) in the run folder, what comes back, and how each answer is graded.

The two identities never share a token: the collector can read the space's configuration but
not create a space on its data; the tester can do the reverse. Files in a workspace folder they
both can reach are the hand-off.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from evalops.schemas.evalset import Split, SplitDimension
from evalops.schemas.trace import Step, Trace

Arm = Literal["baseline", "candidate"]
RunSplit = Literal["dev", "test", "all"]


class ArmConfig(BaseModel):
    """One configuration to test, copied into its own scratch space."""

    arm: Arm
    config_version: str = Field(description="Content hash of the configuration (SpaceConfig.version).")
    title: str
    warehouse_id: str
    serialized_space: str


class AskCase(BaseModel):
    case_id: str
    version: int
    turns: list[str]
    reference_sql: str | None = None


class AskRequest(BaseModel):
    """`request.json` in the run folder."""

    run_id: str
    space_id: str = Field(description="The production space: the tester must not manage it.")
    arms: list[ArmConfig]
    cases: list[AskCase]
    k: int = Field(ge=1, le=10)
    concurrency: int = Field(default=3, ge=1, le=8)
    forbidden_table: str | None = Field(
        default=None, description="A table consumers cannot read; the tester must not read it either."
    )


class Preflight(BaseModel):
    """The tester checks itself before asking anything: a test run as anyone with more access
    than a consumer can pass while consumers fail."""

    tester: str
    manages_space: bool
    forbidden_readable: bool | None = None
    problems: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class Attempt(BaseModel):
    """One ask of one case on one arm: `attempts/<arm>__<case_id>__<n>.json`."""

    arm: Arm
    case_id: str
    case_version: int
    attempt: int
    conversation_id: str | None = None
    seconds: float = 0
    error: str | None = None
    traces: list[Trace] = Field(default_factory=list, description="One per turn, in order.")
    steps: list[Step] = Field(default_factory=list)
    reference_columns: list[str] = Field(default_factory=list)
    reference_rows: list[list] = Field(default_factory=list)
    reference_error: str | None = None


class AskDone(BaseModel):
    """`done.json`: written last by the tester, so a missing file means the ask job died."""

    attempts: int
    scratch_spaces: list[str]
    deleted: bool
    error: str | None = None


ExpectationKind = Literal["rule", "behaviour", "fact", "reference", "data", "must_not_query"]


class ExpectationResult(BaseModel):
    kind: ExpectationKind
    name: str
    passed: bool | None = Field(description="None when it could not be decided (a judge or query error).")
    detail: str = ""


class GradedAttempt(BaseModel):
    """One row of `eval_results`."""

    run_id: str
    space_id: str
    arm: Arm
    case_id: str
    case_version: int
    attempt: int
    verdict: Literal["pass", "fail", "error"]
    hard_fails: list[str] = Field(default_factory=list)
    expectations: list[ExpectationResult] = Field(default_factory=list)
    failed_by: list[str] = Field(
        default_factory=list,
        description="Evaluators that failed it (`criterion:<id>`, `expectation_judge`, `case_check:<kind>`).",
    )
    trace_id: str | None = None
    conversation_id: str | None = None
    question: str | None = None
    report_text: str | None = None
    seconds: float = 0
    error: str | None = None
    graded_at: datetime
    steps: list[Step] = Field(
        default_factory=list,
        description="The graded turn's steps (result rows cut to 20), so a reviewer can check the answer.",
    )


class ArmOutcome(BaseModel):
    """One case on one arm across its k attempts."""

    passes: int
    fails: int
    errors: int
    pass_all: bool = Field(description="pass^k: every attempt passed (and none errored).")
    pass_any: bool = Field(description="pass@k: at least one attempt passed.")
    flaky: bool = Field(description="Passed some attempts and failed others: Genie's noise, not a verdict.")


class CaseRun(BaseModel):
    case_id: str
    case_version: int
    title: str | None = None
    split: Split | None = Field(default=None, description="The case's split when the run asked it.")
    arms: dict[str, ArmOutcome]
    attempts: list[GradedAttempt]


class ArmSummary(BaseModel):
    arm: Arm
    config_version: str | None = None
    cases: int
    pass_all: int
    pass_any: int
    flaky: int
    errored: int = Field(description="Cases with at least one attempt that could not be graded.")


class EvalRun(BaseModel):
    run_id: str
    space_id: str
    label: str | None = None
    change_id: str | None = Field(default=None, description="The change the candidate arm applied.")
    split: RunSplit = Field(
        default="all",
        description="dev = an iteration run; test = a gate run (test cases plus the change's targets); "
        "all = every active case (runs before splits existed).",
    )
    scope: str | None = Field(
        default=None, description="The part of the eval set a dev run asked; empty = all of it."
    )
    status: str
    k: int
    started_at: datetime
    finished_at: datetime | None = None
    criteria_version: str | None = None
    tester: str | None = None
    preflight: Preflight | None = None
    error: str | None = None
    arms: list[ArmSummary] = Field(default_factory=list)


class EvalRuns(BaseModel):
    """`GET /spaces/{space_id}/evaluations`: newest first."""

    space_id: str
    runs: list[EvalRun]


CaseChange = Literal["fixed", "broken", "still_passes", "still_fails", "flaky", "not_graded", "leaked"]


class CaseComparison(BaseModel):
    case_id: str
    title: str | None = None
    outcome: CaseChange
    target: bool = Field(description="The change was written to fix this case.")
    split: Split | None = None
    example_match: bool = Field(
        default=False,
        description="A dev case whose question the change adds as a SQL example: allowed, but its fix proves little.",
    )
    unconfirmed_by: list[str] = Field(
        default_factory=list,
        description="The outcome rests only on these uncalibrated evaluators, so it does not decide.",
    )


class GateCheck(BaseModel):
    """One line of a gate: what was measured, by whom, against what limit."""

    name: str
    evaluator: str
    baseline: str | None = None
    candidate: str
    threshold: str
    passed: bool | None = Field(description="None when it could not be measured (no such cases).")
    decides: bool = Field(default=True, description="False when it rests on a judge that is not calibrated.")


class Comparison(BaseModel):
    """Baseline vs candidate. Accept = nothing broke and every target case is fixed; reject = a
    case broke or a target still fails; otherwise inconclusive (flaky or ungraded targets).
    Only a gate run's comparison decides; a dev run's is for iterating."""

    verdict: Literal["accept", "reject", "inconclusive"]
    decides: bool = Field(
        default=False, description="True for a gate run: only it can let a change be applied."
    )
    reasons: list[str]
    notes: list[str] = Field(default_factory=list, description="Worth knowing, not part of the verdict.")
    gate: list[GateCheck] = Field(default_factory=list, description="The space's gate, check by check.")
    counts: dict[str, int]
    cases: list[CaseComparison]


class Slice(BaseModel):
    """pass^k of one arm on the cases with one value of one dimension."""

    dimension: Literal["split", "type", "intent", "category", "turns"]
    value: str
    arm: Arm
    cases: int
    pass_all: int
    low: float | None = Field(description="Wilson 95% interval of the pass^k rate.")
    high: float | None
    too_small: bool = Field(description="Fewer cases than the space's minimum stratum: read, do not decide.")


class EvalRunDetail(EvalRun):
    """`GET /spaces/{space_id}/evaluations/{run_id}`."""

    cases: list[CaseRun]
    slices: list[Slice] = Field(default_factory=list)
    comparison: Comparison | None = Field(default=None, description="When the run tested a change.")


class RunScope(BaseModel):
    """Part of the eval set, to iterate faster on a dev run: one slice, or the change's targets.
    A gate run (split test) always asks every test case plus the targets, so its verdict is never
    about a convenient subset. No scope = the whole split."""

    dimension: SplitDimension | None = Field(default=None, description="A slice: this dimension ...")
    value: str | None = Field(default=None, description="... has this value (as Dev and test shows it).")
    targets_only: bool = Field(default=False, description="Only the change's target cases.")

    @model_validator(mode="after")
    def _one_kind(self) -> "RunScope":
        if (self.dimension is None) != (self.value is None):
            raise ValueError("A slice needs both a dimension and a value.")
        if (self.dimension is None) == (not self.targets_only):
            raise ValueError("Choose one: a slice or the change's targets.")
        return self

    def describe(self) -> str:
        """How a run list shows it."""
        return "the change's targets" if self.targets_only else f"{self.dimension} = {self.value}"


def check_scope(split: str, scope: RunScope | None, has_change: bool) -> None:
    """The one rule for every way a run starts (API and job)."""
    if scope and split == "test":
        raise ValueError("A gate run asks every test case; choose part of the eval set only for a dev run.")
    if scope and scope.targets_only and not has_change:
        raise ValueError("Only a change has targets: test the change instead.")


def scope_param(scope: RunScope | None) -> str:
    """The `evaluate` job's `scope` parameter: JSON, or empty for the whole split."""
    return scope.model_dump_json(exclude_defaults=True) if scope else ""


class EvalStart(BaseModel):
    label: str = Field(default="", max_length=200)
    split: RunSplit = "dev"
    scope: RunScope | None = Field(default=None, description="Dev runs only: ask part of the eval set.")

    @model_validator(mode="after")
    def _valid_scope(self) -> "EvalStart":
        check_scope(self.split, self.scope, has_change=False)
        return self


class ChangeTest(BaseModel):
    split: Literal["dev", "test"] = Field(
        default="dev", description="dev = iterate (any number of times); test = the gate run that decides."
    )
    scope: RunScope | None = Field(default=None, description="Dev runs only: ask part of the eval set.")

    @model_validator(mode="after")
    def _valid_scope(self) -> "ChangeTest":
        check_scope(self.split, self.scope, has_change=True)
        return self


class EvalStarted(BaseModel):
    job_run_id: int
