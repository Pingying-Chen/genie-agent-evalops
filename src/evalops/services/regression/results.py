"""Read side of regression runs: runs, and per case how each arm did
across its k attempts."""

import json

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import NotFound
from evalops.schemas.changes import SpaceChange
from evalops.schemas.evaluation import (
    ArmOutcome,
    ArmSummary,
    CaseComparison,
    CaseRun,
    Comparison,
    EvalRun,
    EvalRunDetail,
    EvalRuns,
    ExpectationResult,
    GateCheck,
    GradedAttempt,
    Preflight,
    Slice,
)
from evalops.schemas.manifest import Manifest
from evalops.schemas.profile import GateConfig
from evalops.services import evalset, splits
from evalops.services.evaluators import undecidable
from evalops.services.regression.leakage import added_examples, leaks
from evalops.services.review.agreement import wilson_lower_bound
from evalops.services.splits import DIMENSIONS


def outcome(attempts: list[GradedAttempt]) -> ArmOutcome:
    passes = sum(a.verdict == "pass" for a in attempts)
    fails = sum(a.verdict == "fail" for a in attempts)
    errors = sum(a.verdict == "error" for a in attempts)
    return ArmOutcome(
        passes=passes,
        fails=fails,
        errors=errors,
        pass_all=passes > 0 and fails == 0 and errors == 0,
        pass_any=passes > 0,
        flaky=passes > 0 and fails > 0,
    )


def case_runs(
    attempts: list[GradedAttempt], titles: dict[str, str], split_of: dict[str, str] | None = None
) -> list[CaseRun]:
    by_case: dict[tuple[str, int], list[GradedAttempt]] = {}
    for a in attempts:
        by_case.setdefault((a.case_id, a.case_version), []).append(a)
    runs = []
    for (case_id, version), mine in sorted(
        by_case.items(), key=lambda kv: titles.get(kv[0][0], kv[0][0]).lower()
    ):
        arms = sorted({a.arm for a in mine})
        runs.append(
            CaseRun(
                case_id=case_id,
                case_version=version,
                title=titles.get(case_id),
                split=(split_of or {}).get(case_id),
                arms={arm: outcome([a for a in mine if a.arm == arm]) for arm in arms},
                attempts=sorted(mine, key=lambda a: (a.arm, a.attempt)),
            )
        )
    return runs


def arm_summaries(cases: list[CaseRun], versions: dict[str, str]) -> list[ArmSummary]:
    arms = sorted({arm for c in cases for arm in c.arms}, key=lambda a: a != "baseline")
    out = []
    for arm in arms:
        mine = [c.arms[arm] for c in cases if arm in c.arms]
        out.append(
            ArmSummary(
                arm=arm,
                config_version=versions.get(arm),
                cases=len(mine),
                pass_all=sum(o.pass_all for o in mine),
                pass_any=sum(o.pass_any for o in mine),
                flaky=sum(o.flaky for o in mine),
                errored=sum(o.errors > 0 for o in mine),
            )
        )
    return out


def _leak_aware(outcome: str, leaked: bool) -> str:
    return "leaked" if leaked and outcome in ("fixed", "still_passes") else outcome


def classify(baseline: ArmOutcome, candidate: ArmOutcome) -> str:
    """Only k-out-of-k results count as fixed or broken; any split is flaky, any error ungraded."""
    if baseline.errors or candidate.errors:
        return "not_graded"
    if baseline.flaky or candidate.flaky:
        return "flaky"
    if baseline.pass_all and candidate.pass_all:
        return "still_passes"
    if baseline.pass_all:
        return "broken"
    return "fixed" if candidate.pass_all else "still_fails"


def complete(case: CaseRun, k: int | None) -> bool:
    """Both configurations answered the case, k times each when k is known."""
    return all(
        arm in case.arms and (k is None or sum(a.arm == arm for a in case.attempts) >= k)
        for arm in ("baseline", "candidate")
    )


