"""The `check` job: run the criteria over a space's collected traces.

Per space:
1. load the traces to check: on the first run all of them; after that only those collected since
   the last run, those whose judging the last run deferred, and, when the rule set changed, those
   asked in the last `recheck_days` days;
2. `run_checks` on every gradable trace (unreadable ones get no results), and the LLM-judged
   rules on a stable sample of them, at most `max_judged` fresh judgements per run;
3. append the results to the history (`check_results`), make them the traces' current verdicts
   (`current_results`, what the pages read) and record the run;
4. recompute the daily rollup in Lakebase for the days those traces were asked, from the current
   verdicts of every trace on those days, so the UI never scans Delta for charts.
"""

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from evalops.clients.lakebase import StateStore
from evalops.schemas.checks import CheckResult, CheckRun, Criterion, Verdict
from evalops.schemas.trace import AccessStatus, Step, Trace
from evalops.services.checks import run_checks, trace_verdict
from evalops.services.checks.llm_checks import rubric_criteria, run_rubric_checks
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill

HARD_GATE = "_hard_gate"  # rollup row for the per-trace verdict (any hard failure = fail)


class CheckStore(Protocol):
    async def load_traces(
        self,
        space_id: str,
        since: datetime | None = None,
        *,
        collected_since: datetime | None = None,
        trace_ids: list[str] = ...,
        conversation_ids: list[str] = ...,
    ) -> list[tuple[Trace, list[Step]]]: ...

    async def load_current_results(
        self, space_id: str, criterion_ids: list[str], trace_ids: list[str] | None = None
    ) -> list[CheckResult]: ...

    async def last_check(self, space_id: str) -> dict[str, Any] | None: ...

    async def write_check_results(self, results: list[CheckResult]) -> None: ...

    async def replace_current_results(self, space_id: str, run_id: str, trace_ids: list[str]) -> None: ...

    async def rollup_counts(self, space_id: str, days: list[date]) -> list[dict[str, Any]]: ...

    async def write_check_run(self, run: CheckRun) -> None: ...


@dataclass
class CheckStats:
    space_id: str
    n_traces: int = 0
    n_gradable: int = 0
    n_results: int = 0
    n_trace_fail: int = 0
    n_llm_calls: int = 0
    n_deferred: int = 0
    fails_by_criterion: dict[str, int] = field(default_factory=dict)


async def check_space(
    space_id: str,
    *,
    delta: CheckStore,
    store: StateStore,
    criteria: list[Criterion],
    criteria_version: str,
    run_id: str,
    since: datetime | None = None,
    now: datetime | None = None,
    runner: SkillRunner | None = None,
    skills: dict[str, Skill] | None = None,
    sample_rate: float = 1.0,
    max_judged: int | None = None,
    recheck_days: int = 30,
) -> CheckStats:
    """`since` set = re-check every trace asked since then (the job's --since); otherwise incremental."""
    now = now or datetime.now(UTC)
    stats = CheckStats(space_id=space_id)
    loaded, incremental = await _traces_to_check(delta, space_id, since, criteria_version, now, recheck_days)
    targets = [t.trace_id for t, _ in loaded]
    deferred: set[str] = set()
    stats.n_traces = len(loaded)

    by_trace: dict[str, list[CheckResult]] = {}
    for trace, steps in loaded:
        by_trace[trace.trace_id] = run_checks(trace, steps, criteria, run_id=run_id, now=now)

    rubric = rubric_criteria(criteria)
    if rubric and runner is not None and loaded:
        # Earlier turns of the same conversations are the judge's context, not checked again.
        follow_ups = sorted({t.conversation_id for t, _ in loaded if t.turn_index > 0})
        seen = set(targets)
        conversations, prior = await asyncio.gather(
            delta.load_traces(space_id, conversation_ids=follow_ups) if follow_ups else _nothing(),
            delta.load_current_results(space_id, [c.id for c in rubric], targets if incremental else None),
        )
        context = [pair for pair in conversations if pair[0].trace_id not in seen]
        llm_results, stats.n_llm_calls, deferred = await run_rubric_checks(
            loaded + context,
            criteria,
            runner=runner,
            skills=skills or {},
            previous={(r.trace_id, r.criterion_id): r for r in prior},
            run_id=run_id,
            now=now,
            only=seen,
            sample_rate=sample_rate,
            max_judged=max_judged,
        )
        for result in llm_results:
            by_trace[result.trace_id].append(result)

    all_results: list[CheckResult] = []
    per_trace: list[tuple[Trace, list[CheckResult]]] = []
    for trace, _ in loaded:
        results = by_trace[trace.trace_id]
        all_results.extend(results)
        if trace.access_status == AccessStatus.OK:
            per_trace.append((trace, results))

    await delta.write_check_results(all_results)
    await delta.replace_current_results(space_id, run_id, targets)
    await delta.write_check_run(
        CheckRun(
            run_id=run_id,
            space_id=space_id,
            started_at=now,
            finished_at=datetime.now(UTC),
            criteria_version=criteria_version,
            n_traces=len(loaded),
            status="succeeded",
            deferred=sorted(deferred),
        )
    )
    days = sorted({(t.asked_at or t.collected_at).date() for t, _ in per_trace})
    await store.put_daily_rollup(daily_rollup(space_id, await delta.rollup_counts(space_id, days), criteria))

    stats.n_gradable = len(per_trace)
    stats.n_results = len(all_results)
    stats.n_trace_fail = sum(trace_verdict(r) == "fail" for _, r in per_trace)
    stats.n_deferred = len(deferred)
    for _, results in per_trace:
        for criterion_id, verdict in criterion_verdicts(results).items():
            if verdict == Verdict.FAIL:
                stats.fails_by_criterion[criterion_id] = stats.fails_by_criterion.get(criterion_id, 0) + 1
    return stats


