"""Read side for the API: space cards, summaries, trace lists and trace detail.

Identity: the app reads Delta and Lakebase as **its own service principal**, never as the
viewer. Viewers get no grants on the evalops tables (a grant would let them query every
space's traces directly, around this filter). Permission is decided per request with the
viewer's own token (`services/permissions`), and every query here takes the allowed space(s)
as a SQL parameter, so a caller cannot forget the filter.

Verdicts are the current ones (`current_results`: the latest check of each trace).
"""

import base64
import json
from datetime import UTC, date, datetime, time, timedelta

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import NotFound
from evalops.schemas.api import (
    BlindSpot,
    Coverage,
    CriterionStat,
    DailyPoint,
    JudgeCalibration,
    JudgeRow,
    Observation,
    Observations,
    SpaceCard,
    SpaceSummary,
    Suggestion,
    TraceDetail,
    TraceListItem,
)
from evalops.schemas.checks import CheckResult, Criterion, Gate
from evalops.schemas.common import Page
from evalops.schemas.review import Agreement
from evalops.schemas.trace import AccessStatus, Step, Trace
from evalops.services.checker import HARD_GATE
from evalops.services.review.agreement import normalized


def _window(day_from: date, day_to: date) -> tuple[datetime, datetime]:
    """Inclusive day window -> [start, end) timestamps in UTC."""
    return datetime.combine(day_from, time(), UTC), datetime.combine(day_to + timedelta(days=1), time(), UTC)


async def coverage(sql: SqlClient, prefix: str, space_id: str, day_from: date, day_to: date) -> Coverage:
    start, end = _window(day_from, day_to)
    rows = await sql.execute(
        f"""SELECT access_status, count(*) AS n FROM {prefix}.traces
            WHERE space_id = :space_id AND coalesce(asked_at, collected_at) >= :start
              AND coalesce(asked_at, collected_at) < :end
            GROUP BY access_status""",
        {"space_id": space_id, "start": start, "end": end},
    )
    by_status = {AccessStatus(r["access_status"]): r["n"] for r in rows}
    return Coverage(total=sum(by_status.values()), by_status=by_status)


def _rate(n: int, n_fail: int, n_na: int) -> float | None:
    graded = n - n_na
    return (graded - n_fail) / graded if graded > 0 else None


async def space_summary(
    space_id: str,
    day_from: date,
    day_to: date,
    *,
    sql: SqlClient,
    store: StateStore,
    prefix: str,
    criteria: list[Criterion],
) -> SpaceSummary:
    """Coverage from Delta; per-criterion stats and the daily trend from the Lakebase rollup."""
    rollup = await store.get_daily_rollup(space_id, day_from, day_to)
    gates = {c.id: c.gate for c in criteria}
    totals: dict[str, list[int]] = {}
    daily: list[DailyPoint] = []
    for row in rollup:
        if row["criterion_id"] == HARD_GATE:
            daily.append(
                DailyPoint(
                    day=row["day"], n_traces=row["n"], hard_pass_rate=_rate(row["n"], row["n_fail"], 0)
                )
            )
            continue
        t = totals.setdefault(row["criterion_id"], [0, 0, 0])
        t[0] += row["n"]
        t[1] += row["n_fail"]
        t[2] += row["n_na"]
    stats = [
        CriterionStat(
            criterion_id=cid,
            gate=gates.get(cid, Gate.SOFT),
            n=n - n_na,
            n_fail=n_fail,
            pass_rate=_rate(n, n_fail, n_na),
        )
        for cid, (n, n_fail, n_na) in sorted(totals.items())
    ]
    status = await store.get_space_status([space_id])
    return SpaceSummary(
        space_id=space_id,
        title=status.get(space_id, {}).get("title"),
        window_from=day_from,
        window_to=day_to,
        coverage=await coverage(sql, prefix, space_id, day_from, day_to),
        criteria=stats,
        daily=daily,
    )


