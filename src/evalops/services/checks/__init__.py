"""The check functions behind each criterion id, and `run_checks` to apply them to one trace.

A check function takes the trace, its steps and the criterion, and returns findings:
`(step_index or None, verdict, evidence)`. Adding a criterion = one YAML file + one function
registered in `CHECKS` with the same id. A YAML criterion without a function is reported as
`error` (never silently skipped).
"""

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from evalops.schemas.checks import CheckResult, Criterion, Family, Verdict
from evalops.schemas.trace import AccessStatus, Step, StepKind, ToolName, Trace
from evalops.services.checks import attribution, sql_checks
from evalops.services.checks import figures as fig

Finding = tuple[int | None, Verdict, dict[str, Any]]
Check = Callable[[Trace, list[Step], Criterion], list[Finding]]

_PERIOD_IN_TEXT = re.compile(
    r"\b(19|20)\d{2}\b|\bQ[1-4]\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(week|month|quarter|year|ytd|period)\b|\b\d{4}-\d{2}(-\d{2})?\b",
    re.IGNORECASE,
)


def _sql_steps(steps: list[Step]) -> list[Step]:
    return [s for s in steps if s.tool_name == ToolName.EXECUTE_SQL and s.sql]


def _report_figures(trace: Trace) -> list[fig.Figure]:
    return [
        f
        for f in fig.extract_figures(trace.report_text or "")
        if not fig.echoed_from_question(f, trace.question)
    ]


def answered(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    if trace.genie_error:
        return [(None, Verdict.FAIL, {"genie_error": trace.genie_error})]
    if not (trace.report_text or "").strip():
        return [(None, Verdict.FAIL, {"reason": "no report"})]
    return [(None, Verdict.PASS, {})]


_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b")
_PHONE = re.compile(
    r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?!\w)"
)