FAILING_ARMS = {"fixed": ("baseline",), "broken": ("candidate",), "still_fails": ("baseline", "candidate")}


def unconfirmed_by(case: CaseRun, outcome: str, undecidable: set[str]) -> list[str]:
    """The uncalibrated evaluators an outcome rests on, if it rests on nothing else: every failed
    attempt on the side(s) that failed was failed only by evaluators that may not decide."""
    failing = [a for a in case.attempts if a.arm in FAILING_ARMS.get(outcome, ()) and a.verdict == "fail"]
    if not failing or not undecidable:
        return []
    if all(a.failed_by and set(a.failed_by) <= undecidable for a in failing):
        return sorted({e for a in failing for e in a.failed_by})
    return []


def compare(
    cases: list[CaseRun],
    targets: set[str],
    leaked: set[str] = frozenset(),
    *,
    decides: bool = False,
    undecidable: set[str] = frozenset(),
    expected: dict[str, str | None] | None = None,
    k: int | None = None,
    titles: dict[str, str] | None = None,
) -> Comparison | None:
    """`leaked`: cases whose question the change adds as a SQL example. On a test (or unsplit)
    case, passing proves nothing (Genie was handed the answer), so it never counts as fixed;
    breaking it still counts. On a dev case it is allowed (few-shot examples may come from dev)
    and only marked. `decides`: a gate run, whose verdict alone can let the change be applied.
    `undecidable`: evaluators that may not decide (uncalibrated judges, as recorded when the run
    started); an outcome resting only on them is unconfirmed, so it neither accepts nor rejects.
    `expected`: every case the run asked (id -> split) and `k`: attempts per configuration. A case
    without all of them, or with an error, is not graded, and any such case makes it inconclusive:
    a gate must not accept on the cases that happened to come back."""
    if not any({"baseline", "candidate"} <= set(c.arms) for c in cases):
        return None
    asked = {c.case_id for c in cases}
    missing = [
        CaseRun(case_id=cid, case_version=0, title=(titles or {}).get(cid), split=split, arms={}, attempts=[])
        for cid, split in (expected or {}).items()
        if cid not in asked
    ]
    rows = []
    for c in [*cases, *missing]:
        graded = classify(c.arms["baseline"], c.arms["candidate"]) if complete(c, k) else "not_graded"
        outcome = _leak_aware(graded, c.case_id in leaked and c.split != "dev")
        rows.append(
            CaseComparison(
                case_id=c.case_id,
                title=c.title,
                outcome=outcome,
                target=c.case_id in targets,
                split=c.split,
                example_match=c.case_id in leaked and c.split == "dev",
                unconfirmed_by=unconfirmed_by(c, outcome, set(undecidable)),
            )
        )
    counts = {
        o: sum(r.outcome == o for r in rows)
        for o in ("fixed", "broken", "still_passes", "still_fails", "flaky", "not_graded", "leaked")
    }
    counts["unconfirmed"] = sum(bool(r.unconfirmed_by) for r in rows)
    broken = [r for r in rows if r.outcome == "broken" and not r.unconfirmed_by]
    missed = [r for r in rows if r.target and r.outcome == "still_fails" and not r.unconfirmed_by]
    # A target that already passes without the change shows nothing was fixed: not an accept.
    unsure = [r for r in rows if r.target and r.outcome in ("flaky", "leaked", "still_passes")]
    unsure += [
        r
        for r in rows
        if r.unconfirmed_by and (r.outcome == "broken" or (r.target and r.outcome != "still_passes"))
    ]
    reasons = [f"broke “{r.title or r.case_id}”" for r in broken]
    reasons += [f"did not fix “{r.title or r.case_id}”" for r in missed]
    reasons += [
        f"“{r.title or r.case_id}” is {r.outcome.replace('_', ' ')} only by {', '.join(r.unconfirmed_by)}, "
        "which is not calibrated, so it cannot decide"
        if r.unconfirmed_by
        else f"“{r.title or r.case_id}” is a SQL example in this change, so passing it proves nothing"
        if r.outcome == "leaked"
        else f"“{r.title or r.case_id}” already passes without the change"
        if r.outcome == "still_passes"
        else f"“{r.title or r.case_id}” is {r.outcome.replace('_', ' ')}"
        for r in unsure
    ]
    ungraded = [r for r in rows if r.outcome == "not_graded"]
    if ungraded:
        named = ", ".join(f"“{r.title or r.case_id}”" for r in ungraded[:5]) + (
            " …" if len(ungraded) > 5 else ""
        )
        reasons.append(
            f"{len(ungraded)} of {len(rows)} cases ({len(ungraded) / len(rows):.0%}) were not graded "
            f"on both configurations, {k or 'every'} time(s) each: {named}"
        )
    if not targets:
        reasons.append("no target cases: only checked that nothing broke")
    verdict = "reject" if broken or missed else "inconclusive" if unsure or ungraded else "accept"
    notes = [] if decides else ["an iteration run: only a gate run (test split) can accept the change"]
    notes += [
        f"“{r.title or r.case_id}” is a test case and a target: the change was written against the test set"
        for r in rows
        if r.target and r.split == "test"
    ]
    notes += [
        f"“{r.title or r.case_id}” is a dev case the change adds as a SQL example: its fix proves little"
        for r in rows
        if r.example_match
    ]
    return Comparison(
        verdict=verdict, decides=decides, reasons=reasons, notes=notes, counts=counts, cases=rows
    )