async def list_space_cards(
    spaces: list[tuple[str, str | None]], *, sql: SqlClient, store: StateStore, prefix: str, today: date
) -> list[SpaceCard]:
    """One card per allowed space `(id, title)`: monitored flag, 30-day coverage and pass rate."""
    status = await store.get_space_status([space_id for space_id, _ in spaces])
    cards = []
    for space_id, title in spaces:
        info = status.get(space_id, {})
        rollup = await store.get_daily_rollup(space_id, today - timedelta(days=30), today)
        gate = [r for r in rollup if r["criterion_id"] == HARD_GATE]
        n, n_fail = sum(r["n"] for r in gate), sum(r["n_fail"] for r in gate)
        cards.append(
            SpaceCard(
                space_id=space_id,
                title=title or info.get("title"),
                monitored=bool(info["collector_has_manage"]) if info else None,
                coverage=await coverage(sql, prefix, space_id, today - timedelta(days=30), today)
                if info
                else None,
                hard_pass_rate=_rate(n, n_fail, 0),
                last_collected_at=info.get("last_collected_at"),
            )
        )
    return cards


def _encode_cursor(asked_at: datetime | None, trace_id: str) -> str:
    raw = json.dumps([asked_at.isoformat() if asked_at else None, trace_id])
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        at, trace_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(at) if at else datetime.min.replace(tzinfo=UTC), trace_id
    except (ValueError, TypeError) as exc:
        raise NotFound("Invalid cursor.") from exc


async def list_traces(
    space_id: str,
    *,
    window_from: date | None,
    window_to: date | None,
    verdict: str | None,
    criterion: str | None,
    limit: int,
    cursor: str | None,
    sql: SqlClient,
    prefix: str,
) -> Page[TraceListItem]:
    """Newest first, keyset-paginated on (asked_at, trace_id); current verdicts."""
    params: dict = {"space_id": space_id}
    # LIMIT must be an INT literal (a BIGINT parameter is rejected: INVALID_LIMIT_LIKE_EXPRESSION).
    # `limit` is validated by the route (1..200) and forced through int(), so inlining is safe.
    fetch = int(limit) + 1
    filters = ["t.space_id = :space_id"]
    if window_from:
        params["start"] = _window(window_from, window_from)[0]
        filters.append("coalesce(t.asked_at, t.collected_at) >= :start")
    if window_to:
        params["end"] = _window(window_to, window_to)[1]
        filters.append("coalesce(t.asked_at, t.collected_at) < :end")
    if cursor:
        params["c_at"], params["c_id"] = _decode_cursor(cursor)
        filters.append(
            "(coalesce(t.asked_at, t.collected_at) < :c_at OR "
            "(coalesce(t.asked_at, t.collected_at) = :c_at AND t.trace_id < :c_id))"
        )
    verdict_sql = (
        "CASE WHEN t.access_status <> 'ok' THEN 'not_gradable' "
        "WHEN size(coalesce(r.hard_fails, array())) > 0 THEN 'fail' ELSE 'pass' END"
    )
    if verdict:
        params["verdict"] = verdict
        filters.append(f"{verdict_sql} = :verdict")
    if criterion:
        params["criterion"] = criterion
        filters.append("array_contains(coalesce(r.fails, array()), :criterion)")
    rows = await sql.execute(
        f"""WITH r AS (
                SELECT trace_id,
                       collect_set(CASE WHEN verdict = 'fail' AND gate = 'hard' THEN criterion_id END) AS hard_fails,
                       collect_set(CASE WHEN verdict = 'fail' THEN criterion_id END) AS fails
                FROM {prefix}.current_results WHERE space_id = :space_id
                GROUP BY trace_id)
            SELECT t.trace_id, t.conversation_id, coalesce(t.asked_at, t.collected_at) AS asked_at,
                   t.question, t.access_status, {verdict_sql} AS verdict, coalesce(r.fails, array()) AS fails
            FROM {prefix}.traces t LEFT JOIN r USING (trace_id)
            WHERE {" AND ".join(filters)}
            ORDER BY asked_at DESC, t.trace_id DESC
            LIMIT {fetch}""",
        params,
    )
    items = [
        TraceListItem(
            trace_id=r["trace_id"],
            conversation_id=r["conversation_id"],
            asked_at=r["asked_at"],
            question=r["question"],
            access_status=r["access_status"],
            verdict=r["verdict"],
            failed_criteria=sorted(r["fails"] or []),
        )
        for r in rows[:limit]
    ]
    next_cursor = _encode_cursor(items[-1].asked_at, items[-1].trace_id) if len(rows) > limit else None
    return Page[TraceListItem](items=items, next_cursor=next_cursor)


