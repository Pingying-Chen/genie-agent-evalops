"""The eval set: cases owners write or promote from reviewed answers.

Every edit is a new version, so a regression run records exactly what it asked and expected.
Cases are never deleted: retiring one keeps its history.
"""

import uuid
from datetime import UTC, datetime

import yaml
from pydantic import ValidationError

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import NotFound, ValidationFailed
from evalops.schemas.evalset import CaseSource, CategoryCoverage, EvalCase, EvalCaseBody, EvalSet, Expect
from evalops.services import failures, splits


async def cases(store: StateStore, space_id: str) -> list[EvalCase]:
    return [EvalCase.model_validate(c) for c in await store.get_eval_cases(space_id)]


async def evalset(space_id: str, *, sql: SqlClient, store: StateStore, prefix: str) -> EvalSet:
    """Active cases first, then retired; categories without an active case first, by priority."""
    current = await cases(store, space_id)
    ranked = (await failures.analysis(space_id, sql=sql, store=store, prefix=prefix)).categories
    active = [c for c in current if c.status == "active"]
    coverage = [
        CategoryCoverage(
            category_id=c.id,
            name=c.name,
            priority=c.priority,
            active_cases=sum(1 for case in active if c.id in case.category_ids),
        )
        for c in ranked
    ]
    coverage.sort(key=lambda c: (c.active_cases > 0, -(c.priority or 0), c.name.lower()))
    current.sort(key=lambda c: (c.status != "active", c.title.lower()))
    assigned = await splits.stored(store, space_id)
    settings = await splits.get_settings(store, space_id)
    values = await splits.values_for(space_id, current, sql=sql, store=store, prefix=prefix)
    return EvalSet(
        space_id=space_id,
        cases=current,
        coverage=coverage,
        splits=sorted(assigned.values(), key=lambda s: s.case_id),
        split_settings=settings,
        split_summary=splits.summary(current, assigned, values, settings),
    )


async def _validate(store: StateStore, space_id: str, body: EvalCaseBody, criterion_ids: set[str]) -> None:
    unknown = sorted(set(body.expect.criteria) - criterion_ids)
    if unknown:
        raise ValidationFailed(f"Unknown rules in expect.criteria: {', '.join(unknown)}.")
    categories = {c["id"] for c in await store.get_categories(space_id)}
    missing = sorted(set(body.category_ids) - categories)
    if missing:
        raise ValidationFailed(f"Unknown failure categories: {', '.join(missing)}.")


async def create_case(
    store: StateStore,
    space_id: str,
    body: EvalCaseBody,
    user: str,
    criterion_ids: set[str],
    case_id: str | None = None,
) -> EvalCase:
    await _validate(store, space_id, body, criterion_ids)
    case = EvalCase(
        **body.model_dump(),
        case_id=case_id or uuid.uuid4().hex[:12],
        space_id=space_id,
        version=1,
        updated_by=user,
        updated_at=datetime.now(UTC),
    )
    await store.add_eval_case_version(space_id, case.model_dump())
    return case


async def update_case(
    store: StateStore,
    space_id: str,
    case_id: str,
    body: EvalCaseBody,
    base_version: int,
    user: str,
    criterion_ids: set[str],
) -> EvalCase:
    """Save `body` as the version after `base_version`, the one the owner edited. If someone saved
    in between, the store raises Conflict instead of silently overwriting their change."""
    current = {c.case_id: c for c in await cases(store, space_id)}
    if case_id not in current:
        raise NotFound("No such eval case in this space.")
    await _validate(store, space_id, body, criterion_ids)
    case = EvalCase(
        **body.model_dump(),
        case_id=case_id,
        space_id=space_id,
        version=base_version + 1,
        updated_by=user,
        updated_at=datetime.now(UTC),
    )
    await store.add_eval_case_version(space_id, case.model_dump())
    return case


async def history(store: StateStore, space_id: str, case_id: str) -> list[EvalCase]:
    versions = await store.get_eval_case_history(space_id, case_id)
    if not versions:
        raise NotFound("No such eval case in this space.")
    return [EvalCase.model_validate(v) for v in versions]


