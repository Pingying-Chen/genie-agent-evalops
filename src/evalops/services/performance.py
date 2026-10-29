"""The Performance page: how the agent, its judges and its reviewers
change over time. Computed on read from the tables the jobs already write.

Every week of the agent chart is measured with the traces' current verdicts. After a rule change
the check job re-checks the last `recheck_days` days, so recent weeks move together instead of
splitting at the day it was deployed; older weeks keep the verdicts of the rules they had.
"""

import json
import statistics
from datetime import UTC, date, datetime, time, timedelta

from evalops.clients.sql import SqlClient
from evalops.schemas.api import (
    AgentPerformance,
    AgentWeek,
    JudgeBehaviour,
    JudgeHistoryPoint,
    JudgePerformance,
    JudgeTrack,
    JudgeWeek,
    PairAgreement,
    ReviewerPair,
    ReviewerPerformance,
    ReviewerStats,
    SpaceVersionMark,
)
from evalops.services.review.agreement import cohen_kappa, normalized, wilson_lower_bound
from evalops.services.traces import corrected_rate


def week_start(day: date) -> date:
    """Monday of the week, as Spark's `date_trunc('WEEK', ...)`."""
    return day - timedelta(days=day.weekday())


def wilson_interval(successes: int, n: int) -> tuple[float | None, float | None]:
    if n == 0:
        return None, None
    return wilson_lower_bound(successes, n), 1 - wilson_lower_bound(n - successes, n)


