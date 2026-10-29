"""Request and response bodies of the HTTP API.

These are the contract with the React frontend. Swagger at `/docs` is generated from them.
"""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evalops.schemas.checks import CheckResult, Criterion, Gate
from evalops.schemas.review import Agreement
from evalops.schemas.trace import AccessStatus, Step, Trace


class Me(BaseModel):
    """The viewer, as identified by the Databricks Apps proxy headers."""

    email: str
    allowed_space_ids: list[str] = Field(description="Spaces where the viewer holds CAN_MANAGE.")
    review_space_ids: list[str] = Field(
        default_factory=list, description="Spaces whose reviewer group the viewer belongs to (human review)."
    )
    review_titles: dict[str, str] = Field(
        default_factory=dict,
        description="Title per reviewed space (reviewers cannot look spaces up themselves).",
    )
    review_sessions: dict[str, str] = Field(
        default_factory=dict,
        description="Labeling session url per reviewed space, once the review job made one.",
    )


class Coverage(BaseModel):
    """How many traces could be graded, split by why not."""

    total: int
    by_status: dict[AccessStatus, int]


class SpaceCard(BaseModel):
    """One row of `GET /spaces`."""

    space_id: str
    title: str | None
    monitored: bool | None = Field(
        description="True when the collector service principal holds CAN_MANAGE; None when no collect "
        "has checked this space yet (a newly registered space)."
    )
    coverage: Coverage | None = None
    hard_pass_rate: float | None = Field(
        default=None, ge=0, le=1, description="Share of gradable traces with no hard failure."
    )
    last_collected_at: datetime | None = None


class CriterionStat(BaseModel):
    criterion_id: str
    gate: Gate
    n: int
    n_fail: int
    pass_rate: float | None = Field(ge=0, le=1)


class DailyPoint(BaseModel):
    day: date
    n_traces: int
    hard_pass_rate: float | None


class SpaceSummary(BaseModel):
    """`GET /spaces/{space_id}/summary`."""

    space_id: str
    title: str | None = None
    window_from: date
    window_to: date
    coverage: Coverage
    criteria: list[CriterionStat]
    daily: list[DailyPoint]
    judge_sample_rate: float = Field(
        default=1.0, description="Share of new answers the LLM judges read (the space's settings)."
    )


class BlindSpot(BaseModel):
    """An answer a reviewer failed on task success that no check failed: no rule covers it yet."""

    trace_id: str
    question: str | None = None
    task_rationale: str | None = None
    findings: str | None = None


class JudgeRow(Agreement):
    """A judge's agreement with people, and what it says about the judge's own fail rate."""

    observed_applies: int = Field(
        default=0, description="Turns the judge (current verdicts) said the rule applies to."
    )
    observed_fails: int = Field(default=0, description="Of those, turns the judge failed.")
    corrected_fail_rate: float | None = Field(
        default=None,
        description="The judge's fail rate corrected for its measured TPR and TNR (Rogan-Gladen): "
        "(observed + TNR - 1) / (TPR + TNR - 1), clipped to 0-1. None until both are measured "
        "and the judge beats chance (TPR + TNR > 1).",
    )


class JudgeCalibration(BaseModel):
    """`GET /spaces/{space_id}/calibration`: how far human review has got, and how well each
    LLM judge agrees with it. Owners only: reviewers must not see judge verdicts (blind review)."""

    space_id: str
    min_lower_bound: float = Field(description="Both Wilson lower bounds must reach this to calibrate.")
    queued: int = Field(description="Traces in the current labeling session.")
    labeled: int = Field(description="Of those, traces with at least one label, as of the last review run.")
    computed_at: datetime | None = Field(default=None, description="When agreement was last computed.")
    reviewed: int = Field(default=0, description="Turns with a task-success verdict from a reviewer.")
    task_failed: int = Field(
        default=0, description="Of those, turns the reviewer said do not answer the question."
    )
    caught: int = Field(
        default=0, description="Of the failed turns, how many at least one check failed (rule coverage)."
    )
    blind_spots: list[BlindSpot] = Field(
        default_factory=list, description="Failed turns no check caught: where new rules come from."
    )
    conversations: int = Field(default=0, description="Conversations with at least one reviewed turn.")
    multi_turn: int = Field(
        default=0, description="Of those, conversations with more than one reviewed turn."
    )
    conversations_strict: int = Field(
        default=0, description="Conversations whose every reviewed turn passed (an earlier failure counts)."
    )
    conversations_outcome: int = Field(
        default=0,
        description="Conversations whose last reviewed turn passed (the user got there in the end).",
    )
    judges: list[JudgeRow] = Field(description="One row per (criterion, criterion version, judge).")


