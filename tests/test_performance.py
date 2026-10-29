"""Performance page maths: reviewer activity and agreement."""

from datetime import UTC, datetime, timedelta

import pytest

from evalops.services.performance import (
    pair_agreement,
    reviewer_stats,
    version_marks,
    week_start,
    wilson_interval,
)

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)
QUEUED = NOW - timedelta(days=3)


def review(who, trace, task, reason="", hours=1.0):
    return {
        "reviewer": who,
        "trace_id": trace,
        "task_success": task,
        "task_rationale": reason,
        "updated_at": QUEUED + timedelta(hours=hours),
    }


def label(who, trace, value, verdict="pass", version=1, reason="", hours=2.0):
    return {
        "reviewer": who,
        "trace_id": trace,
        "criterion_id": "rubric.x",
        "criterion_version": 1,
        "label": value,
        "rationale": reason,
        "labeled_at": QUEUED + timedelta(hours=hours),
        "verdict": verdict,
        "verdict_version": version,
    }


def test_weeks_start_on_monday_and_an_interval_is_wide_with_few_reviews():
    assert week_start(datetime(2026, 10, 25).date()).isoformat() == "2026-10-19"
    low, high = wilson_interval(4, 5)
    assert 0.3 < low < 0.8 < high < 1
    assert wilson_interval(0, 0) == (None, None)


def test_a_reviewer_is_measured_on_activity_strictness_reasons_and_turnaround():
    reviews = [
        review("a", "t1", "fail", "wrong year", hours=1),
        review("a", "t2", "pass", hours=5),
        review("a", "t3", "fail"),
    ]
    labels = [
        label("a", "t1", "fail", verdict="fail", reason="no measure"),
        label("a", "t2", "na", verdict="na", hours=6),
    ]

    [a] = reviewer_stats(reviews, labels, {"t1": QUEUED, "t2": QUEUED}, NOW).reviewers

    assert (a.turns_reviewed, a.task_failed, a.task_fails_with_reason) == (3, 2, 1)
    assert (a.rule_labels, a.rule_fails, a.rule_na, a.rule_fails_with_reason) == (2, 1, 1, 1)
    assert a.median_turnaround_hours == 3.0  # t1 first touched after 1h, t2 after 5h; t3 was never queued
    assert a.active_7d == 3
    assert (a.with_judges.n, a.with_judges.exact) == (2, 2)


def test_judge_agreement_skips_errors_and_verdicts_on_another_rule_version():
    labels = [label("a", "t1", "fail", verdict="error"), label("a", "t2", "fail", verdict="pass", version=2)]

    [a] = reviewer_stats([], labels, {}, NOW).reviewers

    assert a.with_judges.n == 0


def test_two_reviewers_are_compared_only_on_turns_both_reviewed():
    reviews = [
        review("a", "t1", "fail"),
        review("b", "t1", "fail"),
        review("a", "t2", "pass"),
        review("b", "t2", "fail"),
        review("b", "t3", "pass"),
        review("a", "t4", "pass"),
        review("b", "t4", "pass"),
    ]
    labels = [label("a", "t1", "fail"), label("b", "t1", "pass")]

    result = reviewer_stats(reviews, labels, {}, NOW)

    [pair] = result.pairs
    assert (pair.a, pair.b) == ("a", "b")
    assert (pair.task.n, pair.task.exact, pair.task.both_fail, pair.task.only_b_fail) == (3, 2, 1, 1)
    assert pair.task.kappa == pytest.approx(0.4)
    assert (pair.rules.n, pair.rules.only_a_fail) == (1, 1)
    assert result.task_fail_rate == pytest.approx(3 / 7)


def test_one_reviewer_has_no_pairs_and_kappa_needs_both_classes():
    assert reviewer_stats([review("a", "t1", "pass")], [], {}, NOW).pairs == []
    assert pair_agreement([("pass", "pass"), ("pass", "pass")]).kappa is None


def test_a_version_mark_says_what_changed_and_the_first_version_is_not_a_change():
    rows = [
        {
            "version": "a",
            "captured_at": datetime(2026, 9, 1, tzinfo=UTC),
            "counts": '{"instructions": 5, "sql_examples": 0}',
        },
        {
            "version": "b",
            "captured_at": datetime(2026, 9, 20, tzinfo=UTC),
            "counts": '{"instructions": 5, "sql_examples": 1}',
        },
        {
            "version": "c",
            "captured_at": datetime(2026, 10, 21, tzinfo=UTC),
            "counts": '{"instructions": 4, "sql_examples": 1}',
        },
    ]

    marks = version_marks(rows, datetime(2026, 10, 5).date())

    [mark] = marks  # "b" is before the window, "a" is where history starts
    assert (mark.version, mark.week.isoformat(), mark.changes) == ("c", "2026-10-19", {"instructions": -1})
