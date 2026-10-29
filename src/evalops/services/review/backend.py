"""What the review service needs from MLflow, and how a Genie turn is shown to a reviewer.

`ReviewBackend` is a Protocol so the service is tested with an in-memory fake; the real
implementation (`mlflow_backend.MlflowBackend`) is a thin layer over MLflow's labeling API.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from evalops.schemas.checks import Criterion
from evalops.schemas.trace import Step, ToolName, Trace

PREVIEW_ROWS = 20
# Genie cites a step as `\[[1](<link to the asker's conversation>&gra_focus=<call_id>)\]`.
CITATION = re.compile(r"\\?\[?\[(\d+)\]\(([^)]*)\)(?:\\?\])?(\s*#)?")


@dataclass(frozen=True)
class TurnView:
    """One Genie turn as the reviewer sees it: a chat (context, earlier turns, question), the
    report, and one span per tool call (SQL with its first result rows, or a chart's title)."""

    trace_id: str
    space_id: str
    messages: list[dict[str, str]]
    report: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    title: str = ""  # the Review App's item title; by default it shows the first user message


@dataclass(frozen=True)
class Assessment:
    """An assessment on an MLflow trace, human or judge."""

    mlflow_trace_id: str
    name: str
    source_type: str  # HUMAN | LLM_JUDGE | CODE
    source_id: str
    value: Any
    rationale: str | None
    assessment_id: str
    created_ms: int


def readable_citations(report: str, steps: list[Step]) -> str:
    """Replace Genie's citation links with the cited step's title.

    The links open the asker's own Genie conversation, which a reviewer cannot open
    ("does not exist"); the step title names the span holding that SQL. A heading that
    Genie glued to a citation ("\\]### Key Trends") gets its line break back.
    """
    titles = {s.call_id: s.title or f"step {s.step_index + 1}" for s in steps}

    def replace(m: re.Match) -> str:
        call_id = re.search(r"gra_focus=([^&)]+)", m.group(2))
        title = titles.get(call_id.group(1)) if call_id else None
        cited = f"[{m.group(1)}: {title}]" if title else f"[{m.group(1)}]"
        return cited + ("\n\n#" if m.group(3) else "")

    return CITATION.sub(replace, report)


# Bump when what a reviewer sees changes: logged traces cannot be edited, so a new version
# re-logs every queued turn and starts a new session (`service.session_name`).
# 2: earlier turns' steps as context, "answer N" stated, charts name the query they plot.
# 3: a follow-up's item title is its own question ("Turn 2: ..."), not the first turn's.
# 4: the chat is only the reviewed turn, so the Review App title and box show which one.
VIEW_VERSION = "4"


def _span_name(step: Step) -> str:
    return step.title or f"step {step.step_index + 1}"


def _step_spans(steps: list[Step]) -> list[dict[str, Any]]:
    """SQL steps with their first result rows, and charts with the query they plot.

    The API gives a chart's title and its source query but no chart type or axes, so the
    chart span names the SQL span whose rows it plots.
    """
    by_call = {s.call_id: s for s in steps}
    shown: list[dict[str, Any]] = []
    for s in steps:
        if s.tool_name == ToolName.GENERATE_VISUALIZATION:
            title = s.title or "untitled"
            source = by_call.get(s.source_call_id or "")
            outputs: dict[str, Any] = {"chart": title}
            if source is not None:
                outputs["plots the rows of"] = _span_name(source)
            shown.append({"name": f"Chart: {title}", "inputs": {"kind": "viz"}, "outputs": outputs})
            continue
        if s.tool_name != ToolName.EXECUTE_SQL:
            continue
        columns = [c.get("name") for c in s.result_schema or []]
        shown.append(
            {
                "name": _span_name(s),
                "inputs": {"sql": s.sql or "", "kind": s.step_kind.value},
                "outputs": {
                    "columns": columns,
                    "rows": (s.result_rows or [])[:PREVIEW_ROWS],
                    "row_count": s.row_count,
                    **({"error": s.error} if s.error else {}),
                },
            }
        )
    return shown


def _short(text: str | None, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def turn_view(
    trace: Trace,
    steps: list[Step],
    earlier: list[tuple[Trace, list[Step]]],
    *,
    context: str = "",
    asked: str = "",
) -> TurnView:
    """`asked` and `context` (see `review.context`) open the chat as a system message, so the
    reviewer reads when the question was asked and what the space covers before the turn.

    The chat holds only the turn under review: the Review App titles an item by its first user
    message (it ignores the trace's request preview) and draws a follow-up question inside the
    previous answer's box. So a follow-up's message starts with its turn number and lists the
    earlier questions; their answers and steps are context spans in the trace.
    """
    earlier = sorted(earlier, key=lambda pair: pair[0].turn_index)
    header = "\n\n".join(part for part in (asked, context) if part)
    messages: list[dict[str, str]] = [{"role": "system", "content": header}] if header else []
    question = trace.question or ""
    if earlier:
        asked_before = "\n".join(f"Turn {t.turn_index + 1}: {t.question or ''}" for t, _ in earlier)
        question = (
            f"Turn {trace.turn_index + 1}: {question}\n\n"
            "(Follow-up; only this answer is under review. Earlier in the conversation:\n"
            f"{asked_before}\n"
            'Their answers and queries are in the trace under "(context)".)'
        )
    messages.append({"role": "user", "content": question})
    report = readable_citations(trace.report_text or "", steps)
    if earlier:
        shown = [
            {
                "name": f"Turn {t.turn_index + 1} (context): {_short(t.question)}",
                "inputs": {"question": t.question or ""},
                "outputs": {"answer": readable_citations(t.report_text or "", t_steps)},
                "children": _step_spans(t_steps),
            }
            for t, t_steps in earlier
        ]
        shown.append(
            {
                "name": f"Turn {trace.turn_index + 1} (under review): {_short(trace.question)}",
                "inputs": {"question": trace.question or ""},
                "outputs": {"answer": report},
                "children": _step_spans(steps),
            }
        )
    else:
        shown = _step_spans(steps)
    tags = {
        "evalops.trace_id": trace.trace_id,
        "evalops.space_id": trace.space_id,
        "evalops.conversation_id": trace.conversation_id,
        "evalops.view": VIEW_VERSION,
        **({"evalops.asked_at": trace.asked_at.isoformat()} if trace.asked_at else {}),
    }
    title = question.split("\n", 1)[0]
    return TurnView(trace.trace_id, trace.space_id, messages, report, shown, tags, _short(title, 200))


# Asked first, before any rule, so the reviewer judges the whole answer before a rule frames it.
# v2: read the SQL, do not recompute numbers. v3: says which answer is judged in a follow-up.
TASK_SUCCESS = "task_success__v3"
FINDINGS = "findings__v2"  # v2: says which answer, and to type "none"
TASK_SUCCESS_TITLE = "Does the last answer (the one under review) answer its question?"
TASK_SUCCESS_INSTRUCTION = (
    "Only the last answer is under review; earlier turns are context and are reviewed in their "
    "own items. Judge it against what the person meant then, including what earlier turns "
    "settled (period, places, metric).\n\n"
    "pass = it answers what was asked: right metric, filters and time range, or a correct "
    "clarifying question or refusal. fail = it answers a different question, misreads the "
    "request, or you could not use it. No na: decide, and give a reason.\n\n"
    "Check what the SQL means, not its syntax (View full trace): what each step filters, which "
    "metric it computes, its GROUP BY, ORDER BY and LIMIT. Do not re-run queries or recompute "
    "numbers: a check already confirms every number comes from a query result. If a number "
    "looks implausible, say so under findings."
)
FINDINGS_TITLE = "What went wrong in the last answer, or anything notable? (type none if nothing)"
FINDINGS_INSTRUCTION = (
    "Anything you noticed in the last answer, in your own words, including problems the rules "
    "below do not cover. The Review App needs an answer to every question: type none if there "
    "is nothing to add."
)


def label_instruction(criterion: Criterion) -> str:
    """What the reviewer reads next to the label: the rule and what counts as a failure."""
    failure = criterion.params.get("failure", "").strip()
    return (
        f"Rule: {criterion.params.get('rule', '').strip()}\n\n"
        + (f"What counts as a failure:\n{failure}\n\n" if failure else "")
        + "pass = the turn follows the rule; fail = it breaks the rule; "
        "na = the rule does not apply to this question. Always give a reason."
    )


class ReviewBackend(Protocol):
    def ensure_experiment(self, space_id: str) -> str:
        """The space's review experiment id (one experiment per space: reviewers get write
        access to the whole experiment, so spaces must not share one)."""
        ...

    def set_reviewers(self, experiment_id: str, reviewers: list[str]) -> None:
        """Exactly these users may edit the experiment (replaces earlier grants)."""
        ...

    def ensure_label_schemas(self, experiment_id: str, criteria: list[Criterion]) -> list[str]: ...

    def ensure_schema(self, experiment_id: str, name: str, title: str, instruction: str) -> None:
        """One pass/fail label schema with a comment box, created if missing (immutable after)."""
        ...

    def ensure_session(
        self, experiment_id: str, name: str, reviewers: list[str], schemas: list[str]
    ) -> tuple[str, str]:
        """(session id, url) of the named session, created if missing; assignees updated."""
        ...

    def find_traces(self, experiment_id: str) -> dict[str, str]:
        """`{evalops trace_id: mlflow trace id}` already logged in the experiment."""
        ...

    def log_turn(self, experiment_id: str, turn: TurnView) -> str: ...

    def add_to_session(self, experiment_id: str, session_id: str, mlflow_trace_ids: list[str]) -> None: ...

    def assessments(self, experiment_id: str) -> list[Assessment]: ...

    def log_judge(
        self, mlflow_trace_id: str, name: str, value: str, rationale: str, source_id: str
    ) -> None: ...
