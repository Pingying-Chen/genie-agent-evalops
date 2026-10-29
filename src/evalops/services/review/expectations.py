"""Calibrating `expectation_judge` against people (step 3b).

The judge decides whether a regression answer meets its eval case's expectations (the facts,
and asking back or declining when the case says so). Until people have checked it, it may only
report. So the review job queues a sample of regression answers, both judge verdicts, into a
labeling session of their own; the reviewer sees the question, the answer and the expectations
and answers one label: does the answer meet every expectation? Agreement is computed as for the
rubric judges, with judge pass = every expectation met.

A separate session: adding a label to the production session would change its schema set,
which (by design, see `session_name`) starts a new session and queues every production answer
again.
"""

import asyncio
import dataclasses
import json
import random
from datetime import UTC, datetime

from evalops.schemas.evalset import EvalCase
from evalops.schemas.manifest import Manifest
from evalops.schemas.review import Agreement, HumanLabel, QueueReason, ReviewItem
from evalops.schemas.trace import AccessStatus, Step, Trace
from evalops.services.evaluators import EXPECTATIONS_LABEL
from evalops.services.llm.skills import Skill
from evalops.services.review.agreement import judge_fingerprint, score
from evalops.services.review.backend import Assessment, turn_view

SCHEMA = f"{EXPECTATIONS_LABEL}__v1"
TITLE = "Does the answer meet every expectation listed above it?"
INSTRUCTION = (
    "The system message lists what a good answer to this question must state or do. "
    "pass = it meets every one; fail = it misses at least one (say which, and why). "
    "Judge only the last answer, and only against the list."
)
SESSION = "evalops-expectations-v1"
SYNTHETIC = "synthetic"  # reviewer name of made-up labels (sandbox only); shown on the judge
JUDGED = ("fact", "behaviour")


def judge_verdict(expectations: list[dict]) -> str | None:
    """The judge's verdict on one answer: fail if any judged expectation failed, pass if all
    passed, None if it judged nothing or could not decide one."""
    judged = [e for e in expectations if e.get("kind") in JUDGED]
    if not judged:
        return None
    if any(e.get("passed") is False for e in judged):
        return "fail"
    return "pass" if all(e.get("passed") is True for e in judged) else None


def judge_versions(manifests: list[Manifest]) -> dict[str, str]:
    """`{eval run id: expectation_judge version}` from the runs' manifests. Runs without a
    manifest (older runs) cannot say which judge graded them, so they are left out."""
    return {
        m.run_id: a.version
        for m in manifests
        for a in m.artifacts
        if a.kind == "skill" and a.name == "expectation_judge"
    }


def labels(assessments: list[Assessment], items: dict[str, ReviewItem], space_id: str) -> list[HumanLabel]:
    """The latest human pass/fail per (answer, reviewer) on expectation items we queued."""
    latest: dict[tuple[str, str], Assessment] = {}
    for a in sorted(assessments, key=lambda a: a.created_ms):
        if (
            a.source_type == "HUMAN"
            and a.name == SCHEMA
            and a.mlflow_trace_id in items
            and str(a.value).lower() in ("pass", "fail")
        ):
            latest[(a.mlflow_trace_id, a.source_id)] = a
    return [
        HumanLabel(
            space_id=space_id,
            trace_id=items[mid].trace_id,
            mlflow_trace_id=mid,
            criterion_id=EXPECTATIONS_LABEL,
            criterion_version=1,
            reviewer=reviewer,
            label=str(a.value).lower(),
            rationale=a.rationale,
            assessment_id=a.assessment_id,
            labeled_at=datetime.fromtimestamp(a.created_ms / 1000, tz=UTC),
        )
        for (mid, reviewer), a in latest.items()
    ]


def agreement(
    human: list[HumanLabel],
    attempts: dict[str, dict],
    versions: dict[str, str],
    *,
    current: str,
    judge: str,
    space_id: str,
    now: datetime,
    min_lower_bound: float,
) -> Agreement | None:
    """Pure: judge vs people on the answers graded by the current judge version. `attempts`:
    eval_results rows by trace id. A judge that could not decide an answer is a judge error."""
    pairs, errors, synthetic = [], 0, 0
    for label in human:
        row = attempts.get(label.trace_id)
        if label.criterion_id != EXPECTATIONS_LABEL or row is None or versions.get(row["run_id"]) != current:
            continue
        verdict = judge_verdict(json.loads(row.get("expectations") or "[]"))
        if verdict is None:
            errors += 1
            continue
        pairs.append((label.label, verdict))
        synthetic += label.reviewer == SYNTHETIC
    if not pairs and not errors:
        return None
    m = score(pairs)
    return Agreement(
        computed_at=now,
        space_id=space_id,
        criterion_id=EXPECTATIONS_LABEL,
        criterion_version=int(current) if current.isdigit() else 0,
        judge=judge + (f"|{synthetic} synthetic labels" if synthetic else ""),
        n_judge_error=errors,
        calibrated=m["tpr_lb"] is not None
        and m["tnr_lb"] is not None
        and min(m["tpr_lb"], m["tnr_lb"]) >= min_lower_bound,
        **m,
        n_by_reason={QueueReason.EXPECTATION_CHECK.value: len(pairs) + errors},
    )


