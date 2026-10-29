"""Judge-human agreement: TPR and TNR with Wilson lower bounds, as the AI Evals Playbook defines
them: the positive class is `pass`, so TPR = good answers the judge passed and TNR = bad answers
the judge failed (the Rogan-Gladen correction uses the same definitions). The minority class is
`fail`: its recall is TNR and its precision is reported beside it.

Why lower bounds: 10 agreements out of 10 sounds perfect, but its 95% Wilson lower bound is
0.72. A criterion counts as calibrated only when BOTH bounds reach the threshold (default
0.80, which needs at least 16 fails and 16 passes labelled with no disagreement).

Only labels where the reviewer said the rule applies (`pass` or `fail`) count towards TPR, TNR
and kappa: most turns are `na` for any one rule, and a judge that agrees "does not apply" on
them would otherwise look excellent at leaving good answers alone. Whether the judge agrees
the rule applies at all is reported separately (applicability).
"""

import math
import re
from collections import Counter
from datetime import datetime

from evalops.schemas.checks import CheckResult, Verdict
from evalops.schemas.review import Agreement, HumanLabel, QueueReason

Z95 = 1.959964


def wilson_lower_bound(successes: int, n: int, z: float = Z95) -> float | None:
    if n == 0:
        return None
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / (1 + z * z / n)


def cohen_kappa(tp: int, fn: int, fp: int, tn: int) -> float | None:
    """Kappa for two raters on a binary label: (observed - chance) / (1 - chance).

    A judge that passes everything agrees 95% of the time when 95% of answers are good, yet
    its kappa is 0. Undefined (None) when chance agreement is 1, e.g. both raters gave only
    "not fail", which is every early sample of a rare failure.
    """
    n = tp + fn + fp + tn
    if n == 0:
        return None
    observed = (tp + tn) / n
    chance = ((tp + fn) / n) * ((tp + fp) / n) + ((tn + fp) / n) * ((tn + fn) / n)
    return None if chance == 1 else (observed - chance) / (1 - chance)


POSITIVE = "pass"


def normalized(row: dict) -> dict:
    """A stored agreement row in today's definitions. Rows computed before 2026-10-27 have no
    `positive_class` and used fail as the positive class: swap their counts and rates."""
    if row.get("positive_class") == POSITIVE:
        return row
    swapped = dict(row)
    for a, b in (("tp", "tn"), ("fn", "fp"), ("tpr", "tnr"), ("tpr_lb", "tnr_lb")):
        swapped[a], swapped[b] = row.get(b), row.get(a)
    swapped["positive_class"] = POSITIVE
    return swapped


def score(pairs: list[tuple[str, str]]) -> dict:
    """Agreement counts and rates from (human label, judge verdict) pairs of pass/fail/na.

    TPR, TNR and kappa count only pairs where the human said the rule applies. The judge "passes"
    an answer unless it fails it, so a judge `na` on a human `fail` is a missed failure. Shared by
    the review job and offline judge experiments, so both report the same numbers.
    """
    applies = [(h, j) for h, j in pairs if h != "na"]
    tp = sum(h == "pass" and j != "fail" for h, j in applies)  # good answer, judge passed it
    fn = sum(h == "pass" and j == "fail" for h, j in applies)  # good answer, judge failed it
    tn = sum(h == "fail" and j == "fail" for h, j in applies)  # bad answer, judge failed it
    fp = len(applies) - tp - fn - tn  # bad answer, judge let it through
    return {
        "positive_class": POSITIVE,
        "n": len(pairs),
        "n_applies": len(applies),
        "tp": tp,
        "fn": fn,
        "fp": fp,
        "tn": tn,
        "exact": sum(h == j for h, j in pairs),
        "applicability_agree": sum((h == "na") == (j == "na") for h, j in pairs),
        "tpr": tp / (tp + fn) if tp + fn else None,
        "tnr": tn / (tn + fp) if tn + fp else None,
        "tpr_lb": wilson_lower_bound(tp, tp + fn),
        "tnr_lb": wilson_lower_bound(tn, tn + fp),
        "kappa": cohen_kappa(tp, fn, fp, tn),
    }


def judge_key(result: CheckResult) -> str:
    """Everything that can change a judge's verdict, as one label for grouping."""
    return judge_label(result.evidence)


def judge_label(evidence: dict) -> str:
    """`<skill>@<version>|<model>|<sampling settings>` from a judge verdict's evidence."""
    params = ",".join(f"{k}={v}" for k, v in sorted((evidence.get("model_params") or {}).items()))
    return f"{evidence.get('skill')}|{evidence.get('model')}|{params}"


def judge_fingerprint(skill_ref: str, model: str, model_params: dict) -> str:
    """The judge a calibration measured: prompt (skill and version), model and sampling settings."""
    return judge_label({"skill": skill_ref, "model": model, "model_params": model_params})


_SYNTHETIC = re.compile(r"\|\d+ synthetic labels$")


def same_judge(recorded: str | None, current: str | None) -> bool:
    """Was this calibration measured on the current judge? (A note on synthetic labels aside.)"""
    return bool(recorded and current) and _SYNTHETIC.sub("", recorded) == current


def compute_agreement(
    labels: list[HumanLabel],
    results: list[CheckResult],
    reasons: dict[str, QueueReason],
    *,
    space_id: str,
    now: datetime,
    min_lower_bound: float,
) -> list[Agreement]:
    """One row per (criterion, criterion version, judge) that has labels to compare with.

    `results` are the current verdicts; a label is compared only with a verdict of the same
    criterion version, because the rule text it was given may have changed since.
    """
    verdicts = {(r.trace_id, r.criterion_id): r for r in results}
    groups: dict[tuple[str, int, str], list[tuple[HumanLabel, CheckResult]]] = {}
    for label in labels:
        result = verdicts.get((label.trace_id, label.criterion_id))
        if result is None or result.criterion_version != label.criterion_version:
            continue
        groups.setdefault((label.criterion_id, label.criterion_version, judge_key(result)), []).append(
            (label, result)
        )
    rows = []
    for (cid, version, judge), pairs in sorted(groups.items()):
        graded = [(h, r) for h, r in pairs if r.verdict != Verdict.ERROR]
        m = score([(h.label, r.verdict.value) for h, r in graded])
        rows.append(
            Agreement(
                computed_at=now,
                space_id=space_id,
                criterion_id=cid,
                criterion_version=version,
                judge=judge,
                n_judge_error=len(pairs) - len(graded),
                calibrated=m["tpr_lb"] is not None
                and m["tnr_lb"] is not None
                and min(m["tpr_lb"], m["tnr_lb"]) >= min_lower_bound,
                **m,
                n_by_reason=dict(Counter(str(reasons.get(h.trace_id, "unknown")) for h, _ in pairs)),
            )
        )
    return rows