async def trace_space(trace_id: str, *, sql: SqlClient, prefix: str) -> str:
    """The space a trace belongs to, or `NotFound`."""
    rows = await sql.execute(
        f"SELECT space_id FROM {prefix}.traces WHERE trace_id = :trace_id", {"trace_id": trace_id}
    )
    if not rows:
        raise NotFound("Not found.")
    return rows[0]["space_id"]


async def get_trace(trace_id: str, space_id: str, *, sql: SqlClient, prefix: str) -> TraceDetail:
    """Trace + steps + its current results. The caller has already checked `space_id`."""
    params = {"trace_id": trace_id, "space_id": space_id}
    traces = await sql.execute(
        f"SELECT * FROM {prefix}.traces WHERE trace_id = :trace_id AND space_id = :space_id", params
    )
    if not traces:
        raise NotFound("Not found.")
    steps = await sql.execute(
        f"SELECT * FROM {prefix}.steps WHERE trace_id = :trace_id AND space_id = :space_id ORDER BY step_index",
        params,
    )
    results = await sql.execute(
        f"""SELECT * FROM {prefix}.current_results
            WHERE trace_id = :trace_id AND space_id = :space_id
            ORDER BY criterion_id, step_index""",
        params,
    )
    for row in steps:
        row.pop("space_id", None)
        row["result_schema"] = json.loads(row["result_schema"]) if row.get("result_schema") else None
        row["result_rows"] = json.loads(row["result_rows"]) if row.get("result_rows") else None
    for row in results:
        row["evidence"] = json.loads(row["evidence"]) if row.get("evidence") else {}
    return TraceDetail(
        trace=Trace.model_validate(traces[0]),
        steps=[Step.model_validate(s) for s in steps],
        results=[CheckResult.model_validate(r) for r in results],
    )