class Suggestion(BaseModel):
    """An LLM's suggested category for an untagged observation; the owner accepts or ignores it."""

    category: str
    category_id: str | None = Field(default=None, description="The existing category of that name, if any.")
    description: str = Field(default="", description="For a new category: what the failure looks like.")
    reason: str = ""


class Observation(BaseModel):
    """A reviewed turn that a reviewer failed or wrote findings on: the raw material of failure
    categories. The latest review across reviewers."""

    trace_id: str
    question: str | None = None
    turn_index: int = Field(default=0, description="0 = the first turn of its conversation.")
    asked_at: datetime | None = None
    task_success: Literal["pass", "fail"] | None = None
    task_rationale: str | None = None
    findings: str | None = None
    reviewer: str
    caught_by: list[str] = Field(
        default_factory=list, description="Criteria that fail this turn now (its current verdicts)."
    )
    category_ids: list[str] = Field(default_factory=list, description="Failure categories the owner tagged.")
    suggestion: Suggestion | None = Field(default=None, description="Only while the observation is untagged.")


Impact = Annotated[
    int,
    Field(
        ge=1,
        le=5,
        description="5 = harm (privacy, safety, compliance); 4 = a wrong answer the user would act "
        "on; 3 = wrong or incomplete, likely noticed; 2 = extra effort (rephrase, follow-up); "
        "1 = minor annoyance.",
    ),
]


class FailureCategory(BaseModel):
    """A kind of failure an owner named while reading observations."""

    id: str
    name: str
    description: str = ""
    impact: Impact = 3
    criterion_id: str | None = Field(default=None, description="The criterion that now covers it, if any.")
    created_at: datetime
    updated_by: str
    updated_at: datetime


class CategorySummary(FailureCategory):
    """A category with how often it happens and how much of it the criteria catch."""

    turns: int = Field(description="Tagged observations.")
    failed: int = Field(description="Of those, turns a reviewer failed.")
    caught: int = Field(description="Of the tagged turns, how many at least one criterion failed.")
    frequency: float | None = Field(description="turns / reviewed turns; None before any review.")
    frequency_score: int | None = Field(
        description="frequency on a 1-5 scale (see failures.FREQUENCY_BANDS)."
    )
    priority: int | None = Field(description="frequency_score x impact, 1-25: work on the highest first.")
    linked_caught: int | None = Field(
        default=None,
        description="Of the tagged turns, how many the linked criterion failed (None if unlinked).",
    )
    linked_false_alarms: int | None = Field(
        default=None,
        description="Turns reviewers passed that the linked criterion failed (None if unlinked).",
    )


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    impact: Impact = 3


