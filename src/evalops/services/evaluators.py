"""The evaluator registry.

Every verdict comes from an evaluator: a code criterion, an LLM judge, a deterministic check
of the case, or a person. An LLM judge may decide a gate only once it is calibrated against
people (both Wilson lower bounds at or above the threshold); until then it
reports. Code is deterministic: it decides, and where it was measured against hand labels the
measurement is shown with it.
"""

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.schemas.checks import Criterion, Family
from evalops.schemas.evaluator import Calibration, Evaluator, Evaluators
from evalops.services.review.agreement import normalized, same_judge, wilson_lower_bound

EXPECTATION_JUDGE = "expectation_judge"
EXPECTATIONS_LABEL = "expectations"  # judge_agreement.criterion_id of the expectation judge (3b)

CASE_CHECKS = {
    "data": "The answer queries data, or answers without querying, as the case requires.",
    "must_not_query": "The answer's SQL does not touch the columns or tables the case forbids.",
    "reference": "The reference query's values appear in the answer, each said of its own row and column.",
}


def evaluator_id_for(kind: str, name: str) -> str:
    """Which evaluator produced an expectation result of this kind (see grading.ExpectationKind)."""
    if kind == "rule":
        return f"criterion:{name}"
    if kind in ("fact", "behaviour"):
        return EXPECTATION_JUDGE
    return f"case_check:{kind}"


def calibration(row: dict | None) -> Calibration | None:
    if not row:
        return None
    row = normalized(row)
    tn, fn = int(row.get("tn") or 0), int(row.get("fn") or 0)
    return Calibration(
        computed_at=row.get("computed_at"),
        judge=row.get("judge"),
        n=int(row.get("n_applies") or row.get("n") or 0),
        tp=int(row.get("tp") or 0),
        fn=fn,
        fp=int(row.get("fp") or 0),
        tn=tn,
        tpr=row.get("tpr"),
        tpr_lb=row.get("tpr_lb"),
        tnr=row.get("tnr"),
        tnr_lb=row.get("tnr_lb"),
        # Of the answers the judge failed, the share people failed too (the minority class).
        precision=tn / (tn + fn) if tn + fn else None,
        precision_lb=wilson_lower_bound(tn, tn + fn),
        kappa=row.get("kappa"),
    )


def _calibrated(cal: Calibration | None, min_lower_bound: float) -> bool:
    return bool(
        cal
        and cal.tpr_lb is not None
        and cal.tnr_lb is not None
        and cal.tpr_lb >= min_lower_bound
        and cal.tnr_lb >= min_lower_bound
    )


OTHER_JUDGE = (
    "Its calibration measured another prompt, model or sampling setting: it reports until new labels "
    "measure this one."
)


def _measured(rows: list[dict], judge: str | None) -> tuple[Calibration | None, bool]:
    """The newest calibration of exactly this judge, and whether only other judges were measured."""
    mine = [r for r in rows if same_judge(r.get("judge"), judge)]
    return calibration(mine[0] if mine else None), bool(rows) and not mine


