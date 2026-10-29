"""Where a judge and the reviewers disagree, and why.

A disagreement is a labelled (turn, criterion) whose current judge verdict differs from the
reviewer's label for the same criterion version. The owner marks its cause, which decides the
fix: `judge_wrong` (change the prompt or its examples), `rule_unclear` (rewrite the rule: a new
criterion version), `label_wrong` (relabel in the Review App).
"""

import json

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import NotFound
from evalops.schemas.api import CauseUpdate, Disagreement, Disagreements
from evalops.services.review.agreement import judge_label

ORDER = {"missed_failure": 0, "false_alarm": 1, "applicability": 2}


def kind(label: str, verdict: str) -> str:
    if label == "fail":
        return "missed_failure"
    if verdict == "fail" and label == "pass":
        return "false_alarm"
    return "applicability"  # one of them said na, the other did not


async def _rows(space_id: str, *, sql: SqlClient, prefix: str) -> list[dict]:
    return await sql.execute(
        f"""WITH r AS (
                SELECT trace_id, criterion_id, criterion_version, verdict, evidence
                FROM {prefix}.current_results
                WHERE space_id = :space_id AND family = 'rubric' AND verdict <> 'error')
            SELECT l.trace_id, l.criterion_id, l.criterion_version, l.reviewer, l.label, l.rationale,
                   r.verdict, r.evidence, t.question, t.asked_at
            FROM {prefix}.human_labels l
            JOIN r ON r.trace_id = l.trace_id AND r.criterion_id = l.criterion_id
                  AND r.criterion_version = l.criterion_version
            JOIN {prefix}.traces t ON t.trace_id = l.trace_id AND t.space_id = :space_id
            WHERE l.space_id = :space_id AND l.label <> r.verdict
            ORDER BY t.asked_at DESC""",
        {"space_id": space_id},
    )


async def disagreements(space_id: str, *, sql: SqlClient, store: StateStore, prefix: str) -> Disagreements:
    causes = await store.get_causes(space_id)
    items = []
    for r in await _rows(space_id, sql=sql, prefix=prefix):
        evidence = json.loads(r["evidence"]) if r.get("evidence") else {}
        marked = causes.get((r["trace_id"], r["criterion_id"]), {})
        items.append(
            Disagreement(
                trace_id=r["trace_id"],
                question=r.get("question"),
                criterion_id=r["criterion_id"],
                criterion_version=r["criterion_version"],
                reviewer=r["reviewer"],
                label=r["label"],
                rationale=r.get("rationale"),
                verdict=r["verdict"],
                judge=judge_label(evidence),
                judge_reason=evidence.get("reason") or "",
                votes=evidence.get("votes") or [],
                kind=kind(r["label"], r["verdict"]),
                cause=marked.get("cause"),
                note=marked.get("note") or "",
            )
        )
    items.sort(key=lambda d: ORDER[d.kind])  # stable: newest first within a kind
    return Disagreements(space_id=space_id, items=items)


async def mark(
    space_id: str,
    trace_id: str,
    criterion_id: str,
    body: CauseUpdate,
    user: str,
    *,
    sql: SqlClient,
    store: StateStore,
    prefix: str,
) -> Disagreement:
    """Only a current disagreement can be marked, so a cause always refers to one the owner saw."""
    found = await disagreements(space_id, sql=sql, store=store, prefix=prefix)
    item = next((d for d in found.items if (d.trace_id, d.criterion_id) == (trace_id, criterion_id)), None)
    if item is None:
        raise NotFound("No current disagreement for this turn and criterion.")
    await store.put_cause(space_id, trace_id, criterion_id, body.cause, body.note.strip(), item.judge, user)
    return item.model_copy(update={"cause": body.cause, "note": body.note.strip() if body.cause else ""})