class CategoryUpdate(BaseModel):
    """Only the fields sent change; send `criterion_id: null` to unlink."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=1000)
    impact: Impact | None = None
    criterion_id: str | None = None


class TagsUpdate(BaseModel):
    category_ids: list[str] = Field(description="The observation's categories after the change.")


class Observations(BaseModel):
    """`GET /spaces/{space_id}/observations`, owners only. Failed turns come first, blind spots
    (failed, caught by nothing) before caught ones."""

    space_id: str
    reviewed: int = Field(description="Turns with a task-success verdict: the denominator of frequency.")
    passed: int = Field(
        default=0, description="Of those, turns reviewers passed: where false alarms are counted."
    )
    items: list[Observation]
    categories: list[CategorySummary] = Field(default_factory=list, description="Highest priority first.")


class TraceListItem(BaseModel):
    """One row of the trace list."""

    trace_id: str
    conversation_id: str
    asked_at: datetime | None
    question: str | None
    access_status: AccessStatus
    verdict: Literal["pass", "fail", "not_gradable"]
    failed_criteria: list[str] = []


class TraceDetail(BaseModel):
    """`GET /traces/{trace_id}`: everything needed to show where a trace fails."""

    trace: Trace
    steps: list[Step]
    results: list[CheckResult]


class CollectResponse(BaseModel):
    job_run_id: int = Field(description="Databricks job run id; poll `GET /runs/{job_run_id}`.")


class RunStatus(BaseModel):
    job_run_id: int
    state: Literal["pending", "running", "succeeded", "failed", "canceled"]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    n_traces_new: int | None = None
    n_traces_updated: int | None = None
    error: str | None = None


class CriteriaList(BaseModel):
    version: str = Field(description="Content hash of the loaded criteria files.")
    criteria: list[Criterion]


class CriterionDraft(BaseModel):
    """`GET /spaces/{space_id}/categories/{category_id}/draft`: a criterion file to edit and add
    to the criteria folder. Criteria stay files under review; the app never writes them."""

    criterion_id: str
    filename: str
    yaml: str


Cause = Literal["judge_wrong", "rule_unclear", "label_wrong"]


class Disagreement(BaseModel):
    """A labelled turn where a judge's current verdict differs from the reviewer's label, for
    the same criterion version. Owners only."""

    trace_id: str
    question: str | None = None
    criterion_id: str
    criterion_version: int
    reviewer: str
    label: Literal["pass", "fail", "na"]
    rationale: str | None = None
    verdict: Literal["pass", "fail", "na"]
    judge: str = Field(description="`<skill>@<version>|<model>|<sampling settings>`.")
    judge_reason: str = ""
    votes: list[str] = Field(default_factory=list)
    kind: Literal["missed_failure", "false_alarm", "applicability"] = Field(
        description="missed_failure = the reviewer failed it, the judge did not; false_alarm = the "
        "judge failed what the reviewer passed; applicability = they disagree on whether the rule applies."
    )
    cause: Cause | None = Field(default=None, description="The owner's diagnosis, once marked.")
    note: str = ""


class Disagreements(BaseModel):
    """`GET /spaces/{space_id}/disagreements`, missed failures first."""

    space_id: str
    items: list[Disagreement]


class CauseUpdate(BaseModel):
    """`cause: null` clears the diagnosis."""

    cause: Cause | None
    note: str = Field(default="", max_length=1000)


class JudgeWeek(BaseModel):
    """One rubric judge in one week of the agent chart."""

    criterion_id: str
    applies: int = Field(
        description="Turns asked that week the judge said the rule applies to (current verdicts)."
    )
    fails: int = Field(description="Of those, turns it failed.")
    corrected_fail_rate: float | None = Field(
        default=None, description="The fail rate corrected for the judge's measured TPR and TNR."
    )


class AgentWeek(BaseModel):
    """One UTC week (Monday start) of `GET /spaces/{space_id}/performance/agent`, by the week asked."""

    week: date
    answers: int
    graded: int = Field(description="Answers whose conversation could be read (access_status ok).")
    checked: int = Field(description="Graded answers with current verdicts.")
    hard_failed: int = Field(description="Of those, answers a must-pass rule failed.")
    reviewed: int = Field(description="Answers with a task-success verdict from a reviewer (latest review).")
    task_passed: int
    task_low: float | None = Field(default=None, description="Wilson 95% interval of task success, low end.")
    task_high: float | None = Field(
        default=None, description="Wilson 95% interval of task success, high end."
    )
    median_seconds: float | None = Field(default=None, description="Median time from question to answer.")
    p90_seconds: float | None = None
    sql_steps: float | None = Field(default=None, description="Mean SQL steps per graded answer.")
    genie_errors: int = Field(default=0, description="Answers that ended in a Genie error.")
    judges: list[JudgeWeek] = Field(default_factory=list)


class SpaceVersionMark(BaseModel):
    """A configuration change of the space, to read the weekly numbers against."""

    version: str = Field(description="Content hash of the configuration.")
    captured_at: datetime = Field(description="When the collect job first saw it (not when it was edited).")
    week: date
    changes: dict[str, int] = Field(
        default_factory=dict,
        description="Count change per kind (instructions, sql_examples, snippets, tables, column_configs); "
        "empty when only content changed.",
    )


class AgentPerformance(BaseModel):
    """`GET /spaces/{space_id}/performance/agent`: the agent's quality and operations by week,
    every week measured with the traces' current verdicts."""

    space_id: str
    weeks: list[AgentWeek] = Field(
        description="Oldest first, every week of the window, empty weeks included."
    )
    versions: list[SpaceVersionMark] = Field(
        default_factory=list, description="Configuration changes in the window, oldest first."
    )