async def _traces_to_check(
    delta: CheckStore,
    space_id: str,
    since: datetime | None,
    criteria_version: str,
    now: datetime,
    recheck_days: int,
) -> tuple[list[tuple[Trace, list[Step]]], bool]:
    """The traces this run checks, and whether that is only part of the space (incremental)."""
    last = None if since else await delta.last_check(space_id)
    if last is None:
        return await delta.load_traces(space_id, since), False
    changed = last["criteria_version"] != criteria_version
    loaded = await delta.load_traces(
        space_id,
        now - timedelta(days=recheck_days) if changed else None,
        collected_since=last["started_at"],
        trace_ids=last["deferred"],
    )
    return loaded, True


async def _nothing() -> list:
    return []


def criterion_verdicts(results: list[CheckResult]) -> dict[str, Verdict]:
    """One verdict per criterion for a trace: any step failing fails it; all-NA stays NA."""
    by_criterion: dict[str, list[Verdict]] = defaultdict(list)
    for r in results:
        by_criterion[r.criterion_id].append(r.verdict)
    out: dict[str, Verdict] = {}
    for criterion_id, verdicts in by_criterion.items():
        if Verdict.FAIL in verdicts:
            out[criterion_id] = Verdict.FAIL
        elif Verdict.ERROR in verdicts:
            out[criterion_id] = Verdict.ERROR
        elif Verdict.PASS in verdicts:
            out[criterion_id] = Verdict.PASS
        else:
            out[criterion_id] = Verdict.NA
    return out


def daily_rollup(space_id: str, counts: list[dict[str, Any]], criteria: list[Criterion]) -> list[dict]:
    """Rows `{space_id, day, criterion_id, n, n_fail, n_na}` at trace level, plus `_hard_gate`, from
    `DeltaWriter.rollup_counts` (per day and rule: fails and passes; per day: traces and traces a
    hard rule fails).

    Counting at trace level (not per step) keeps "pass rate" meaning "share of traces". A rule a
    trace was not judged on (not sampled, deferred, not applicable) counts as `n_na`, so its rate
    is over judged answers only.
    """
    totals = {row["day"]: row for row in counts if row["criterion_id"] is None}
    by_rule = {(row["day"], row["criterion_id"]): row for row in counts if row["criterion_id"] is not None}
    rows = []
    for day, total in sorted(totals.items()):
        n = total["n_traces"]
        for criterion in criteria:
            c = by_rule.get((day, criterion.id), {"n_fail": 0, "n_pass": 0})
            rows.append(
                {
                    "space_id": space_id,
                    "day": day,
                    "criterion_id": criterion.id,
                    "n": n,
                    "n_fail": c["n_fail"],
                    "n_na": n - c["n_fail"] - c["n_pass"],
                }
            )
        rows.append(
            {
                "space_id": space_id,
                "day": day,
                "criterion_id": HARD_GATE,
                "n": n,
                "n_fail": total["n_fail"],
                "n_na": 0,
            }
        )
    return rows
