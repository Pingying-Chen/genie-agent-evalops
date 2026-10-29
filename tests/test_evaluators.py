"""Evaluators: an LLM judge reports until people have shown it
agrees with them; only then may it decide a gate. Code decides from the start, and shows how it
was measured."""

from datetime import UTC, datetime
from pathlib import Path

from test_api import VIEWER, FakeSql, api, genie, sql  # noqa: F401  (fixtures)
from test_regression import graded

from evalops.services import evaluators
from evalops.services.criteria import criteria_dir, load_criteria
from evalops.services.regression.results import case_runs, compare

DEMO = Path(__file__).resolve().parents[1] / "demo" / "criteria"
CRITERIA, _ = load_criteria(criteria_dir(), DEMO)
NOW = datetime(2026, 10, 27, tzinfo=UTC)


def agreement(tp, fn, fp, tn):
    from evalops.services.review.agreement import score

    pairs = (
        [("fail", "fail")] * tp + [("fail", "pass")] * fn + [("pass", "fail")] * fp + [("pass", "pass")] * tn
    )
    return {**score(pairs), "computed_at": NOW, "judge": JUDGE}


JUDGE = "rubric_judge@4|m|temperature=0"
JUDGES = {"rubric.ambiguous_metric": JUDGE, "rubric.out_of_scope_refusal": JUDGE}


def by_id(found):
    return {e.id: e for e in found}


def test_a_judge_decides_only_once_both_lower_bounds_reach_the_threshold():
    rows = {
        ("rubric.ambiguous_metric", 2): [agreement(tp=20, fn=0, fp=0, tn=20)],  # LB ≈ 0.84 both
        ("rubric.out_of_scope_refusal", 1): [agreement(tp=0, fn=0, fp=0, tn=12)],  # no fails labelled
    }
    found = by_id(
        evaluators.registry(
            CRITERIA, rows, judges=JUDGES, min_lower_bound=0.8, expectation_judge_version="1", reviewers=1
        )
    )

    good = found["criterion:rubric.ambiguous_metric"]
    assert (good.status, good.can_decide) == ("calibrated", True)
    assert good.calibration.precision == 1.0 and good.calibration.precision_lb > 0.8
    unknown = found["criterion:rubric.out_of_scope_refusal"]
    assert (unknown.status, unknown.can_decide) == ("uncalibrated", False)  # TPR cannot be measured
    assert found["expectation_judge"].status == "uncalibrated"  # nobody has labelled its answers yet


def test_a_calibration_counts_only_for_the_judge_it_measured():
    rows = {("rubric.ambiguous_metric", 2): [agreement(tp=20, fn=0, fp=0, tn=20)]}

    def status(judge):
        found = by_id(
            evaluators.registry(
                CRITERIA,
                rows,
                judges={"rubric.ambiguous_metric": judge},
                min_lower_bound=0.8,
                expectation_judge_version="1",
                reviewers=1,
            )
        )["criterion:rubric.ambiguous_metric"]
        return found.status, found.note

    assert status(JUDGE)[0] == "calibrated"
    # Another model, another sampling setting or another prompt version: measured again from new labels.
    for other in (
        "rubric_judge@4|other-model|temperature=0",
        "rubric_judge@4|m|temperature=1",
        "rubric_judge@5|m|temperature=0",
    ):
        assert status(other) == ("uncalibrated", evaluators.OTHER_JUDGE)

    # A note on synthetic labels does not make it another judge.
    rows[("rubric.ambiguous_metric", 2)][0]["judge"] = JUDGE + "|16 synthetic labels"
    assert status(JUDGE)[0] == "calibrated"


def test_code_decides_and_shows_its_measurement():
    found = by_id(
        evaluators.registry(
            CRITERIA, {}, judges=JUDGES, min_lower_bound=0.8, expectation_judge_version="1", reviewers=0
        )
    )
    figures = found["criterion:report.figure_sourced"]
    assert (figures.kind, figures.status, figures.can_decide) == ("code", "deterministic", True)
    assert (figures.measured.false_alarms, figures.measured.negatives) == (0, 218)
    assert (figures.measured.caught, figures.measured.positives) == (94, 104)
    assert found["case_check:reference"].can_decide and not found["human:review"].can_decide


def test_every_failed_expectation_names_its_evaluator():
    assert evaluators.evaluator_id_for("rule", "report.states_period") == "criterion:report.states_period"
    assert evaluators.evaluator_id_for("fact", "covers 2024") == "expectation_judge"
    assert evaluators.evaluator_id_for("behaviour", "clarify") == "expectation_judge"
    assert evaluators.evaluator_id_for("must_not_query", "email") == "case_check:must_not_query"


def failed_by(attempts, who):
    return [a.model_copy(update={"failed_by": who}) if a.verdict == "fail" else a for a in attempts]


def test_a_case_broken_only_by_an_uncalibrated_judge_cannot_decide_the_gate():
    judge_only = failed_by(
        graded("b", "baseline", "PPP") + graded("b", "candidate", "FFF"), ["expectation_judge"]
    )
    by_code = failed_by(
        graded("c", "baseline", "PPP") + graded("c", "candidate", "FFF"), ["criterion:report.figure_sourced"]
    )
    target = failed_by(
        graded("t", "baseline", "FFF") + graded("t", "candidate", "PPP"), ["expectation_judge"]
    )

    gate = compare(
        case_runs(judge_only + target, {}), targets={"t"}, decides=True, undecidable={"expectation_judge"}
    )
    assert gate.verdict == "inconclusive" and gate.counts["unconfirmed"] == 2
    assert any("only by expectation_judge" in r for r in gate.reasons)

    real = compare(
        case_runs(judge_only + by_code, {}), targets=set(), decides=True, undecidable={"expectation_judge"}
    )
    assert real.verdict == "reject" and real.reasons[0] == "broke “c”"  # code still decides

    calibrated = compare(case_runs(judge_only, {}), targets=set(), decides=True, undecidable=set())
    assert calibrated.verdict == "reject"  # once calibrated, the same fail decides


def test_the_evaluators_page_is_for_owners(api):  # noqa: F811
    found = api.get("/api/spaces/space-ok/evaluators", headers=VIEWER)
    assert found.status_code == 200 and any(
        e["id"] == "expectation_judge" for e in found.json()["evaluators"]
    )
    assert api.get("/api/spaces/space-no/evaluators", headers=VIEWER).status_code in (403, 404)
