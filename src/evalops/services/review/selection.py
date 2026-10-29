"""Which traces go to human review, and how a criterion is named in MLflow."""

import random
from collections import defaultdict

from evalops.schemas.checks import CheckResult, Verdict
from evalops.schemas.review import QueueReason


def schema_name(criterion_id: str, version: int) -> str:
    """`rubric.x` v2 -> `rubric__x__v2`: MLflow label schema names allow only letters, digits
    and `_`, and a schema used by a session cannot be changed, so a new rule version needs a
    new name. The judge's feedback is logged under the same name (`align()` pairs by name).
    """
    return f"{criterion_id.replace('.', '__')}__v{version}"


def parse_schema_name(name: str) -> tuple[str, int] | None:
    """`rubric__x__v2` -> ("rubric.x", 2); None for a name we did not make."""
    base, sep, version = name.rpartition("__v")
    if not sep or not version.isdigit():
        return None
    return base.replace("__", "."), int(version)


def reason_for(results: list[CheckResult]) -> QueueReason:
    """A trace's queue reason from its rubric verdicts (one per rubric criterion)."""
    if any(r.verdict == Verdict.ERROR or r.evidence.get("agreement", 1) < 1 for r in results):
        return QueueReason.JUDGE_UNCERTAIN
    if any(r.verdict == Verdict.FAIL for r in results):
        return QueueReason.JUDGE_FAIL
    return QueueReason.RANDOM


def select_queue(
    results: list[CheckResult], already_queued: set[str], budget: int, rng: random.Random
) -> list[tuple[str, QueueReason]]:
    """Up to `budget` new traces, most informative first, with up to half the budget kept for
    a random sample: the random items are the only unbiased estimate of judge accuracy.
    """
    by_trace: dict[str, list[CheckResult]] = defaultdict(list)
    for r in results:
        if r.trace_id not in already_queued:
            by_trace[r.trace_id].append(r)
    reasons = {trace_id: reason_for(rs) for trace_id, rs in sorted(by_trace.items())}
    order = [QueueReason.JUDGE_UNCERTAIN, QueueReason.JUDGE_FAIL]
    priority = sorted(
        (t for t, why in reasons.items() if why in order), key=lambda t: order.index(reasons[t])
    )
    pool = [t for t, why in reasons.items() if why == QueueReason.RANDOM]
    rng.shuffle(pool)
    reserve = min(len(pool), budget // 2)
    chosen = priority[: budget - reserve]
    chosen += pool[: budget - len(chosen)]
    return [(t, reasons[t]) for t in chosen]