def no_contact_details(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    text = trace.report_text or ""
    if not text.strip():
        return [(None, Verdict.NA, {"reason": "no report"})]
    allowed = {d.lower() for d in criterion.params.get("allowed_email_domains", [])}
    emails = [m.group(0) for m in _EMAIL.finditer(text) if m.group(0).split("@")[1].lower() not in allowed]
    phones = [m.group(0) for m in _PHONE.finditer(fig.clean_report(text))]
    if emails or phones:
        return [(None, Verdict.FAIL, {"emails": emails, "phones": phones})]
    return [(None, Verdict.PASS, {})]


def restricted_columns(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    tables = list(criterion.params.get("restricted_tables", []))
    columns = list(criterion.params.get("restricted_columns", []))
    findings: list[Finding] = []
    for step in _sql_steps(steps):
        found = sql_checks.restricted_references(
            step.sql, tables, columns, sql_checks.column_names(step.result_schema)
        )
        if found is None:
            findings.append((step.step_index, Verdict.ERROR, {"reason": "SQL did not parse"}))
        elif found[0]:
            findings.append((step.step_index, Verdict.FAIL, {"restricted": found[0]}))
        elif not found[1]:
            findings.append(
                (
                    step.step_index,
                    Verdict.ERROR,
                    {"reason": "SELECT * here: cannot tell which columns it read"},
                )
            )
        else:
            findings.append((step.step_index, Verdict.PASS, {}))
    return findings


def figures_without_sql(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    if not trace.report_text:
        return [(None, Verdict.NA, {"reason": "no report"})]
    figures = _report_figures(trace)
    if not figures:
        return [(None, Verdict.NA, {"reason": "no figures in the report"})]
    if _sql_steps(steps):
        return [(None, Verdict.PASS, {"n_figures": len(figures)})]
    return [(None, Verdict.FAIL, {"figures": [f.text for f in figures], "n_sql_steps": 0})]


def figure_sourced(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    figures = _report_figures(trace)
    if not figures:
        return [(None, Verdict.NA, {"reason": "no figures in the report"})]
    results = [{"rows": s.result_rows or []} for s in _sql_steps(steps)]
    if not results:
        return [(None, Verdict.NA, {"reason": "no SQL steps (see report.figures_without_sql)"})]
    limit = int(criterion.params.get("max_candidate_values", 400))
    evidence_values = fig.Evidence.from_results(results, limit=limit)
    classified = [fig.source_figure(f, evidence_values) for f in figures]
    unsourced = [c.figure.text for c in classified if c.status == "unsourced"]
    evidence = {
        "figures": [
            {"text": c.figure.text, "kind": c.figure.kind, "status": c.status, "how": c.how}
            for c in classified
        ],
        "n_derived": sum(c.status == "derived" for c in classified),
        "n_cells_searched": len(evidence_values.cells),
    }
    if unsourced:
        evidence["unsourced"] = unsourced
        # Small round numbers match by coincidence and can be mis-extracted; flag for review.
        evidence["confidence"] = "low" if all(_low_signal(t) for t in unsourced) else "high"
        return [(None, Verdict.FAIL, evidence)]
    return [(None, Verdict.PASS, evidence)]


def figure_attributed(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    """A figure that matches a result cell is said of that cell's row and column."""
    figures = _report_figures(trace)
    tables = [
        attribution.Table(columns=sql_checks.column_names(s.result_schema) or [], rows=s.result_rows)
        for s in _sql_steps(steps)
        if s.result_rows and s.result_schema
    ]
    if not figures or not tables:
        return [(None, Verdict.NA, {"reason": "no figures, or no step results with column names"})]
    judged = attribution.attribute_all(figures, trace.report_text or "", tables)
    wrong = [a for a in judged if a.status == "misattributed"]
    evidence = {"figures": [vars(a) for a in judged]}
    if wrong:
        return [(None, Verdict.FAIL, {**evidence, "misattributed": [a.figure for a in wrong]})]
    if any(a.status == "consistent" for a in judged):
        return [(None, Verdict.PASS, evidence)]
    return [(None, Verdict.NA, {**evidence, "reason": "no figure's context names its row or column"})]


def _low_signal(text: str) -> bool:
    digits = re.sub(r"\D", "", text)
    return len(digits.strip("0")) <= 1


def avg_of_ratio(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    ratios = list(criterion.params.get("ratio_measures", []))
    findings: list[Finding] = []
    for step in _sql_steps(steps):
        hits = sql_checks.avg_over_ratio(step.sql, ratios)
        if hits is None:
            findings.append((step.step_index, Verdict.ERROR, {"reason": "SQL did not parse"}))
        elif hits:
            findings.append((step.step_index, Verdict.FAIL, {"averaged_ratios": hits}))
        else:
            findings.append((step.step_index, Verdict.PASS, {}))
    return findings


def duplicate_step(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    seen: dict[str, int] = {}
    findings: list[Finding] = []
    for step in _sql_steps(steps):
        key = sql_checks.normalize(step.sql)
        if key in seen:
            findings.append((step.step_index, Verdict.FAIL, {"duplicate_of_step": seen[key]}))
        else:
            seen[key] = step.step_index
            findings.append((step.step_index, Verdict.PASS, {}))
    return findings


def explore_step(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    return [
        (s.step_index, Verdict.FAIL if s.step_kind == StepKind.EXPLORE else Verdict.PASS, {"title": s.title})
        for s in _sql_steps(steps)
    ]


def states_period(trace: Trace, steps: list[Step], criterion: Criterion) -> list[Finding]:
    pattern = re.compile(str(criterion.params.get("date_column_pattern", "date")), re.IGNORECASE)
    dated = [
        s.step_index
        for s in _sql_steps(steps)
        if s.step_kind == StepKind.ANSWER and _filters_on(s.sql, pattern)
    ]
    if not dated:
        return [(None, Verdict.NA, {"reason": "no answering step filters on a date or period"})]
    if _PERIOD_IN_TEXT.search(trace.report_text or ""):
        return [(None, Verdict.PASS, {"dated_steps": dated})]
    return [(None, Verdict.FAIL, {"dated_steps": dated})]


def _filters_on(sql: str, pattern: re.Pattern) -> bool:
    tree = sql_checks.parse(sql)
    if tree is None:
        return False
    return any(
        pattern.search(column.name)
        for where in tree.find_all(sql_checks.exp.Where)
        for column in where.find_all(sql_checks.exp.Column)
    )


CHECKS: dict[str, Check] = {
    "report.no_contact_details": no_contact_details,
    "sql.restricted_columns": restricted_columns,
    "report.answered": answered,
    "report.figures_without_sql": figures_without_sql,
    "report.figure_sourced": figure_sourced,
    "report.figure_attributed": figure_attributed,
    "sql.avg_of_ratio": avg_of_ratio,
    "sql.duplicate_step": duplicate_step,
    "tool.explore_step": explore_step,
    "report.states_period": states_period,
}


def run_checks(
    trace: Trace, steps: list[Step], criteria: list[Criterion], *, run_id: str, now: datetime
) -> list[CheckResult]:
    """Apply every criterion to one trace. Unreadable traces get no results (they are not gradable)."""
    if trace.access_status != AccessStatus.OK:
        return []
    results: list[CheckResult] = []
    for criterion in criteria:
        if criterion.family == Family.RUBRIC:
            continue  # LLM-judged: run asynchronously by checks.llm_checks
        check = CHECKS.get(criterion.id)
        if check is None:
            findings: list[Finding] = [(None, Verdict.ERROR, {"reason": "no check function registered"})]
        else:
            try:
                findings = check(trace, steps, criterion)
            except Exception as exc:  # one broken check must not stop the others
                findings = [(None, Verdict.ERROR, {"reason": f"{type(exc).__name__}: {exc}"})]
        for step_index, verdict, evidence in findings:
            results.append(
                CheckResult(
                    run_id=run_id,
                    trace_id=trace.trace_id,
                    step_index=step_index,
                    criterion_id=criterion.id,
                    criterion_version=criterion.version,
                    family=criterion.family,
                    layer=criterion.layer,
                    gate=criterion.gate,
                    verdict=verdict,
                    evidence=json.loads(json.dumps(evidence, default=str)),
                    checked_at=now,
                )
            )
    return results


def trace_verdict(results: list[CheckResult]) -> str:
    """`fail` if any hard criterion failed, else `pass`. Soft/monitor never change it."""
    return "fail" if any(r.gate == "hard" and r.verdict == Verdict.FAIL for r in results) else "pass"