def decided(attempt: GradedAttempt, undecidable: set[str]) -> str:
    """An attempt's verdict as a gate may use it: a fail that only uncalibrated judges gave counts
    as a pass, so they report without deciding."""
    if attempt.verdict == "fail" and attempt.failed_by and set(attempt.failed_by) <= undecidable:
        return "pass"
    return attempt.verdict


def _rate(cases: list[CaseRun], arm: str, undecidable: set[str]) -> tuple[int, int]:
    """(cases passed every time, cases graded) on one arm, as the gate counts them."""
    passed = graded = 0
    for c in cases:
        attempts = [a for a in c.attempts if a.arm == arm]
        if not attempts or any(a.verdict == "error" for a in attempts):
            continue
        graded += 1
        passed += all(decided(a, undecidable) == "pass" for a in attempts)
    return passed, graded


def _pct(hits: int, n: int) -> str:
    return f"{hits}/{n} ({hits / n:.0%})" if n else "—"


def _declined(cases: list[CaseRun], arm: str) -> tuple[int, int]:
    """Attempts on cases that expect an answer, and those judged to have declined instead."""
    declined = total = 0
    for c in cases:
        for a in c.attempts:
            if a.arm != arm:
                continue
            for e in a.expectations:
                if e.kind == "behaviour" and e.name == "answer" and e.passed is not None:
                    total += 1
                    declined += e.passed is False
    return declined, total


def _p95(cases: list[CaseRun], arm: str) -> float | None:
    seconds = sorted(a.seconds for c in cases for a in c.attempts if a.arm == arm and a.verdict != "error")
    if not seconds:
        return None
    return seconds[min(len(seconds) - 1, round(0.95 * (len(seconds) - 1)))]


