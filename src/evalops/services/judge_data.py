"""Human labels as a judge dataset, split by time.

Each example is the judge's exact input for a labelled turn plus the reviewer's label. The
older share is the dev set a judge is changed against; the newer share is the holdout it is
accepted on. Splitting by time, not at random, mirrors use: a judge tuned on today's labels
must hold up on tomorrow's.
"""

import math

from evalops.schemas.checks import Criterion
from evalops.schemas.review import HumanLabel
from evalops.schemas.trace import Step, Trace
from evalops.services.checks.llm_checks import judge_input


def examples_from_labels(
    labels: list[HumanLabel], loaded: list[tuple[Trace, list[Step]]], criterion: Criterion
) -> list[dict]:
    """One example per labelled turn of this criterion version (the latest label across
    reviewers), oldest first. Labels given under another rule text are left out, and so are
    turns without a report (the judge never sees them)."""
    by_trace = {t.trace_id: (t, steps) for t, steps in loaded}
    latest: dict[str, HumanLabel] = {}
    for label in sorted(labels, key=lambda h: h.labeled_at):
        if label.criterion_id == criterion.id and label.criterion_version == criterion.version:
            latest[label.trace_id] = label
    examples = []
    for trace_id, label in latest.items():
        if trace_id not in by_trace or not (by_trace[trace_id][0].report_text or "").strip():
            continue
        trace, steps = by_trace[trace_id]
        examples.append(
            {
                "name": trace_id,
                "input": judge_input(criterion, trace, steps, loaded).model_dump(),
                "expected": label.label,
                "rationale": label.rationale or "",
                "labeled_at": label.labeled_at.isoformat(),
            }
        )
    return sorted(examples, key=lambda e: e["labeled_at"])


def time_split(examples: list[dict], holdout_share: float = 0.3) -> tuple[list[dict], list[dict]]:
    """(dev, holdout): the newest `holdout_share` of the examples (at least one when there are
    two or more) is held out."""
    ordered = sorted(examples, key=lambda e: e["labeled_at"])
    if len(ordered) < 2:
        return ordered, []
    n_holdout = max(1, math.ceil(len(ordered) * holdout_share))
    return ordered[:-n_holdout], ordered[-n_holdout:]