async def judge_calibration(
    space_id: str, *, sql: SqlClient, prefix: str, min_lower_bound: float
) -> JudgeCalibration:
    """Review progress in the space's current session, and the latest agreement rows.

    Labels reach Delta only when the review job runs, so `labeled` lags the Review App.
    """
    params = {"space_id": space_id}
    progress = await sql.execute(
        f"""WITH cur AS (
                SELECT max_by(session_id, queued_at) AS session_id FROM {prefix}.review_items
                WHERE space_id = :space_id)
            SELECT count(DISTINCT i.trace_id) AS queued, count(DISTINCT l.trace_id) AS labeled
            FROM {prefix}.review_items i
            LEFT JOIN {prefix}.human_labels l ON l.space_id = i.space_id AND l.trace_id = i.trace_id
            WHERE i.space_id = :space_id AND i.session_id = (SELECT session_id FROM cur)""",
        params,
    )
    rows = await sql.execute(
        f"""SELECT * FROM {prefix}.judge_agreement
            WHERE space_id = :space_id
              AND computed_at = (SELECT max(computed_at) FROM {prefix}.judge_agreement WHERE space_id = :space_id)
            ORDER BY criterion_id, criterion_version, judge""",
        params,
    )
    rows = [normalized(r) for r in rows]  # rows from before 2026-10-27 counted fail as positive
    for row in rows:
        row["n_by_reason"] = json.loads(row["n_by_reason"]) if row.get("n_by_reason") else {}
        for added in ("n_applies", "applicability_agree"):  # NULL in rows written before they existed
            if row.get(added) is None:
                row.pop(added, None)
    counts = progress[0] if progress else {}
    # Latest verdict per turn across reviewers; "caught" = any check fails it now.
    reviews = await sql.execute(
        f"""WITH tr AS (
                SELECT trace_id, max_by(task_success, updated_at) AS task_success,
                       max_by(task_rationale, updated_at) AS task_rationale,
                       max_by(findings, updated_at) AS findings
                FROM {prefix}.trace_reviews WHERE space_id = :space_id AND task_success IS NOT NULL
                GROUP BY trace_id),
            r AS (
                SELECT trace_id, max(CASE WHEN verdict = 'fail' THEN 1 ELSE 0 END) AS caught
                FROM {prefix}.current_results WHERE space_id = :space_id
                GROUP BY trace_id)
            SELECT tr.*, t.question, t.conversation_id, t.turn_index, coalesce(r.caught, 0) AS caught
            FROM tr JOIN {prefix}.traces t ON t.trace_id = tr.trace_id AND t.space_id = :space_id
            LEFT JOIN r ON r.trace_id = tr.trace_id
            ORDER BY t.asked_at DESC""",
        params,
    )
    observed = {
        r["criterion_id"]: r
        for r in await sql.execute(
            f"""SELECT criterion_id, count_if(verdict IN ('pass', 'fail')) AS applies,
                       count_if(verdict = 'fail') AS fails
                FROM {prefix}.current_results c
                JOIN {prefix}.traces t ON t.trace_id = c.trace_id AND t.space_id = :space_id
                WHERE c.space_id = :space_id AND c.family = 'rubric'
                GROUP BY criterion_id""",
            params,
        )
    }
    failed = [r for r in reviews if r["task_success"] == "fail"]
    by_conversation: dict[str, list[dict]] = {}
    for r in reviews:
        by_conversation.setdefault(r.get("conversation_id") or r["trace_id"], []).append(r)
    turns = [sorted(rs, key=lambda r: r.get("turn_index") or 0) for rs in by_conversation.values()]
    return JudgeCalibration(
        space_id=space_id,
        min_lower_bound=min_lower_bound,
        queued=counts.get("queued") or 0,
        labeled=counts.get("labeled") or 0,
        computed_at=rows[0]["computed_at"] if rows else None,
        reviewed=len(reviews),
        task_failed=len(failed),
        caught=sum(1 for r in failed if r["caught"]),
        blind_spots=[
            BlindSpot(
                trace_id=r["trace_id"],
                question=r["question"],
                task_rationale=r["task_rationale"],
                findings=r["findings"],
            )
            for r in failed
            if not r["caught"]
        ],
        conversations=len(turns),
        multi_turn=sum(1 for rs in turns if len(rs) > 1),
        conversations_strict=sum(1 for rs in turns if all(r["task_success"] == "pass" for r in rs)),
        conversations_outcome=sum(1 for rs in turns if rs[-1]["task_success"] == "pass"),
        judges=[judge_row(Agreement.model_validate(r), observed.get(r["criterion_id"], {})) for r in rows],
    )


def corrected_rate(observed: float, tpr: float | None, tnr: float | None) -> float | None:
    """Rogan-Gladen: the true fail rate implied by a judge's observed fail rate. With TPR = good
    answers passed and TNR = bad answers failed, the true pass rate is
    θ = (p_obs + TNR − 1) / (TPR + TNR − 1) with p_obs = 1 − observed; this returns 1 − θ.

    A judge that misses failures (low TNR) under-reports them; one that flags good answers (low
    TPR) over-reports. Undefined unless both are measured and the judge beats chance."""
    if tpr is None or tnr is None or tpr + tnr <= 1:
        return None
    return min(1.0, max(0.0, (observed + tpr - 1) / (tpr + tnr - 1)))