def _day(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def current_rates(agreement_rows: list[dict]) -> dict[str, tuple[float | None, float | None]]:
    """(TPR, TNR) per criterion from the latest agreement rows. Where one criterion has several
    rows (verdicts reused from an older judge version), the row with the most labels speaks for it."""
    best: dict[str, dict] = {}
    for row in agreement_rows:
        if row["criterion_id"] not in best or (row.get("n") or 0) > (best[row["criterion_id"]].get("n") or 0):
            best[row["criterion_id"]] = row
    return {cid: (row.get("tpr"), row.get("tnr")) for cid, row in best.items()}


def agent_weeks(
    first_week: date,
    n_weeks: int,
    week_rows: list[dict],
    judge_rows: list[dict],
    rates: dict[str, tuple[float | None, float | None]],
) -> list[AgentWeek]:
    """Every week from `first_week`, with zeros for weeks nobody asked anything."""
    by_week = {_day(r["week"]): r for r in week_rows}
    judges: dict[date, list[JudgeWeek]] = {}
    for r in sorted(judge_rows, key=lambda r: r["criterion_id"]):
        applies, fails = r["applies"] or 0, r["fails"] or 0
        tpr, tnr = rates.get(r["criterion_id"], (None, None))
        judges.setdefault(_day(r["week"]), []).append(
            JudgeWeek(
                criterion_id=r["criterion_id"],
                applies=applies,
                fails=fails,
                corrected_fail_rate=corrected_rate(fails / applies, tpr, tnr) if applies else None,
            )
        )
    weeks = []
    for i in range(n_weeks):
        week = first_week + timedelta(weeks=i)
        r = by_week.get(week, {})
        reviewed, task_passed = r.get("reviewed") or 0, r.get("task_passed") or 0
        low, high = wilson_interval(task_passed, reviewed)
        weeks.append(
            AgentWeek(
                week=week,
                answers=r.get("answers") or 0,
                graded=r.get("graded") or 0,
                checked=r.get("checked") or 0,
                hard_failed=r.get("hard_failed") or 0,
                reviewed=reviewed,
                task_passed=task_passed,
                task_low=low,
                task_high=high,
                median_seconds=r.get("median_seconds"),
                p90_seconds=r.get("p90_seconds"),
                sql_steps=r.get("sql_steps"),
                genie_errors=r.get("genie_errors") or 0,
                judges=judges.get(week, []),
            )
        )
    return weeks


async def agent(space_id: str, *, weeks: int, today: date, sql: SqlClient, prefix: str) -> AgentPerformance:
    first = week_start(today) - timedelta(weeks=weeks - 1)
    params = {"space_id": space_id, "start": datetime.combine(first, time(), UTC)}
    asked = "coalesce(t.asked_at, t.collected_at)"
    week_rows = await sql.execute(
        f"""WITH r AS (
                SELECT trace_id, max(CASE WHEN verdict = 'fail' AND gate = 'hard' THEN 1 ELSE 0 END) AS hard_fail
                FROM {prefix}.current_results WHERE space_id = :space_id
                GROUP BY trace_id),
            tr AS (
                SELECT trace_id, max_by(task_success, updated_at) AS task_success
                FROM {prefix}.trace_reviews WHERE space_id = :space_id AND task_success IS NOT NULL
                GROUP BY trace_id)
            SELECT date_trunc('WEEK', {asked}) AS week, count(*) AS answers,
                   count_if(t.access_status = 'ok') AS graded,
                   count_if(t.access_status = 'ok' AND r.trace_id IS NOT NULL) AS checked,
                   count_if(t.access_status = 'ok' AND r.hard_fail = 1) AS hard_failed,
                   count(tr.task_success) AS reviewed, count_if(tr.task_success = 'pass') AS task_passed,
                   percentile(timestampdiff(SECOND, t.asked_at, t.completed_at), 0.5) AS median_seconds,
                   percentile(timestampdiff(SECOND, t.asked_at, t.completed_at), 0.9) AS p90_seconds,
                   avg(CASE WHEN t.access_status = 'ok' THEN t.n_sql_steps END) AS sql_steps,
                   count_if(t.genie_error IS NOT NULL) AS genie_errors
            FROM {prefix}.traces t LEFT JOIN r ON r.trace_id = t.trace_id LEFT JOIN tr ON tr.trace_id = t.trace_id
            WHERE t.space_id = :space_id AND {asked} >= :start
            GROUP BY 1""",
        params,
    )
    judge_rows = await sql.execute(
        f"""SELECT date_trunc('WEEK', {asked}) AS week, c.criterion_id,
                   count_if(c.verdict IN ('pass', 'fail')) AS applies, count_if(c.verdict = 'fail') AS fails
            FROM {prefix}.current_results c
            JOIN {prefix}.traces t ON t.trace_id = c.trace_id AND t.space_id = :space_id
            WHERE c.space_id = :space_id AND c.family = 'rubric' AND {asked} >= :start
            GROUP BY 1, 2""",
        params,
    )
    agreement = await sql.execute(
        f"""SELECT * FROM {prefix}.judge_agreement
            WHERE space_id = :space_id
              AND computed_at = (SELECT max(computed_at) FROM {prefix}.judge_agreement WHERE space_id = :space_id)""",
        {"space_id": space_id},
    )
    versions = await sql.execute(
        f"""SELECT version, captured_at, previous_version, counts FROM {prefix}.space_versions
            WHERE space_id = :space_id ORDER BY captured_at""",
        {"space_id": space_id},
    )
    return AgentPerformance(
        space_id=space_id,
        weeks=agent_weeks(
            first, weeks, week_rows, judge_rows, current_rates([normalized(r) for r in agreement])
        ),
        versions=version_marks(versions, first),
    )


def version_marks(rows: list[dict], first_week: date) -> list[SpaceVersionMark]:
    """Changes since `first_week`. The first version ever seen is a starting point, not a change."""
    marks = []
    for previous, row in zip(rows, rows[1:], strict=False):
        day = _day(row["captured_at"])
        if day < first_week:
            continue
        before, after = json.loads(previous["counts"] or "{}"), json.loads(row["counts"] or "{}")
        marks.append(
            SpaceVersionMark(
                version=row["version"],
                captured_at=row["captured_at"],
                week=week_start(day),
                changes={
                    k: after.get(k, 0) - before.get(k, 0)
                    for k in after
                    if after.get(k, 0) != before.get(k, 0)
                },
            )
        )
    return marks


def _counts(p: JudgeHistoryPoint) -> tuple:
    return p.n, p.n_applies, p.tp, p.fn, p.fp, p.tn


def judge_tracks(rows: list[dict]) -> list[JudgeTrack]:
    """One track per (criterion version, judge), oldest first, one point per review run that
    changed its counts. Rows from before applicability was measured are left out."""
    tracks: dict[tuple[str, int, str], list[JudgeHistoryPoint]] = {}
    for r in sorted(rows, key=lambda r: r["computed_at"]):
        if r.get("n_applies") is None:
            # Computed when `na` labels still counted towards TNR: not comparable.
            continue
        key = (r["criterion_id"], r["criterion_version"], r["judge"])
        point = JudgeHistoryPoint.model_validate({k: v for k, v in r.items() if v is not None})
        points = tracks.setdefault(key, [])
        # Most review runs add no label for a given judge: keep only the runs that changed it.
        if points and _counts(points[-1]) == _counts(point):
            continue
        points.append(point)
    return [
        JudgeTrack(criterion_id=cid, criterion_version=version, judge=judge, points=points)
        for (cid, version, judge), points in sorted(tracks.items())
    ]


async def judges(space_id: str, *, sql: SqlClient, prefix: str, min_lower_bound: float) -> JudgePerformance:
    params = {"space_id": space_id}
    history = await sql.execute(
        f"""SELECT computed_at, criterion_id, criterion_version, judge, n, n_applies, tp, fn, fp, tn,
                   tpr_lb, tnr_lb, kappa, calibrated, positive_class
            FROM {prefix}.judge_agreement WHERE space_id = :space_id""",
        params,
    )
    history = [normalized(r) for r in history]
    ev = "c.evidence"
    latest = await sql.execute(
        f"""SELECT c.criterion_id, c.criterion_version, count(*) AS verdicts,
                   count_if(c.verdict = 'na') AS na, count_if(c.verdict = 'error') AS errors,
                   count(get_json_object({ev}, '$.agreement')) AS sampled,
                   count_if(CAST(get_json_object({ev}, '$.agreement') AS DOUBLE) = 1) AS unanimous,
                   count_if(get_json_object({ev}, '$.reused_from') IS NULL) AS judged,
                   avg(CASE WHEN get_json_object({ev}, '$.reused_from') IS NULL
                            THEN CAST(get_json_object({ev}, '$.tokens') AS DOUBLE) END) AS tokens_per_verdict
            FROM {prefix}.current_results c
            JOIN {prefix}.traces t ON t.trace_id = c.trace_id AND t.space_id = :space_id
            WHERE c.space_id = :space_id AND c.family = 'rubric'
            GROUP BY 1, 2 ORDER BY 1, 2""",
        params,
    )
    return JudgePerformance(
        space_id=space_id,
        min_lower_bound=min_lower_bound,
        tracks=judge_tracks(history),
        latest=[JudgeBehaviour.model_validate(r) for r in latest],
    )


def pair_agreement(pairs: list[tuple[str, str]]) -> PairAgreement:
    """(a, b) labels; fail vs not fail for kappa, exact match over all labels."""
    both = sum(a == "fail" and b == "fail" for a, b in pairs)
    only_a = sum(a == "fail" and b != "fail" for a, b in pairs)
    only_b = sum(a != "fail" and b == "fail" for a, b in pairs)
    return PairAgreement(
        n=len(pairs),
        exact=sum(a == b for a, b in pairs),
        both_fail=both,
        only_a_fail=only_a,
        only_b_fail=only_b,
        kappa=cohen_kappa(both, only_a, only_b, len(pairs) - both - only_a - only_b),
    )


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def reviewer_stats(
    reviews: list[dict], labels: list[dict], queued: dict[str, datetime], now: datetime
) -> ReviewerPerformance:
    """`reviews`: trace_reviews rows; `labels`: human_labels rows with the current judge
    `verdict` and `verdict_version`; `queued`: first queued time per trace."""
    people = sorted({r["reviewer"] for r in reviews} | {r["reviewer"] for r in labels})
    week_ago = now - timedelta(days=7)
    stats = []
    for person in people:
        mine = [r for r in reviews if r["reviewer"] == person and r.get("task_success")]
        rules = [r for r in labels if r["reviewer"] == person]
        first: dict[str, datetime] = {}  # per trace: their first and latest verdict
        last: dict[str, datetime] = {}
        for trace_id, at in [(r["trace_id"], r["updated_at"]) for r in mine] + [
            (r["trace_id"], r["labeled_at"]) for r in rules
        ]:
            if at is None:
                continue
            first[trace_id] = min(at, first.get(trace_id, at))
            last[trace_id] = max(at, last.get(trace_id, at))
        turnaround = [
            (at - queued[t]).total_seconds() / 3600
            for t, at in first.items()
            if t in queued and at >= queued[t]
        ]
        judged = [
            (r["label"], r["verdict"])
            for r in rules
            if r.get("verdict") in ("pass", "fail", "na")
            and r.get("verdict_version") == r["criterion_version"]
        ]
        fails = [r for r in mine if r["task_success"] == "fail"]
        rule_fails = [r for r in rules if r["label"] == "fail"]
        stats.append(
            ReviewerStats(
                reviewer=person,
                turns_reviewed=len(mine),
                task_failed=len(fails),
                task_fails_with_reason=sum(1 for r in fails if (r.get("task_rationale") or "").strip()),
                rule_labels=len(rules),
                rule_fails=len(rule_fails),
                rule_na=sum(1 for r in rules if r["label"] == "na"),
                rule_fails_with_reason=sum(1 for r in rule_fails if (r.get("rationale") or "").strip()),
                last_active=max(last.values(), default=None),
                active_7d=sum(1 for at in last.values() if at >= week_ago),
                median_turnaround_hours=_median(turnaround),
                with_judges=pair_agreement(judged),
            )
        )
    task = {(r["reviewer"], r["trace_id"]): r["task_success"] for r in reviews if r.get("task_success")}
    rule = {
        (r["reviewer"], r["trace_id"], r["criterion_id"], r["criterion_version"]): r["label"] for r in labels
    }
    pairs = []
    for i, a in enumerate(people):
        for b in people[i + 1 :]:
            on_task = [(v, task[(b, t)]) for (who, t), v in task.items() if who == a and (b, t) in task]
            on_rules = [(v, rule[(b, *k)]) for (who, *k), v in rule.items() if who == a and (b, *k) in rule]
            if on_task or on_rules:
                pairs.append(
                    ReviewerPair(a=a, b=b, task=pair_agreement(on_task), rules=pair_agreement(on_rules))
                )
    verdicts = list(task.values())
    applies = [v for v in rule.values() if v != "na"]
    return ReviewerPerformance(
        space_id="",
        reviewers=stats,
        pairs=pairs,
        task_fail_rate=verdicts.count("fail") / len(verdicts) if verdicts else None,
        rule_fail_rate=applies.count("fail") / len(applies) if applies else None,
    )


async def reviewers(space_id: str, *, now: datetime, sql: SqlClient, prefix: str) -> ReviewerPerformance:
    params = {"space_id": space_id}
    reviews = await sql.execute(
        f"""SELECT reviewer, trace_id, task_success, task_rationale, updated_at
            FROM {prefix}.trace_reviews WHERE space_id = :space_id""",
        params,
    )
    labels = await sql.execute(
        f"""WITH r AS (
                SELECT trace_id, criterion_id, criterion_version AS verdict_version, verdict
                FROM {prefix}.current_results WHERE space_id = :space_id AND family = 'rubric')
            SELECT l.reviewer, l.trace_id, l.criterion_id, l.criterion_version, l.label, l.rationale,
                   l.labeled_at, r.verdict, r.verdict_version
            FROM {prefix}.human_labels l
            LEFT JOIN r ON r.trace_id = l.trace_id AND r.criterion_id = l.criterion_id
            WHERE l.space_id = :space_id""",
        params,
    )
    queued = await sql.execute(
        f"""SELECT trace_id, min(queued_at) AS queued_at FROM {prefix}.review_items
            WHERE space_id = :space_id GROUP BY trace_id""",
        params,
    )
    result = reviewer_stats(reviews, labels, {r["trace_id"]: r["queued_at"] for r in queued}, now)
    return result.model_copy(update={"space_id": space_id})
