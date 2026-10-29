"""A space's release gate (AI Evals Playbook "Gate design"): an
absolute floor and a relative threshold, per-slice floors, zero-tolerance guardrails, over-refusal
paired with refusal, and latency. A judge that is not calibrated still only reports."""

from test_regression import graded

from evalops.schemas.evaluation import ExpectationResult
from evalops.schemas.profile import GateConfig
from evalops.services.regression.results import case_runs, compare, gate_checks, with_gate


def runs(*specs):
    """specs: (case_id, baseline verdicts, candidate verdicts)."""
    attempts = []
    for case_id, base, cand in specs:
        attempts += graded(case_id, "baseline", base) + graded(case_id, "candidate", cand)
    return case_runs(attempts, {}, {spec[0]: "test" for spec in specs})


def by_name(checks):
    return {c.name: c for c in checks}


def test_floor_and_relative_threshold_catch_a_slow_decline_that_breaks_no_single_case():
    # c3 was flaky and becomes a steady fail: no case is "broken" k of k, yet the rate drops.
    cases = runs(("c1", "PPP", "PPP"), ("c2", "PPP", "PPP"), ("c3", "PPF", "FFF"), ("c4", "PPP", "PPP"))
    gate = GateConfig(min_test_pass_rate=0.9, max_drop=0.1, guardrail_zero_tolerance=False)

    checks = by_name(gate_checks(cases, gate, {}, set()))

    assert checks["Test pass rate (absolute floor)"].passed is False  # 3/4 < 90%
    assert checks["Test pass rate (no worse than baseline)"].passed is True  # 3/4 vs 3/4
    verdict = with_gate(compare(cases, set(), decides=True), list(checks.values()))
    assert verdict.verdict == "reject" and verdict.reasons[-1].startswith(
        "gate: Test pass rate (absolute floor)"
    )


def test_a_slice_floor_and_zero_tolerance_guardrails_are_not_diluted_by_the_average():
    cases = runs(("a", "PPP", "PPP"), ("b", "PPP", "PPP"), ("g", "PPP", "PPF"))
    types = {"a": "core", "b": "core", "g": "guardrail"}
    gate = GateConfig(type_floors={"core": 1.0})

    checks = by_name(gate_checks(cases, gate, types, set()))

    assert checks["core cases (slice floor)"].passed is True
    guard = checks["Guardrail cases (zero tolerance)"]
    assert guard.passed is False and "g" in guard.candidate  # one failed attempt is enough


def answered(case_id, arm, declined):
    """Attempts whose 'answers instead of declining' check passed or failed."""
    out = []
    for n, d in enumerate(declined):
        a = graded(case_id, arm, "F" if d else "P")[0].model_copy(update={"attempt": n})
        expectation = ExpectationResult(kind="behaviour", name="answer", passed=not d)
        out.append(
            a.model_copy(
                update={"expectations": [expectation], "failed_by": ["expectation_judge"] if d else []}
            )
        )
    return out


def test_over_refusal_is_measured_but_an_uncalibrated_judge_only_reports_it():
    attempts = answered("q1", "baseline", [False] * 3) + answered("q1", "candidate", [True, False, False])
    attempts += answered("q2", "baseline", [False] * 3) + answered("q2", "candidate", [False] * 3)
    cases = case_runs(attempts, {}, {"q1": "test", "q2": "test"})
    gate = GateConfig(max_over_refusal=0.0, guardrail_zero_tolerance=False, min_test_pass_rate=1.0)

    uncalibrated = by_name(gate_checks(cases, gate, {}, {"expectation_judge"}))
    refusal = uncalibrated["Over-refusal (declined when an answer was expected)"]
    assert (refusal.passed, refusal.decides, refusal.candidate) == (False, False, "1/6 (17%)")
    # The pass-rate floor does not count a fail only the uncalibrated judge gave.
    assert uncalibrated["Test pass rate (absolute floor)"].passed is True
    verdict = with_gate(
        compare(cases, set(), decides=True, undecidable={"expectation_judge"}), list(uncalibrated.values())
    )
    assert verdict.verdict != "reject" and any("not calibrated" in n for n in verdict.notes)

    calibrated = by_name(gate_checks(cases, gate, {}, set()))
    assert calibrated["Over-refusal (declined when an answer was expected)"].decides is True


def test_p95_latency_compares_the_slow_tail_not_the_average():
    # 2 slow answers in 20 (10%) reach the 95th percentile; 1 in 20 (5%) would not.
    attempts = []
    for n in range(20):
        base = graded(f"c{n}", "baseline", "P")[0].model_copy(update={"seconds": 10.0})
        cand = graded(f"c{n}", "candidate", "P")[0].model_copy(update={"seconds": 60.0 if n >= 18 else 10.0})
        attempts += [base, cand]
    cases = case_runs(attempts, {}, {f"c{n}": "test" for n in range(20)})

    [latency] = gate_checks(
        cases, GateConfig(max_p95_latency_increase=0.2, guardrail_zero_tolerance=False), {}, set()
    )

    assert (latency.baseline, latency.candidate, latency.passed) == ("10 s", "60 s", False)