def judge_row(agreement: Agreement, observed: dict) -> JudgeRow:
    applies, fails = observed.get("applies") or 0, observed.get("fails") or 0
    return JudgeRow(
        **agreement.model_dump(),
        observed_applies=applies,
        observed_fails=fails,
        corrected_fail_rate=corrected_rate(fails / applies, agreement.tpr, agreement.tnr)
        if applies
        else None,
    )


async def observations(space_id: str, *, sql: SqlClient, prefix: str) -> Observations:
    """Reviewed turns a reviewer failed or wrote findings on, with the criteria that failed them
    in the current verdicts. Failed before passed-with-findings, uncaught before caught."""
    params = {"space_id": space_id}
    rows = await sql.execute(
        f"""WITH tr AS (
                SELECT trace_id, max_by(task_success, updated_at) AS task_success,
                       max_by(task_rationale, updated_at) AS task_rationale,
                       max_by(findings, updated_at) AS findings,
                       max_by(reviewer, updated_at) AS reviewer
                FROM {prefix}.trace_reviews WHERE space_id = :space_id
                GROUP BY trace_id),
            r AS (
                SELECT trace_id, collect_set(CASE WHEN verdict = 'fail' THEN criterion_id END) AS caught_by
                FROM {prefix}.current_results WHERE space_id = :space_id
                GROUP BY trace_id)
            SELECT tr.*, t.question, t.turn_index, t.asked_at, coalesce(r.caught_by, array()) AS caught_by,
                   s.category AS suggested_category, s.description AS suggested_description,
                   s.reason AS suggested_reason
            FROM tr JOIN {prefix}.traces t ON t.trace_id = tr.trace_id AND t.space_id = :space_id
            LEFT JOIN r ON r.trace_id = tr.trace_id
            LEFT JOIN {prefix}.observation_suggestions s ON s.space_id = :space_id AND s.trace_id = tr.trace_id
            WHERE tr.task_success = 'fail' OR tr.findings IS NOT NULL
            ORDER BY tr.task_success = 'fail' DESC, size(coalesce(r.caught_by, array())), t.asked_at DESC""",
        params,
    )
    counted = await sql.execute(
        f"""WITH tr AS (
                SELECT trace_id, max_by(task_success, updated_at) AS task_success
                FROM {prefix}.trace_reviews WHERE space_id = :space_id AND task_success IS NOT NULL
                GROUP BY trace_id)
            SELECT count(*) AS reviewed, count_if(task_success = 'pass') AS passed FROM tr""",
        params,
    )
    totals = counted[0] if counted else {}
    return Observations(
        space_id=space_id,
        reviewed=totals.get("reviewed") or 0,
        passed=totals.get("passed") or 0,
        items=[
            Observation.model_validate(
                {
                    **r,
                    "suggestion": Suggestion(
                        category=r["suggested_category"],
                        description=r.get("suggested_description") or "",
                        reason=r.get("suggested_reason") or "",
                    )
                    if r.get("suggested_category")
                    else None,
                }
            )
            for r in rows
        ],
    )


async def false_alarms(
    space_id: str, criterion_ids: list[str], *, sql: SqlClient, prefix: str
) -> dict[str, int]:
    """Per criterion: turns reviewers passed (latest review) that it fails now."""
    if not criterion_ids:
        return {}
    rows = await sql.execute(
        f"""WITH tr AS (
                SELECT trace_id, max_by(task_success, updated_at) AS task_success
                FROM {prefix}.trace_reviews WHERE space_id = :space_id GROUP BY trace_id)
            SELECT c.criterion_id, count(DISTINCT c.trace_id) AS false_alarms
            FROM {prefix}.current_results c JOIN tr ON tr.trace_id = c.trace_id AND tr.task_success = 'pass'
            WHERE c.space_id = :space_id AND c.verdict = 'fail'
              AND array_contains(from_json(:criterion_ids, 'ARRAY<STRING>'), c.criterion_id)
            GROUP BY c.criterion_id""",
        {"space_id": space_id, "criterion_ids": json.dumps(criterion_ids)},
    )
    return {r["criterion_id"]: r["false_alarms"] for r in rows}