def select(
    rows: list[dict],
    versions: dict[str, str],
    *,
    current: str,
    queued: set[str],
    budget: int,
    seed: int | None,
) -> list[dict]:
    """Up to `budget` answers graded by the current judge and not queued yet, half the judge
    failed and half it passed where possible (the fail class is the one a gate must catch)."""
    fresh: dict[str, dict] = {}
    for r in rows:
        if (
            r.get("trace_id")
            and r["trace_id"] not in queued
            and versions.get(r["run_id"]) == current
            and judge_verdict(json.loads(r.get("expectations") or "[]")) is not None
        ):
            fresh.setdefault(r["trace_id"], r)
    rng = random.Random(seed)
    by_verdict = {"fail": [], "pass": []}
    for r in fresh.values():
        by_verdict[judge_verdict(json.loads(r["expectations"]))].append(r)
    for group in by_verdict.values():
        rng.shuffle(group)
    half = budget // 2
    chosen = by_verdict["fail"][: max(half, budget - len(by_verdict["pass"]))]
    chosen += by_verdict["pass"][: budget - len(chosen)]
    return chosen


def view(row: dict, case: EvalCase | None, space_id: str):
    """The answer as the reviewer sees it: the expectations first, then the question (with the
    case's earlier turns), the answer and its steps."""
    expectations = json.loads(row.get("expectations") or "[]")
    listed = [
        e["name"] if e["kind"] == "fact" else f"Behaviour: {e['name']}"
        for e in expectations
        if e["kind"] in JUDGED
    ]
    if (
        case
        and case.expect.behaviour in ("clarify", "refuse")
        and not any(e["kind"] == "behaviour" for e in expectations)
    ):
        listed.insert(0, f"Behaviour: {case.expect.behaviour}")
    header = "Expectations for this answer (an eval case of this agent):\n" + "\n".join(
        f"{n}. {text}" for n, text in enumerate(listed, start=1)
    )
    turns = case.turns if case else [row.get("question") or ""]
    trace = Trace(
        trace_id=row["trace_id"],
        space_id=space_id,
        conversation_id=row.get("conversation_id") or row["trace_id"],
        turn_index=len(turns) - 1,
        question=row.get("question") or turns[-1],
        report_text=row.get("report_text") or "",
        access_status=AccessStatus.OK,
        parser_version="eval",
        collected_at=datetime.now(UTC),
    )
    earlier = [
        (
            trace.model_copy(
                update={
                    "trace_id": f"{trace.trace_id}-{i}",
                    "turn_index": i,
                    "question": q,
                    "report_text": "",
                }
            ),
            [],
        )
        for i, q in enumerate(turns[:-1])
    ]
    steps = [Step.model_validate(s) for s in json.loads(row.get("steps") or "[]")]
    turn = turn_view(trace, steps, earlier, context=header)
    return dataclasses.replace(
        turn,
        tags={**turn.tags, "evalops.kind": "expectations"},
        title=f"[eval case] {case.title if case else trace.question}"[:200],
    )


async def review_expectations(
    space_id: str,
    *,
    delta,
    store,
    backend,
    experiment_id: str,
    assessments: list[Assessment],
    reviewers: list[str],
    judge: Skill,
    model: str,
    budget: int,
    min_lower_bound: float,
    now: datetime,
    seed: int | None = None,
) -> tuple[Agreement | None, int]:
    """Pull labels and measure the judge, then queue more answers. Returns (agreement, queued)."""
    items = {
        i.mlflow_trace_id: i
        for i in await delta.load_review_items(space_id)
        if i.reason == QueueReason.EXPECTATION_CHECK
    }
    await delta.merge_human_labels(labels(assessments, items, space_id))
    attempts = {r["trace_id"]: r for r in await delta.load_judged_attempts(space_id)}
    versions = judge_versions(
        [Manifest.model_validate_json(r["manifest"]) for r in await delta.load_manifests(space_id, "eval")]
    )
    current = str(judge.version)
    found = agreement(
        [h for h in await delta.load_human_labels(space_id) if h.criterion_id == EXPECTATIONS_LABEL],
        attempts,
        versions,
        current=current,
        judge=judge_fingerprint(judge.ref, model, judge.model_params),
        space_id=space_id,
        now=now,
        min_lower_bound=min_lower_bound,
    )
    if found:
        await delta.write_agreement([found])
    if not reviewers or budget <= 0:
        return found, 0

    await asyncio.to_thread(backend.ensure_schema, experiment_id, SCHEMA, TITLE, INSTRUCTION)
    session_id, _ = await asyncio.to_thread(
        backend.ensure_session, experiment_id, SESSION, reviewers, [SCHEMA]
    )
    chosen = select(
        list(attempts.values()),
        versions,
        current=current,
        queued={i.trace_id for i in items.values()},
        budget=budget,
        seed=seed,
    )
    if not chosen:
        return found, 0
    history: dict[str, list[EvalCase]] = {}
    new_items = []
    for row in chosen:
        if row["case_id"] not in history:
            history[row["case_id"]] = [
                EvalCase.model_validate(c)
                for c in await store.get_eval_case_history(space_id, row["case_id"])
            ]
        case = next((c for c in history[row["case_id"]] if c.version == row["case_version"]), None)
        mlflow_id = await asyncio.to_thread(backend.log_turn, experiment_id, view(row, case, space_id))
        new_items.append(
            ReviewItem(
                space_id=space_id,
                trace_id=row["trace_id"],
                mlflow_trace_id=mlflow_id,
                session_id=session_id,
                reason=QueueReason.EXPECTATION_CHECK,
                queued_at=now,
            )
        )
    await asyncio.to_thread(
        backend.add_to_session, experiment_id, session_id, [i.mlflow_trace_id for i in new_items]
    )
    await delta.merge_review_items(new_items)
    return found, len(new_items)