def gate_checks(
    cases: list[CaseRun], gate: GateConfig, types: dict[str, str], undecidable: set[str]
) -> list[GateCheck]:
    """Pure: the space's configured gate on the cases that count (a gate run's test cases)."""
    pairs = [c for c in cases if {"baseline", "candidate"} <= set(c.arms)]
    if not pairs:
        return []
    checks = []
    base_hits, base_n = _rate(pairs, "baseline", undecidable)
    cand_hits, cand_n = _rate(pairs, "candidate", undecidable)
    if gate.min_test_pass_rate is not None:
        checks.append(
            GateCheck(
                name="Test pass rate (absolute floor)",
                evaluator="every decisive evaluator",
                baseline=_pct(base_hits, base_n),
                candidate=_pct(cand_hits, cand_n),
                threshold=f"≥ {gate.min_test_pass_rate:.0%}",
                passed=cand_hits / cand_n >= gate.min_test_pass_rate if cand_n else None,
            )
        )
    if gate.max_drop is not None:
        ok = cand_hits / cand_n >= base_hits / base_n - gate.max_drop if cand_n and base_n else None
        checks.append(
            GateCheck(
                name="Test pass rate (no worse than baseline)",
                evaluator="every decisive evaluator",
                baseline=_pct(base_hits, base_n),
                candidate=_pct(cand_hits, cand_n),
                threshold=f"at most {gate.max_drop:.0%} below baseline",
                passed=ok,
            )
        )
    for case_type, floor in sorted(gate.type_floors.items()):
        mine = [c for c in pairs if types.get(c.case_id) == case_type]
        b, bn = _rate(mine, "baseline", undecidable)
        h, n = _rate(mine, "candidate", undecidable)
        checks.append(
            GateCheck(
                name=f"{case_type} cases (slice floor)",
                evaluator="every decisive evaluator",
                baseline=_pct(b, bn),
                candidate=_pct(h, n),
                threshold=f"≥ {floor:.0%}",
                passed=h / n >= floor if n else None,
            )
        )
    if gate.guardrail_zero_tolerance:
        guard = [c for c in pairs if types.get(c.case_id) == "guardrail"]
        failed = [
            c.title or c.case_id
            for c in guard
            if any(a.arm == "candidate" and decided(a, undecidable) == "fail" for a in c.attempts)
        ]
        detail = f": {', '.join(failed)}" if failed else ""
        checks.append(
            GateCheck(
                name="Guardrail cases (zero tolerance)",
                evaluator="every decisive evaluator",
                candidate=f"{len(failed)} of {len(guard)} failed at least once{detail}",
                threshold="no failed attempt",
                passed=not failed if guard else None,
            )
        )
    if gate.max_over_refusal is not None:
        bd, bt = _declined(pairs, "baseline")
        cd, ct = _declined(pairs, "candidate")
        ok = cd / ct <= gate.max_over_refusal and (not bt or cd / ct <= bd / bt) if ct else None
        checks.append(
            GateCheck(
                name="Over-refusal (declined when an answer was expected)",
                evaluator="expectation_judge",
                baseline=_pct(bd, bt),
                candidate=_pct(cd, ct),
                threshold=f"≤ {gate.max_over_refusal:.0%} and not above baseline",
                passed=ok,
                decides="expectation_judge" not in undecidable,
            )
        )
    if gate.max_p95_latency_increase is not None:
        bp, cp = _p95(pairs, "baseline"), _p95(pairs, "candidate")
        checks.append(
            GateCheck(
                name="p95 time to answer",
                evaluator="timer",
                baseline=f"{bp:.0f} s" if bp is not None else "—",
                candidate=f"{cp:.0f} s" if cp is not None else "—",
                threshold=f"at most {gate.max_p95_latency_increase:.0%} above baseline",
                passed=cp <= bp * (1 + gate.max_p95_latency_increase) if bp and cp is not None else None,
            )
        )
    return checks


def with_gate(comparison: Comparison, checks: list[GateCheck]) -> Comparison:
    """A failed decisive gate check rejects; one that only reports is shown, not counted."""
    failed = [c for c in checks if c.passed is False and c.decides]
    reasons = [
        *comparison.reasons,
        *(f"gate: {c.name} failed ({c.candidate}; needs {c.threshold})" for c in failed),
    ]
    notes = [
        *comparison.notes,
        *(
            f"gate: {c.name} would fail ({c.candidate}), but it rests on a judge that is not calibrated"
            for c in checks
            if c.passed is False and not c.decides
        ),
    ]
    verdict = "reject" if failed else comparison.verdict
    return comparison.model_copy(
        update={"verdict": verdict, "reasons": reasons, "notes": notes, "gate": checks}
    )


