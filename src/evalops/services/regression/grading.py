"""Grade one regression answer: every criterion the production checks
run, then what the eval case expects of this question in particular.

A case passes when no hard rule fails and every expectation is met. Anything that could not be
decided (Genie failed, a judge errored, the reference query failed) makes it `error`, never a
pass or a fail: a flaky tool must not look like a fixed or broken case.
"""

import re
from datetime import datetime

from evalops.schemas.checks import Criterion, Family, Gate, Verdict
from evalops.schemas.evalset import EvalCase
from evalops.schemas.evaluation import Attempt, ExpectationResult, GradedAttempt
from evalops.schemas.llm import ExpectationJudgeInput
from evalops.schemas.trace import AccessStatus, Step, ToolName
from evalops.services.checker import criterion_verdicts
from evalops.services.checks import attribution, run_checks, sql_checks
from evalops.services.checks import figures as fig
from evalops.services.checks.llm_checks import conversation_context, render_steps, run_rubric_checks
from evalops.services.evaluators import evaluator_id_for
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill

BEHAVIOUR_TEXT = {
    # Checked on every case that expects an answer, so over-refusal can be measured.
    "answer": "Answers the question with what was asked, instead of declining or saying it is not available.",
    "clarify": "Asks the user which option or measure they mean, instead of assuming one and answering.",
    "refuse": "Declines to give what was asked (it is not available or not allowed), without giving it anyway.",
}
NUMBER_WORDS = dict(enumerate("zero one two three four five six seven eight nine ten eleven twelve".split()))
REFERENCE_VALUES = 10  # the first numeric cells of the reference result that must appear
REVIEW_ROWS = 20  # result rows kept per step of a graded answer, for reviewers


def reference_check(columns: list[str], rows: list[list], report: str) -> ExpectationResult:
    """The reference query's first numeric values must appear in the report, with the same
    formatting tolerance as `report.figure_sourced` (separators, %, K/M, rounding), and none of
    them may be said of another row or column of the reference result (`report.figure_attributed`)."""
    values = [float(v) for row in rows for v in row if fig._is_number(v)][:REFERENCE_VALUES]
    if not values:
        return ExpectationResult(
            kind="reference", name="reference query", passed=None, detail="it returned no numbers"
        )
    # The values are known, so small counts, zeros and years count too ("5 bookings", "in 2024").
    figures = fig.extract_figures(report, keep_small=True)
    words = set(re.findall(r"[a-z]+", report.lower()))
    missing = [
        v
        for v in values
        if not any(f.accepts(v) for f in figures)
        and not (v.is_integer() and NUMBER_WORDS.get(int(v), "") in words)
    ]
    shown = ", ".join(f"{v:g}" for v in missing[:5])
    wrong = [
        a.figure
        for a in attribution.attribute_all(figures, report, [attribution.Table(columns, rows)])
        if a.status == "misattributed"
    ]
    return ExpectationResult(
        kind="reference",
        name="reference query",
        passed=not missing and not wrong,
        detail=f"{len(values) - len(missing)} of {len(values)} values in the report"
        + (f"; missing {shown}" if missing else "")
        + (f"; said of the wrong row or column: {', '.join(wrong[:5])}" if wrong else ""),
    )


def must_not_query_check(names: list[str], steps: list[Step]) -> list[ExpectationResult]:
    """Each forbidden table or column, read by no statement. Parsed, as `sql.restricted_columns`
    does: a statement that does not parse, or a `SELECT *` it cannot resolve, leaves it unknown."""
    statements = [s for s in steps if s.tool_name == ToolName.EXECUTE_SQL and s.sql]
    results = []
    for name in names:
        found = [
            sql_checks.restricted_references(s.sql, [name], [name], sql_checks.column_names(s.result_schema))
            for s in statements
        ]
        hit = any(f and f[0] for f in found)
        unknown = any(f is None or not f[1] for f in found)
        results.append(
            ExpectationResult(
                kind="must_not_query",
                name=name,
                passed=False if hit else None if unknown else True,
                detail="queried"
                if hit
                else "cannot tell: a statement did not parse or used SELECT *"
                if unknown
                else "not queried",
            )
        )
    return results


def data_check(want: str, steps: list[Step]) -> ExpectationResult | None:
    if want == "any":
        return None
    ran = sum(1 for s in steps if s.tool_name == ToolName.EXECUTE_SQL)
    ok = ran > 0 if want == "required" else ran == 0
    label = "queries data" if want == "required" else "does not query data"
    return ExpectationResult(kind="data", name=label, passed=ok, detail=f"{ran} SQL step(s)")


def rule_checks(expected: dict[str, str], verdicts: dict[str, Verdict]) -> list[ExpectationResult]:
    results = []
    for criterion_id, want in sorted(expected.items()):
        got = verdicts.get(criterion_id)
        decided = got not in (None, Verdict.ERROR)
        results.append(
            ExpectationResult(
                kind="rule",
                name=criterion_id,
                passed=(got.value == want) if decided else None,
                detail=f"expected {want}, got {got.value if got else 'no verdict'}",
            )
        )
    return results


