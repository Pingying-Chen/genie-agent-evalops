"""Input and output models of the LLM skills (skills/<name>/SKILL.md names them by class)."""

from typing import Literal

from pydantic import BaseModel, Field


class RubricJudgeInput(BaseModel):
    rule: str = Field(description="The rule text, as written in the criterion.")
    failure: str = Field(description="What counts as breaking the rule.")
    question: str
    context: str = Field(
        default="", description="Earlier turns of the same conversation (question + report)."
    )
    steps: str = Field(description="The turn's SQL steps, rendered as text (title + SQL).")
    report: str


class RubricJudgeOutput(BaseModel):
    """Field order is the generation order: decide applicability, cite, reason, THEN the verdict.

    A verdict written first turns the reasoning into a justification of a decision already
    made; written last, it is conditioned on the evidence and the reasoning.
    """

    applies_reason: str = Field(description="Why the rule does or does not apply to this question.")
    applies: bool
    quote: str = Field(description="Exact report words the verdict rests on ('' if none).")
    reasoning: str = Field(description="How the quoted evidence meets or breaks the rule.")
    verdict: Literal["pass", "fail", "na"]

    def consistent(self) -> bool:
        """`na` exactly when the rule does not apply."""
        return (self.verdict == "na") == (not self.applies)


class RubricJudgeOutputV2(BaseModel):
    """Superseded v2 shape (verdict first); kept to compare against in experiments."""

    verdict: Literal["pass", "fail", "na"]
    reason: str
    quote: str = ""


class CategorizeInput(BaseModel):
    categories: str = Field(description="Existing categories, one per line: name: description.")
    question: str
    verdict: str = Field(description="The reviewer's task-success verdict: pass or fail.")
    rationale: str = Field(default="", description="The reviewer's reason for the verdict.")
    findings: str = Field(default="", description="What the reviewer noticed, in their words.")


class CategorizeOutput(BaseModel):
    """Reason first, then the name: the choice is conditioned on the comparison, not justified after."""

    reasoning: str = Field(
        description="What went wrong, and which existing category (if any) is the same failure."
    )
    category: str = Field(description="An existing category's exact name, or a short new name.")
    is_new: bool
    description: str = Field(
        description="For a new category, one sentence on what the failure looks like; else ''."
    )


class ExpectationJudgeInput(BaseModel):
    context: str = Field(default="", description="Earlier turns of the conversation (question + report).")
    question: str
    steps: str = Field(description="The turn's SQL steps, rendered as text (title + SQL).")
    report: str
    expectations: str = Field(description="Numbered list, one expectation per line: what the answer must do.")


class ExpectationVerdict(BaseModel):
    """Per expectation, evidence first and the decision last (as in the rubric judge)."""

    number: int = Field(description="The expectation's number in the list.")
    quote: str = Field(description="Exact report words the decision rests on ('' if none).")
    reasoning: str
    met: bool


class ExpectationJudgeOutput(BaseModel):
    verdicts: list[ExpectationVerdict]


class ProposeChangeInput(BaseModel):
    category: str = Field(description="The failure category: name and description.")
    failures: str = Field(
        description="Numbered reviewed answers in the category: question, reviewer reason, findings."
    )
    cases: str = Field(
        default="", description="Eval cases covering the category: questions and expectations."
    )
    instructions: str = Field(description="The space's current text instructions, one per line.")
    sql_examples: str = Field(default="", description="Questions of the space's current SQL examples.")
    tables: str = Field(description="The space's tables, and the columns that already have metadata.")


class ProposedOperation(BaseModel):
    """Flat on purpose (strict JSON output); converted to a typed operation and validated after."""

    op: Literal[
        "add_instruction", "replace_instruction", "remove_instruction", "add_sql_example", "set_column"
    ]
    old: str = Field(description="replace/remove: the current instruction line, copied exactly; else ''.")
    text: str = Field(description="add/replace: the new instruction line; else ''.")
    question: str = Field(description="add_sql_example: the example question; else ''.")
    sql: str = Field(description="add_sql_example: its SQL; else ''.")
    table: str = Field(description="set_column: the table identifier; else ''.")
    column: str = Field(description="set_column: the column name; else ''.")
    description: str = Field(description="set_column: the column description ('' to leave it).")
    synonyms: list[str] = Field(description="set_column: synonyms users say for it ([] to leave them).")


class ProposeChangeOutput(BaseModel):
    """Diagnosis first, then the change: the operations follow from what went wrong."""

    diagnosis: str = Field(description="What in the configuration lets this failure happen.")
    title: str
    rationale: str
    operations: list[ProposedOperation]


class IntentInput(BaseModel):
    intents: str = Field(description="Intents already in use for this agent, one per line (reuse them).")
    questions: str = Field(description="Numbered questions; a follow-up shows the question it follows.")


class IntentTag(BaseModel):
    number: int
    intent: str = Field(
        description="Short lowercase phrase: what is measured, by what, and any kind of filter."
    )


class IntentOutput(BaseModel):
    tags: list[IntentTag]


SCHEMAS: dict[str, type[BaseModel]] = {
    "RubricJudgeInput": RubricJudgeInput,
    "RubricJudgeOutput": RubricJudgeOutput,
    "RubricJudgeOutputV2": RubricJudgeOutputV2,
    "CategorizeInput": CategorizeInput,
    "CategorizeOutput": CategorizeOutput,
    "ExpectationJudgeInput": ExpectationJudgeInput,
    "ExpectationJudgeOutput": ExpectationJudgeOutput,
    "ProposeChangeInput": ProposeChangeInput,
    "ProposeChangeOutput": ProposeChangeOutput,
    "IntentInput": IntentInput,
    "IntentOutput": IntentOutput,
}