def decide(
    cases: list[CaseRun],
    *,
    targets: set[str],
    leaked: set[str],
    decides: bool,
    undecidable: set[str],
    gate: GateConfig | None,
    types: dict[str, str],
    expected: dict[str, str | None] | None = None,
    k: int | None = None,
    titles: dict[str, str] | None = None,
) -> Comparison | None:
    """The run's verdict: the comparison, then the space's gate. The evaluate job stores it when
    the run ends, so a later edit to the eval set or the change cannot alter it."""
    comparison = compare(
        cases,
        targets,
        leaked,
        decides=decides,
        undecidable=undecidable,
        expected=expected,
        k=k,
        titles=titles,
    )
    if comparison is None or gate is None:
        return comparison
    # A gate run's limits are about the held-out test cases; the targets are checked by compare().
    counted = [c for c in cases if c.split == "test"] if decides else cases
    return with_gate(comparison, gate_checks(counted, gate, types, undecidable))


def slices(cases: list[CaseRun], values: dict[str, dict[str, str]], min_stratum: int) -> list[Slice]:
    """pass^k per arm by split and by every case dimension, with a Wilson 95% interval."""
    out = []
    for dimension in ("split", *DIMENSIONS):
        groups: dict[tuple[str, str], list[ArmOutcome]] = {}
        for c in cases:
            value = (c.split or "none") if dimension == "split" else values.get(c.case_id, {}).get(dimension)
            if value is None:
                continue
            for arm, o in c.arms.items():
                groups.setdefault((value, arm), []).append(o)
        for (value, arm), outcomes in sorted(groups.items(), key=lambda kv: (kv[0][1] != "baseline", kv[0])):
            n, passed = len(outcomes), sum(o.pass_all for o in outcomes)
            out.append(
                Slice(
                    dimension=dimension,
                    value=value,
                    arm=arm,
                    cases=n,
                    pass_all=passed,
                    low=wilson_lower_bound(passed, n),
                    high=None if not n else 1 - wilson_lower_bound(n - passed, n),
                    too_small=n < min_stratum,
                )
            )
    return out


def graded_from_row(row: dict) -> GradedAttempt:
    return GradedAttempt.model_validate(
        {
            **row,
            "hard_fails": json.loads(row.get("hard_fails") or "[]"),
            "failed_by": json.loads(row.get("failed_by") or "[]"),
            "steps": json.loads(row.get("steps") or "[]"),
            "expectations": [
                ExpectationResult.model_validate(e) for e in json.loads(row.get("expectations") or "[]")
            ],
        }
    )


def _run(row: dict, cases: list[CaseRun]) -> dict:
    versions = {a["arm"]: a.get("config_version") for a in json.loads(row.get("arms") or "[]")}
    return {
        **{
            k: row.get(k)
            for k in ("run_id", "space_id", "label", "change_id", "status", "k", "started_at", "finished_at")
        },
        "split": row.get("split") or "all",
        "criteria_version": row.get("criteria_version"),
        "tester": row.get("tester"),
        "error": row.get("error"),
        "preflight": Preflight.model_validate_json(row["preflight"]) if row.get("preflight") else None,
        "arms": arm_summaries(cases, versions),
    }


async def _titles(store: StateStore, space_id: str) -> dict[str, str]:
    return {c["case_id"]: c["title"] for c in await store.get_eval_cases(space_id)}


