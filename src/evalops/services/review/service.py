"""One review pass for a space: pull labels and measure the judge, then queue new traces.

Pull runs first, so a trace labelled since the last pass gets its judge verdict attached and
counts in the agreement before anything new is queued.
"""

import asyncio
import hashlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from evalops.clients.lakebase import StateStore
from evalops.schemas.checks import CheckResult, Criterion, Verdict
from evalops.schemas.review import Agreement, HumanLabel, ReviewItem, TraceReview
from evalops.services.delta_store import DeltaWriter
from evalops.services.review.agreement import compute_agreement
from evalops.services.review.backend import (
    FINDINGS,
    TASK_SUCCESS,
    VIEW_VERSION,
    Assessment,
    ReviewBackend,
    turn_view,
)
from evalops.services.review.context import asked_line
from evalops.services.review.selection import parse_schema_name, schema_name, select_queue

logger = logging.getLogger(__name__)
LABELS = {"pass", "fail", "na"}


@dataclass
class ReviewStats:
    space_id: str
    reviewers: int = 0
    labels: int = 0
    trace_reviews: int = 0
    judge_attached: int = 0
    queued: int = 0
    session_url: str = ""
    agreement: list[Agreement] | None = None


def human_labels(
    assessments: list[Assessment],
    items: dict[str, ReviewItem],
    criteria: dict[str, Criterion],
    space_id: str,
) -> list[HumanLabel]:
    """The latest human label per (trace, criterion, reviewer) on traces we queued.

    Assessments on traces we did not queue, judge assessments and values outside
    pass/fail/na are ignored.
    """
    latest: dict[tuple[str, str, str], Assessment] = {}
    for a in sorted(assessments, key=lambda a: a.created_ms):
        parsed = parse_schema_name(a.name)
        if a.source_type != "HUMAN" or a.mlflow_trace_id not in items or parsed is None:
            continue
        if parsed[0] not in criteria or str(a.value).lower() not in LABELS:
            continue
        latest[(a.mlflow_trace_id, a.name, a.source_id)] = a
    return [
        HumanLabel(
            space_id=space_id,
            trace_id=items[mid].trace_id,
            mlflow_trace_id=mid,
            criterion_id=parse_schema_name(name)[0],
            criterion_version=parse_schema_name(name)[1],
            reviewer=reviewer,
            label=str(a.value).lower(),
            rationale=a.rationale,
            assessment_id=a.assessment_id,
            labeled_at=datetime.fromtimestamp(a.created_ms / 1000, tz=UTC),
        )
        for (mid, name, reviewer), a in latest.items()
    ]


# The Review App counts a turn as done only when every question has an answer, so reviewers
# type one of these into the "optional" findings box when they have nothing to add.
NO_FINDINGS = {"none", "no", "nothing", "n/a", "na", "-", "nope", "nothing to add", "无", "没有"}


def question_kind(name: str) -> str:
    """`task_success__v2` -> `task_success`."""
    return name.rpartition("__v")[0] or name


def trace_reviews(
    assessments: list[Assessment], items: dict[str, ReviewItem], space_id: str
) -> list[TraceReview]:
    """One row per (trace, reviewer) from the turn-level questions, latest answer of each.

    Any version of a question counts (a new version only rewords its instruction), so an
    answer given under an older session is kept. A reviewer who wrote findings but has not
    decided task success yet still gets a row.
    """
    kinds = {question_kind(TASK_SUCCESS), question_kind(FINDINGS)}
    latest: dict[tuple[str, str, str], Assessment] = {}
    for a in sorted(assessments, key=lambda a: a.created_ms):
        kind = question_kind(a.name)
        if a.source_type == "HUMAN" and a.mlflow_trace_id in items and kind in kinds:
            latest[(a.mlflow_trace_id, a.source_id, kind)] = a
    reviews: dict[tuple[str, str], TraceReview] = {}
    for (mid, reviewer, name), a in latest.items():
        review = reviews.setdefault(
            (mid, reviewer),
            TraceReview(
                space_id=space_id,
                trace_id=items[mid].trace_id,
                mlflow_trace_id=mid,
                reviewer=reviewer,
                updated_at=datetime.fromtimestamp(a.created_ms / 1000, tz=UTC),
            ),
        )
        review.updated_at = max(review.updated_at, datetime.fromtimestamp(a.created_ms / 1000, tz=UTC))
        value = str(a.value).strip() if a.value is not None else ""
        if name == question_kind(TASK_SUCCESS) and value.lower() in ("pass", "fail"):
            review.task_success, review.task_rationale = value.lower(), a.rationale
            review.guideline = a.name
        elif name == question_kind(FINDINGS) and value.lower().strip(" .") not in NO_FINDINGS | {""}:
            review.findings = value
    return list(reviews.values())