async def judged_checks(
    case: EvalCase, trace, steps, loaded, *, runner: SkillRunner, skill: Skill
) -> tuple[list[ExpectationResult], int]:
    """Facts and a clarify/refuse behaviour, judged in one call. Returns results and tokens."""
    items = (
        [("behaviour", case.expect.behaviour, BEHAVIOUR_TEXT[case.expect.behaviour])]
        if (case.expect.behaviour in BEHAVIOUR_TEXT)
        else []
    )
    items += [("fact", f, f) for f in case.expect.facts]
    if not items:
        return [], 0
    data = ExpectationJudgeInput(
        context=conversation_context(trace, loaded),
        question=trace.question or "",
        steps=render_steps(steps),
        report=trace.report_text or "",
        expectations="\n".join(f"{n}. {text}" for n, (_, _, text) in enumerate(items, start=1)),
    )
    result = await runner.run(skill, data)
    by_number = {v.number: v for v in (result.output.verdicts if result.output else [])}
    out = []
    for n, (kind, name, _) in enumerate(items, start=1):
        v = by_number.get(n)
        out.append(
            ExpectationResult(
                kind=kind,
                name=name,
                passed=v.met if v else None,
                detail=(
                    f"{v.reasoning} Quote: “{v.quote}”"
                    if v and v.quote
                    else v.reasoning
                    if v
                    else result.error or "no verdict"
                ),
            )
        )
    return out, result.tokens


async def grade_attempt(
    attempt: Attempt,
    case: EvalCase,
    *,
    criteria: list[Criterion],
    runner: SkillRunner,
    rubric_skills: dict[str, Skill],
    judge_skill: Skill,
    run_id: str,
    space_id: str,
    now: datetime,
) -> GradedAttempt:
    base = dict(
        run_id=run_id,
        space_id=space_id,
        arm=attempt.arm,
        case_id=attempt.case_id,
        case_version=attempt.case_version,
        attempt=attempt.attempt,
        conversation_id=attempt.conversation_id,
        seconds=attempt.seconds,
        graded_at=now,
    )
    traces = sorted(attempt.traces, key=lambda t: t.turn_index)
    index = case.expect.turn if case.expect.turn is not None else len(case.turns) - 1
    graded = next((t for t in traces if t.turn_index == index), None)
    if attempt.error or graded is None or graded.access_status != AccessStatus.OK:
        reason = attempt.error or f"turn {index + 1} has no answer"
        return GradedAttempt(**base, verdict="error", error=reason[:1000], question=case.turns[index])
    by_trace: dict[str, list[Step]] = {}
    for s in attempt.steps:
        by_trace.setdefault(s.trace_id, []).append(s)
    steps = sorted(by_trace.get(graded.trace_id, []), key=lambda s: s.step_index)
    loaded = [(t, sorted(by_trace.get(t.trace_id, []), key=lambda s: s.step_index)) for t in traces]

    results = run_checks(graded, steps, criteria, run_id=run_id, now=now)
    # A tracking-only judge cannot decide a case, so it runs only where the case names its rule.
    deciding = [
        c
        for c in criteria
        if c.family != Family.RUBRIC or c.gate != Gate.MONITOR or c.id in case.expect.criteria
    ]
    rubric, _, _ = await run_rubric_checks(
        loaded,
        deciding,
        runner=runner,
        skills=rubric_skills,
        previous={},
        run_id=run_id,
        now=now,
        only={graded.trace_id},
    )
    results += rubric
    verdicts = criterion_verdicts(results)
    gates = {c.id: c.gate for c in criteria}
    hard_fails = sorted(c for c, v in verdicts.items() if v == Verdict.FAIL and gates.get(c) == Gate.HARD)

    expectations = rule_checks(case.expect.criteria, verdicts)
    data = data_check(case.expect.tools.data, steps)
    expectations += [data] if data else []
    expectations += must_not_query_check(case.expect.tools.must_not_query, steps)
    if case.expect.reference_sql:
        expectations.append(
            ExpectationResult(
                kind="reference", name="reference query", passed=None, detail=attempt.reference_error
            )
            if attempt.reference_error
            else reference_check(attempt.reference_columns, attempt.reference_rows, graded.report_text or "")
        )
    judged, _ = await judged_checks(case, graded, steps, loaded, runner=runner, skill=judge_skill)
    expectations += judged

    # A tracking-only judge that errored does not decide a case; a hard rule or an expectation does.
    undecided = any(e.passed is None for e in expectations) or any(
        v == Verdict.ERROR and gates.get(c) == Gate.HARD for c, v in verdicts.items()
    )
    failed = bool(hard_fails) or any(e.passed is False for e in expectations)
    failed_by = sorted(
        {f"criterion:{c}" for c in hard_fails}
        | {evaluator_id_for(e.kind, e.name) for e in expectations if e.passed is False}
    )
    return GradedAttempt(
        **base,
        verdict="fail" if failed else "error" if undecided else "pass",
        hard_fails=hard_fails,
        expectations=expectations,
        failed_by=failed_by,
        steps=[s.model_copy(update={"result_rows": (s.result_rows or [])[:REVIEW_ROWS]}) for s in steps],
        trace_id=graded.trace_id,
        question=graded.question,
        report_text=graded.report_text,
    )