async def draft_from_trace(
    space_id: str, trace_id: str, *, sql: SqlClient, store: StateStore, prefix: str
) -> EvalCaseBody:
    """A case prefilled from a collected answer: the conversation's questions up to it, the rules
    people said apply to it (they must apply and pass), the reviewer's reason as a first fact to
    rewrite, and the observation's failure categories. The owner edits it before saving."""
    params = {"space_id": space_id, "trace_id": trace_id}
    rows = await sql.execute(
        f"""SELECT t.question, t.turn_index, t.conversation_id
            FROM {prefix}.traces t
            JOIN {prefix}.traces me ON me.conversation_id = t.conversation_id AND me.space_id = t.space_id
            WHERE me.trace_id = :trace_id AND me.space_id = :space_id AND t.turn_index <= me.turn_index
            ORDER BY t.turn_index""",
        params,
    )
    if not rows:
        raise NotFound("Not found.")
    review = await sql.execute(
        f"""SELECT max_by(task_success, updated_at) AS task_success,
                   max_by(task_rationale, updated_at) AS task_rationale
            FROM {prefix}.trace_reviews WHERE space_id = :space_id AND trace_id = :trace_id""",
        params,
    )
    labels = await sql.execute(
        f"""SELECT criterion_id, max_by(label, labeled_at) AS label FROM {prefix}.human_labels
            WHERE space_id = :space_id AND trace_id = :trace_id GROUP BY criterion_id""",
        params,
    )
    verdict = review[0].get("task_success") if review else None
    rationale = (review[0].get("task_rationale") or "").strip() if review else ""
    turns = [r["question"] for r in rows if r.get("question")]
    tags = (await store.get_tags(space_id)).get(trace_id, [])
    return EvalCaseBody(
        title=turns[-1][:200] if turns else trace_id,
        turns=turns or ["(question not readable)"],
        expect=Expect(
            criteria={r["criterion_id"]: "pass" for r in labels if r["label"] in ("pass", "fail")},
            facts=[f"Fixes what the reviewer found: {rationale}"[:500]]
            if verdict == "fail" and rationale
            else [],
        ),
        category_ids=tags,
        origin="production_failure"
        if verdict == "fail"
        else "production_pass"
        if verdict == "pass"
        else "written",
        source=CaseSource(
            trace_id=trace_id, conversation_id=rows[0]["conversation_id"], reviewer_verdict=verdict
        ),
    )


def export_yaml(current: list[EvalCase]) -> str:
    """The current version of every case, in a form `import_yaml` reads back (for review in git)."""
    out = []
    for case in sorted(current, key=lambda c: c.case_id):
        body = EvalCaseBody.model_validate(case.model_dump()).model_dump(mode="json", exclude_defaults=True)
        out.append({"case_id": case.case_id, "version": case.version, **body})
    return yaml.safe_dump({"cases": out}, sort_keys=False, allow_unicode=True, width=100)


async def import_yaml(
    store: StateStore, space_id: str, text: str, user: str, criterion_ids: set[str]
) -> dict[str, int]:
    """Create cases that are new, add a version to cases whose content changed, skip the rest.
    The whole file is validated before anything is written."""
    try:
        loaded = yaml.safe_load(text) or {}
        entries = loaded.get("cases", []) if isinstance(loaded, dict) else None
        if not isinstance(entries, list):
            raise ValidationFailed("The file must have a top-level `cases:` list.")
        parsed = [(e.get("case_id"), EvalCaseBody.model_validate(e)) for e in entries]
    except (yaml.YAMLError, ValidationError, AttributeError) as exc:
        raise ValidationFailed(f"Not a valid eval set file: {exc}") from exc
    for _, body in parsed:
        await _validate(store, space_id, body, criterion_ids)
    current = {c.case_id: c for c in await cases(store, space_id)}
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for case_id, body in parsed:
        existing = current.get(case_id) if case_id else None
        if existing is None:
            await create_case(store, space_id, body, user, criterion_ids, case_id=case_id)
            counts["created"] += 1
        elif EvalCaseBody.model_validate(existing.model_dump()) == body:
            counts["unchanged"] += 1
        else:
            await update_case(store, space_id, case_id, body, existing.version, user, criterion_ids)
            counts["updated"] += 1
    return counts