async def runs(space_id: str, *, sql: SqlClient, store: StateStore, prefix: str, limit: int = 20) -> EvalRuns:
    params = {"space_id": space_id}
    rows = await sql.execute(
        f"""SELECT * FROM {prefix}.eval_runs WHERE space_id = :space_id
            ORDER BY started_at DESC LIMIT {int(limit)}""",
        params,
    )
    results = await sql.execute(
        f"""SELECT r.* FROM {prefix}.eval_results r
            JOIN (SELECT run_id FROM {prefix}.eval_runs WHERE space_id = :space_id
                  ORDER BY started_at DESC LIMIT {int(limit)}) x USING (run_id)
            WHERE r.space_id = :space_id""",
        params,
    )
    titles = await _titles(store, space_id)
    by_run: dict[str, list[GradedAttempt]] = {}
    for r in results:
        by_run.setdefault(r["run_id"], []).append(graded_from_row(r))
    return EvalRuns(
        space_id=space_id,
        runs=[EvalRun.model_validate(_run(r, case_runs(by_run.get(r["run_id"], []), titles))) for r in rows],
    )


async def run_detail(
    space_id: str, run_id: str, *, sql: SqlClient, store: StateStore, prefix: str
) -> EvalRunDetail:
    params = {"space_id": space_id, "run_id": run_id}
    rows = await sql.execute(
        f"SELECT * FROM {prefix}.eval_runs WHERE space_id = :space_id AND run_id = :run_id", params
    )
    if not rows:
        raise NotFound("No such run for this space.")
    results = await sql.execute(
        f"SELECT * FROM {prefix}.eval_results WHERE space_id = :space_id AND run_id = :run_id", params
    )
    run_row = rows[0]
    split_of = {
        c["case_id"]: c.get("split") for c in json.loads(run_row.get("cases") or "[]") if c.get("split")
    }
    titles = await _titles(store, space_id)
    cases = case_runs([graded_from_row(r) for r in results], titles, split_of)
    every = await evalset.cases(store, space_id)
    values = await splits.values_for(space_id, every, sql=sql, store=store, prefix=prefix)
    settings = await splits.get_settings(store, space_id)
    detail = {**_run(run_row, cases), "cases": cases, "slices": slices(cases, values, settings.min_stratum)}
    if run_row.get("decision"):
        # Decided when the run ended, on the eval set and change as they were then.
        return EvalRunDetail.model_validate(
            {**detail, "comparison": Comparison.model_validate_json(run_row["decision"])}
        )
    # Runs from before decisions were stored: decided now, on today's eval set.
    targets: set[str] = set()
    leaked: set[str] = set()
    if rows[0].get("change_id"):
        change = next(
            (c for c in await store.get_changes(space_id) if c["change_id"] == rows[0]["change_id"]), None
        )
        targets = set((change or {}).get("target_case_ids") or [])
        if change:
            added = added_examples(SpaceChange.model_validate(change).operations)
            leaked = {leak.case_id for leak in leaks(added, every)}
    decides = run_row.get("split") == "test"
    recorded = await sql.execute(
        f"SELECT manifest FROM {prefix}.run_manifests WHERE space_id = :space_id AND run_id = :run_id",
        params,
    )
    recorded_settings = Manifest.model_validate_json(recorded[0]["manifest"]).settings if recorded else {}
    statuses = recorded_settings.get("evaluators") or {}
    # The gate as configured when the run started (in its manifest); runs before profiles have none.
    gate = GateConfig.model_validate(recorded_settings["gate"]) if recorded_settings.get("gate") else None
    types = {c.case_id: c.type for c in every}
    expected = {c["case_id"]: c.get("split") for c in json.loads(run_row.get("cases") or "[]")}
    comparison = decide(
        cases,
        targets=targets,
        leaked=leaked,
        decides=decides,
        undecidable=undecidable(statuses),
        gate=gate,
        types=types,
        expected=expected,
        k=int(run_row["k"]) if run_row.get("k") is not None else None,
        titles=titles,
    )
    return EvalRunDetail.model_validate({**detail, "comparison": comparison})