def registry(
    criteria: list[Criterion],
    agreement: dict[tuple[str, int], list[dict]],
    *,
    judges: dict[str, str],
    min_lower_bound: float,
    expectation_judge_version: str | None,
    reviewers: int,
) -> list[Evaluator]:
    """Pure. `agreement`: judge_agreement rows per (criterion id, version), newest first, one per
    judge; the expectation judge's under (`expectations`, its skill version). `judges`: the judge
    each one runs with now (see `Effective.judges`); only a calibration of that judge counts."""
    out = []
    for c in criteria:
        if c.family == Family.RUBRIC:
            cal, other = _measured(agreement.get((c.id, c.version), []), judges.get(c.id))
            ok = _calibrated(cal, min_lower_bound)
            out.append(
                Evaluator(
                    id=f"criterion:{c.id}",
                    kind="llm_judge",
                    name=c.title or c.id,
                    version=f"{c.id} v{c.version} · {c.params.get('skill', 'rubric_judge')}",
                    rubric=str(c.params.get("rule", c.description)).strip(),
                    status="calibrated" if ok else "uncalibrated",
                    can_decide=ok,
                    gate=c.gate.value,
                    calibration=cal,
                    note=""
                    if ok
                    else OTHER_JUDGE
                    if other
                    else "Not enough agreeing human labels yet: it reports, it does not decide.",
                )
            )
        else:
            out.append(
                Evaluator(
                    id=f"criterion:{c.id}",
                    kind="code",
                    name=c.title or c.id,
                    version=f"v{c.version}",
                    rubric=c.description.strip(),
                    status="deterministic",
                    can_decide=True,
                    gate=c.gate.value,
                    measured=c.measured,
                )
            )
    version = int(expectation_judge_version) if (expectation_judge_version or "").isdigit() else 0
    cal, other = _measured(agreement.get((EXPECTATIONS_LABEL, version), []), judges.get(EXPECTATIONS_LABEL))
    ok = _calibrated(cal, min_lower_bound)
    out.append(
        Evaluator(
            id=EXPECTATION_JUDGE,
            kind="llm_judge",
            name="Eval case expectations",
            version=f"{EXPECTATION_JUDGE}@{expectation_judge_version}" if expectation_judge_version else None,
            rubric="Whether an answer states or does each fact the case lists, and asks back or declines when the case says so.",
            status="calibrated" if ok else "uncalibrated",
            can_decide=ok,
            calibration=cal,
            note=""
            if ok
            else OTHER_JUDGE
            if other
            else "Not calibrated yet: its fails are reported in gate runs but cannot decide them.",
        )
    )
    out += [
        Evaluator(
            id=f"case_check:{kind}",
            kind="code",
            name=f"Case check: {kind.replace('_', ' ')}",
            rubric=text,
            status="deterministic",
            can_decide=True,
        )
        for kind, text in CASE_CHECKS.items()
    ]
    out.append(
        Evaluator(
            id="human:review",
            kind="human",
            name="Human review (Review App)",
            rubric="Reviewers label task success and each rubric rule; their labels calibrate the LLM judges.",
            status="human",
            can_decide=False,
            note=f"{reviewers} registered reviewer(s). Reviewer-reviewer agreement is on the Performance page.",
        )
    )
    return out


async def latest_agreement(
    space_id: str, *, sql: SqlClient, prefix: str
) -> dict[tuple[str, int], list[dict]]:
    """The newest agreement of every judge, per (criterion, version), newest first."""
    rows = await sql.execute(
        f"""SELECT * FROM {prefix}.judge_agreement WHERE space_id = :space_id
            QUALIFY row_number() OVER (
                PARTITION BY criterion_id, criterion_version, judge ORDER BY computed_at DESC) = 1
            ORDER BY computed_at DESC""",
        {"space_id": space_id},
    )
    out: dict[tuple[str, int], list[dict]] = {}
    for r in rows:
        out.setdefault((r["criterion_id"], int(r["criterion_version"])), []).append(r)
    return out


async def evaluators(
    space_id: str,
    criteria: list[Criterion],
    *,
    sql: SqlClient,
    store: StateStore,
    prefix: str,
    judges: dict[str, str],
    min_lower_bound: float,
    expectation_judge_version: str | None,
) -> Evaluators:
    return Evaluators(
        space_id=space_id,
        min_lower_bound=min_lower_bound,
        evaluators=registry(
            criteria,
            await latest_agreement(space_id, sql=sql, prefix=prefix),
            judges=judges,
            min_lower_bound=min_lower_bound,
            expectation_judge_version=expectation_judge_version,
            reviewers=len(await store.get_reviewers(space_id)),
        ),
    )


def undecidable(statuses: dict[str, str]) -> set[str]:
    """Evaluator ids whose verdicts may not decide a gate, from a run's recorded statuses."""
    return {eid for eid, status in statuses.items() if status == "uncalibrated"}
