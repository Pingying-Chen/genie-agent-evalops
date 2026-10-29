"""Eval cases: what a consumer asks, and what a good answer must do.

A case is an owner edit, stored in Lakebase with every version kept, so a regression run can
say exactly which version of each case it ran.
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

Behaviour = Literal["answer", "clarify", "refuse"]
CaseStatus = Literal["active", "retired"]
CaseType = Literal["core", "edge", "guardrail"]
CaseOrigin = Literal["written", "production_failure", "production_pass", "probe", "synthetic"]
Split = Literal["dev", "test"]
SplitDimension = Literal["type", "intent", "category", "turns"]
Turn = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Fact = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ToolExpect(BaseModel):
    data: Literal["required", "none", "any"] = Field(
        default="any", description="required = must run SQL; none = must answer without querying data."
    )
    must_not_query: list[str] = Field(
        default_factory=list, description="Columns or tables the answer's SQL must not touch."
    )


class Expect(BaseModel):
    """What the graded turn (the last unless `turn` says otherwise) must do. Every hard rule must
    pass anyway; these add what this question in particular needs."""

    turn: int | None = Field(default=None, ge=0, description="Index of the graded turn; default the last.")
    behaviour: Behaviour = "answer"
    criteria: dict[str, Literal["pass", "na"]] = Field(
        default_factory=dict,
        description="Rules that must apply and pass (`pass`), or must not apply (`na`), on this question.",
    )
    facts: list[Fact] = Field(
        default_factory=list, max_length=20, description="What a correct answer states or does, in words."
    )
    reference_sql: str | None = Field(
        default=None,
        max_length=20000,
        description="A query run live as the tester at ask time; its values must appear in the report.",
    )
    tools: ToolExpect = Field(default_factory=ToolExpect)

    @field_validator("reference_sql")
    @classmethod
    def _one_select(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        sql = value.strip().rstrip(";").strip()
        if ";" in sql or sql.split(None, 1)[0].upper() not in ("SELECT", "WITH"):
            raise ValueError("reference_sql must be one SELECT (or WITH ... SELECT) statement")
        return sql


class CaseSource(BaseModel):
    """Where a case came from, when it was promoted from a reviewed answer."""

    trace_id: str
    conversation_id: str | None = None
    reviewer_verdict: Literal["pass", "fail"] | None = None


class EvalCaseBody(BaseModel):
    """What an owner writes; the server adds the id, version and audit fields."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    turns: list[Turn] = Field(min_length=1, max_length=10, description="Asked in order, in one conversation.")
    expect: Expect = Field(default_factory=Expect)
    category_ids: list[str] = Field(default_factory=list, description="Failure categories it covers.")
    type: CaseType = Field(
        default="core",
        description="core = what users mostly ask; edge = unusual but valid; guardrail = must refuse or "
        "hold back (privacy, out of scope).",
    )
    origin: CaseOrigin = Field(
        default="written",
        description="How the case came about; `synthetic` = made-up data for checking the platform itself.",
    )
    source: CaseSource | None = None
    status: CaseStatus = "active"

    @field_validator("expect")
    @classmethod
    def _turn_in_range(cls, value: Expect, info) -> Expect:
        turns = info.data.get("turns") or []
        if value.turn is not None and turns and value.turn >= len(turns):
            raise ValueError(f"expect.turn {value.turn} is past the last turn ({len(turns) - 1})")
        return value


class EvalCase(EvalCaseBody):
    case_id: str
    space_id: str
    version: int = Field(description="1 on creation, +1 on every edit; old versions are kept.")
    updated_by: str
    updated_at: datetime