def judge_to_attach(
    labels: list[HumanLabel], assessments: list[Assessment], results: list[CheckResult]
) -> list[tuple[HumanLabel, CheckResult]]:
    """Judge verdicts to reveal: only for (trace, criterion) a human has labelled (blind
    review), only once (not if a judge assessment of that name is already there), only for
    the rule version the human saw, and never an error verdict."""
    revealed = {(a.mlflow_trace_id, a.name) for a in assessments if a.source_type == "LLM_JUDGE"}
    verdicts = {(r.trace_id, r.criterion_id): r for r in results if r.verdict != Verdict.ERROR}
    out, seen = [], set()
    for label in labels:
        key = (label.mlflow_trace_id, schema_name(label.criterion_id, label.criterion_version))
        result = verdicts.get((label.trace_id, label.criterion_id))
        if result is None or result.criterion_version != label.criterion_version:
            continue
        if key in revealed or key in seen:
            continue
        seen.add(key)
        out.append((label, result))
    return out


def session_name(schemas: list[str], view: str = VIEW_VERSION) -> str:
    """Stable per set of label schemas and trace view: a session's schemas cannot change after
    creation, and a new view re-logs the traces, which belong in a fresh session."""
    key = ",".join(sorted(schemas)) + f"|view={view}"
    return "evalops-review-" + hashlib.sha256(key.encode()).hexdigest()[:8]


async def review_space(
    space_id: str,
    *,
    delta: DeltaWriter,
    store: StateStore,
    backend: ReviewBackend,
    criteria: list[Criterion],
    budget: int,
    min_lower_bound: float,
    now: datetime,
    load_context: Callable[[str], Awaitable[str]] | None = None,
    timezone: str = "UTC",
    seed: int | None = None,
) -> ReviewStats:
    stats = ReviewStats(space_id)
    by_id = {c.id: c for c in criteria}
    results = await delta.load_current_results(space_id, list(by_id))
    experiment_id = await asyncio.to_thread(backend.ensure_experiment, space_id)

    # --- pull
    items = {i.mlflow_trace_id: i for i in await delta.load_review_items(space_id)}
    assessments = await asyncio.to_thread(backend.assessments, experiment_id)
    labels = human_labels(assessments, items, by_id, space_id)
    await delta.merge_human_labels(labels)
    stats.labels = len(labels)
    reviews = trace_reviews(assessments, items, space_id)
    await delta.merge_trace_reviews(reviews)
    stats.trace_reviews = len(reviews)
    for label, result in judge_to_attach(labels, assessments, results):
        reason = result.evidence.get("reason") or ""
        votes = result.evidence.get("votes")
        rationale = f"{reason}\n\nvotes: {votes}" if votes else reason
        source = f"{result.evidence.get('skill')}|{result.evidence.get('model')}"
        await asyncio.to_thread(
            backend.log_judge,
            label.mlflow_trace_id,
            schema_name(label.criterion_id, label.criterion_version),
            result.verdict.value,
            rationale,
            source,
        )
        stats.judge_attached += 1
    all_labels = await delta.load_human_labels(space_id)
    reasons = {i.trace_id: i.reason for i in items.values()}
    stats.agreement = compute_agreement(
        all_labels, results, reasons, space_id=space_id, now=now, min_lower_bound=min_lower_bound
    )
    await delta.write_agreement(stats.agreement)

    # --- push
    reviewers = await store.get_reviewers(space_id)
    stats.reviewers = len(reviewers)
    if not reviewers:
        logger.warning("space %s has no registered reviewers: none opened the app yet", space_id)
        return stats
    await asyncio.to_thread(backend.set_reviewers, experiment_id, reviewers)
    schemas = await asyncio.to_thread(backend.ensure_label_schemas, experiment_id, criteria)
    session_id, url = await asyncio.to_thread(
        backend.ensure_session, experiment_id, session_name(schemas), reviewers, schemas
    )
    await store.put_review_session(space_id, experiment_id, session_id, url)
    stats.session_url = url
    queued_traces = {i.trace_id for i in items.values() if i.session_id == session_id}
    gradable = [r for r in results if r.evidence.get("reason") != "no report to judge"]  # nothing to label
    chosen = select_queue(gradable, queued_traces, budget, random.Random(seed))
    if not chosen:
        return stats
    loaded = {t.trace_id: (t, steps) for t, steps in await delta.load_traces(space_id)}
    logged = await asyncio.to_thread(backend.find_traces, experiment_id)
    context = await load_context(space_id) if load_context else ""
    new_items = []
    for trace_id, reason in chosen:
        if trace_id not in loaded:
            continue
        trace, steps = loaded[trace_id]
        mlflow_id = logged.get(trace_id)
        if mlflow_id is None:
            earlier = [
                (t, t_steps)
                for t, t_steps in loaded.values()
                if t.conversation_id == trace.conversation_id and t.turn_index < trace.turn_index
            ]
            view = turn_view(
                trace, steps, earlier, context=context, asked=asked_line(trace.asked_at, timezone)
            )
            mlflow_id = await asyncio.to_thread(backend.log_turn, experiment_id, view)
        new_items.append(
            ReviewItem(
                space_id=space_id,
                trace_id=trace_id,
                mlflow_trace_id=mlflow_id,
                session_id=session_id,
                reason=reason,
                queued_at=now,
            )
        )
    await asyncio.to_thread(
        backend.add_to_session, experiment_id, session_id, [i.mlflow_trace_id for i in new_items]
    )
    await delta.merge_review_items(new_items)
    stats.queued = len(new_items)
    return stats