class JudgeHistoryPoint(BaseModel):
    """A judge's agreement with people as computed by one review run."""

    computed_at: datetime
    n: int
    n_applies: int = 0
    tp: int
    fn: int
    fp: int
    tn: int
    tpr_lb: float | None = None
    tnr_lb: float | None = None
    kappa: float | None = None
    calibrated: bool


class JudgeTrack(BaseModel):
    """One (criterion version, judge): a new judge or rule version starts its own track."""

    criterion_id: str
    criterion_version: int
    judge: str
    points: list[JudgeHistoryPoint] = Field(description="Oldest first, one per review run.")


class JudgeBehaviour(BaseModel):
    """What a rubric judge says in the current verdicts."""

    criterion_id: str
    criterion_version: int
    verdicts: int
    na: int = Field(description="Verdicts saying the rule does not apply.")
    errors: int
    sampled: int = Field(description="Verdicts with a vote among several samples.")
    unanimous: int = Field(description="Of those, verdicts where every sample agreed (vote stability).")
    judged: int = Field(description="Verdicts judged in this run; the rest were reused from an earlier one.")
    tokens_per_verdict: float | None = Field(
        default=None, description="Mean tokens of the verdicts judged in this run."
    )


class JudgePerformance(BaseModel):
    """`GET /spaces/{space_id}/performance/judges`."""

    space_id: str
    min_lower_bound: float
    tracks: list[JudgeTrack]
    latest: list[JudgeBehaviour]


class PairAgreement(BaseModel):
    """Two raters on the labels both gave. Fail is the positive class; `a` plays the reference."""

    n: int = Field(description="Labels both gave.")
    exact: int = Field(description="Of those, labels that are the same.")
    both_fail: int = 0
    only_a_fail: int = 0
    only_b_fail: int = 0
    kappa: float | None = Field(
        default=None, description="Cohen's kappa on fail vs not fail; None until both have used both."
    )


class ReviewerStats(BaseModel):
    """One reviewer's activity, how strict they are, and how often the judges agree with them."""

    reviewer: str
    turns_reviewed: int = Field(description="Turns with a task-success verdict from this reviewer.")
    task_failed: int
    task_fails_with_reason: int
    rule_labels: int
    rule_fails: int
    rule_na: int
    rule_fails_with_reason: int
    last_active: datetime | None = None
    active_7d: int = Field(default=0, description="Turns they reviewed or labelled in the last 7 days.")
    median_turnaround_hours: float | None = Field(
        default=None, description="Median time from a turn being queued to their first verdict on it."
    )
    with_judges: PairAgreement = Field(
        description="Their rule labels against the judges' current verdicts (same rule version, no errors). "
        "Measures the pair, not the reviewer."
    )


class ReviewerPair(BaseModel):
    """Agreement between two reviewers on the turns both reviewed: is the rubric clear?"""

    a: str
    b: str
    task: PairAgreement
    rules: PairAgreement


class ReviewerPerformance(BaseModel):
    """`GET /spaces/{space_id}/performance/reviewers`."""

    space_id: str
    reviewers: list[ReviewerStats]
    pairs: list[ReviewerPair] = Field(description="Every two reviewers with at least one turn in common.")
    task_fail_rate: float | None = Field(default=None, description="The space's, over all reviewers.")
    rule_fail_rate: float | None = Field(default=None, description="Of rule labels saying the rule applies.")