class SplitSettings(BaseModel):
    """How a space's cases are split into dev and test."""

    stratify_by: list[SplitDimension] = Field(
        default_factory=lambda: ["type"],
        description="Dimensions whose combinations get the same dev/test share, coarsest first; "
        "empty = a plain random split. Not intent: cases with the same intent stay on one side.",
    )
    test_share: float = Field(default=0.3, ge=0.05, le=0.8)
    min_stratum: int = Field(
        default=4,
        ge=2,
        le=50,
        description="A smaller stratum falls back to a coarser one (drop the last dimension).",
    )

    @field_validator("stratify_by")
    @classmethod
    def _unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("a dimension is listed twice")
        # Intent groups near-identical questions, which must stay on one side (no leak between dev
        # and test), so it cannot also be spread across both; settings saved before keep loading.
        return [d for d in value if d != "intent"]


class CaseSplit(BaseModel):
    case_id: str
    split: Split
    stratum: str = Field(description="The stratum it was assigned in, e.g. `type=core / intent=ranking`.")
    assigned_by: str = Field(description="`auto`, or the owner who overrode it.")
    assigned_at: datetime


class StratumMix(BaseModel):
    """Active cases per value of one dimension, in each split."""

    dimension: SplitDimension
    value: str
    dev: int
    test: int
    unassigned: int


class SplitSummary(BaseModel):
    dev: int
    test: int
    unassigned: int = Field(description="Active cases not assigned yet: assigned at the next run.")
    mix: list[StratumMix]
    warnings: list[str]


class SplitOverride(BaseModel):
    split: Split | None = Field(description="None hands the case back to automatic assignment.")


class SplitAssign(BaseModel):
    reset: bool = Field(
        default=False,
        description="Clear every automatic assignment first (overrides stay). Cases that move were "
        "seen as dev or test before, so do this before the first gate run, not after.",
    )


class CategoryCoverage(BaseModel):
    category_id: str
    name: str
    priority: int | None = None
    active_cases: int


class EvalSet(BaseModel):
    """`GET /spaces/{space_id}/evalset`: the current version of every case, and which failure
    categories have none (they come first)."""

    space_id: str
    cases: list[EvalCase]
    coverage: list[CategoryCoverage]
    splits: list[CaseSplit] = Field(default_factory=list, description="Assigned cases only.")
    split_settings: SplitSettings = Field(default_factory=SplitSettings)
    split_summary: SplitSummary | None = None


class AskedQuestion(BaseModel):
    trace_id: str
    question: str
    asked_at: datetime | None = None


class AddSuggestion(BaseModel):
    """Users keep asking this and no active case covers it."""

    intent: str
    count: int = Field(description="Production questions with this intent in the window.")
    examples: list[AskedQuestion] = Field(
        description="The latest few, to promote one with 'Add to eval set'."
    )


class RetireSuggestion(BaseModel):
    """Nobody asked anything like this case in the window."""

    case_id: str
    title: str
    intent: str


class FlakySuggestion(BaseModel):
    """Split verdicts on the current configuration in several runs: it measures Genie's noise."""

    case_id: str
    title: str
    flaky_runs: int
    runs: int


class DuplicateSuggestion(BaseModel):
    intent: str
    case_ids: list[str]
    titles: list[str]


class Leak(BaseModel):
    """An eval case turn that matches a SQL example: Genie is shown the answer it is tested on."""

    case_id: str
    title: str
    turn: int = Field(description="Which turn of the case (0 = the first).")
    case_question: str
    example_question: str
    split: Split | None = Field(
        default=None,
        description="test = a real leak; dev = allowed (few-shot examples may come from dev), listed only.",
    )


class EvalSetSuggestions(BaseModel):
    """`GET /spaces/{space_id}/evalset/suggestions`. Suggestions only."""

    space_id: str
    window_days: int
    tagged_questions: int = Field(description="Production questions in the window that have an intent.")
    untagged_questions: int = Field(description="Not tagged yet: the review job tags them on its next run.")
    add: list[AddSuggestion]
    retire: list[RetireSuggestion]
    flaky: list[FlakySuggestion]
    duplicates: list[DuplicateSuggestion]
    leaks: list[Leak] = Field(
        default_factory=list,
        description="Cases whose question is already a SQL example of the space (as of the last collect).",
    )
