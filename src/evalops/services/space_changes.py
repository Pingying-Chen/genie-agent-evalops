"""Space changes: what an owner wants to change in a space's
configuration, tested by a regression run before it is applied.

The current configuration is read with the viewer's own token (the app's identity does not
manage spaces), so a change is checked against what the space looks like right now.
"""

import json
import uuid
from datetime import UTC, datetime

from pydantic import ValidationError

from evalops.clients.genie import GenieClient
from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import Conflict, NotFound, UpstreamError, ValidationFailed
from evalops.schemas.changes import ChangeBody, ChangeProposal, SpaceChange
from evalops.schemas.genie import SpaceConfig
from evalops.schemas.llm import ProposeChangeInput, ProposeChangeOutput, ProposedOperation
from evalops.services import evalset, failures
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill
from evalops.services.regression import changes as ops
from evalops.services.regression import results


async def changes(store: StateStore, space_id: str) -> list[SpaceChange]:
    return [SpaceChange.model_validate(c) for c in await store.get_changes(space_id)]


async def get_change(store: StateStore, space_id: str, change_id: str) -> SpaceChange:
    found = next((c for c in await changes(store, space_id) if c.change_id == change_id), None)
    if found is None:
        raise NotFound("No such change in this space.")
    return found


def preview(config: SpaceConfig, body: ChangeBody) -> dict[str, int]:
    """Count change per kind of configuration, after checking every operation fits."""
    after = SpaceConfig(serialized_space=ops.apply(config.serialized_space, body.operations)).counts()
    before = config.counts()
    return {k: after[k] - before.get(k, 0) for k in after if after[k] != before.get(k, 0)}


async def create_change(
    store: StateStore, space_id: str, body: ChangeBody, user: str, config: SpaceConfig
) -> SpaceChange:
    preview(config, body)  # raises ValidationFailed if an operation does not fit
    case_ids = {c["case_id"] for c in await store.get_eval_cases(space_id)}
    unknown = sorted(set(body.target_case_ids) - case_ids)
    if unknown:
        raise ValidationFailed(f"Unknown eval cases: {', '.join(unknown)}.")
    now = datetime.now(UTC)
    change = SpaceChange(
        **body.model_dump(),
        change_id=uuid.uuid4().hex[:12],
        space_id=space_id,
        base_version=config.version,
        created_by=user,
        created_at=now,
        updated_at=now,
    )
    await store.put_change(space_id, change.model_dump())
    return change


async def _stored(store: StateStore, space_id: str, change_id: str) -> dict:
    """The stored record, which also keeps what the API model leaves out (`replaced_config`)."""
    found = next((c for c in await store.get_changes(space_id) if c["change_id"] == change_id), None)
    if found is None:
        raise NotFound("No such change in this space.")
    return found


async def set_status(
    store: StateStore, space_id: str, change_id: str, status: str, **extra: str | None
) -> SpaceChange:
    """Update the status (and `applied_version` / `replaced_config`), keeping every other stored field."""
    record = await _stored(store, space_id, change_id)
    record |= {
        "status": status,
        "updated_at": datetime.now(UTC),
        **{k: v for k, v in extra.items() if v is not None},
    }
    await store.put_change(space_id, record)
    return SpaceChange.model_validate(record)


async def latest_gate(space_id: str, change_id: str, *, sql: SqlClient, store: StateStore, prefix: str):
    """The comparison of the newest finished gate run (test split) of this change, or None."""
    rows = await sql.execute(
        f"""SELECT run_id FROM {prefix}.eval_runs WHERE space_id = :space_id AND change_id = :change_id
              AND status = 'succeeded' AND split = 'test' ORDER BY started_at DESC LIMIT 1""",
        {"space_id": space_id, "change_id": change_id},
    )
    if not rows:
        return None
    detail = await results.run_detail(space_id, rows[0]["run_id"], sql=sql, store=store, prefix=prefix)
    return detail.comparison


async def gate_runs(space_id: str, *, sql: SqlClient, prefix: str) -> dict[str, int]:
    """Gate runs per change that showed test results (a partial one did too): more than one means
    the owner looked at the test set and tried again, so its accept is optimistic."""
    rows = await sql.execute(
        f"""SELECT change_id, count(*) AS n FROM {prefix}.eval_runs
            WHERE space_id = :space_id AND change_id IS NOT NULL AND split = 'test'
              AND status IN ('succeeded', 'partial')
            GROUP BY change_id""",
        {"space_id": space_id},
    )
    return {r["change_id"]: int(r["n"]) for r in rows}


async def apply_change(
    space_id: str,
    change_id: str,
    *,
    genie: GenieClient,
    sql: SqlClient,
    store: StateStore,
    prefix: str,
    override: bool = False,
) -> SpaceChange:
    """Apply a tested change with the viewer's own token. Refused unless its latest gate run was
    accepted (or `override`, the owner's explicit call), and unless the space is still the
    configuration the change was written and tested against."""
    change = await get_change(store, space_id, change_id)
    if change.status != "tested":
        raise Conflict(f"Only a tested change can be applied; this one is {change.status}.")
    comparison = await latest_gate(space_id, change_id, sql=sql, store=store, prefix=prefix)
    if comparison is None:
        raise Conflict("This change has no finished gate run: test it on the test split.")
    if comparison.verdict != "accept" and not override:
        raise Conflict(f"Its latest test was {comparison.verdict}: {'; '.join(comparison.reasons)}.")
    config = await genie.get_space_config(space_id)
    if config.version != change.base_version:
        raise Conflict("The space changed since this change was written; test it again on the current agent.")
    await genie.update_space(
        space_id,
        config.model_copy(update={"serialized_space": ops.apply(config.serialized_space, change.operations)}),
    )
    after = await genie.get_space_config(space_id)
    return await set_status(
        store,
        space_id,
        change_id,
        "applied",
        applied_version=after.version,
        replaced_config=config.serialized_space,
    )


async def rollback_change(
    space_id: str, change_id: str, *, genie: GenieClient, sql: SqlClient, store: StateStore, prefix: str
) -> SpaceChange:
    """Put back the exact configuration the change replaced (kept with the change when applied)."""
    change = await get_change(store, space_id, change_id)
    if change.status != "applied":
        raise Conflict(f"Only an applied change can be rolled back; this one is {change.status}.")
    config = await genie.get_space_config(space_id)
    if change.applied_version and config.version != change.applied_version:
        raise Conflict(
            "The space changed after this change was applied; roll back by hand or write a new change."
        )
    previous = (await _stored(store, space_id, change_id)).get("replaced_config")
    if previous is None:  # no copy kept with the change: fall back to the collector's snapshot
        rows = await sql.execute(
            f"""SELECT serialized_space FROM {prefix}.space_versions
                WHERE space_id = :space_id AND version = :version LIMIT 1""",
            {"space_id": space_id, "version": change.base_version},
        )
        if not rows:
            raise Conflict("No stored snapshot of the configuration before this change.")
        previous = rows[0]["serialized_space"]
    await genie.update_space(space_id, config.model_copy(update={"serialized_space": previous}))
    return await set_status(store, space_id, change_id, "rolled_back")


_FIELDS = {
    "add_instruction": ("text",),
    "replace_instruction": ("old", "text"),
    "remove_instruction": ("old",),
    "add_sql_example": ("question", "sql"),
    "set_column": ("table", "column", "description", "synonyms"),
}


def _typed(op: ProposedOperation) -> dict:
    fields = {k: getattr(op, k) for k in _FIELDS[op.op]}
    if op.op == "set_column":  # '' / [] mean "leave it"
        fields["description"] = fields["description"] or None
        fields["synonyms"] = fields["synonyms"] or None
    return {"op": op.op, **fields}


def proposal_input(category, observations, cases, config: SpaceConfig) -> ProposeChangeInput:
    space = json.loads(config.serialized_space or "{}")
    instructions = [
        line.strip()
        for block in space.get("instructions", {}).get("text_instructions", [])
        for line in "".join(block.get("content", [])).splitlines()
        if line.strip()
    ]
    examples = [
        "".join(e.get("question", [])) for e in space.get("instructions", {}).get("example_question_sqls", [])
    ]
    tables = []
    for t in space.get("data_sources", {}).get("tables", []):
        columns = ", ".join(c.get("column_name", "") for c in t.get("column_configs", []))
        tables.append(f"{t.get('identifier')}" + (f" (metadata on: {columns})" if columns else ""))
    failures = "\n".join(
        f"{n}. Question: {o.question}\n   Reviewer: {o.task_success or 'noted'}: {o.task_rationale or ''}"
        + (f"\n   Findings: {o.findings}" if o.findings else "")
        for n, o in enumerate(observations[:8], start=1)
    )
    case_text = "\n".join(
        f"- {' / '.join(c.turns)}" + (f" -> must: {'; '.join(c.expect.facts)}" if c.expect.facts else "")
        for c in cases
    )
    return ProposeChangeInput(
        category=f"{category.name}: {category.description}",
        failures=failures or "(none tagged yet)",
        cases=case_text,
        instructions="\n".join(instructions) or "(none)",
        sql_examples="\n".join(f"- {q}" for q in examples),
        tables="\n".join(tables) or "(none)",
    )


async def propose(
    space_id: str,
    category_id: str,
    *,
    config: SpaceConfig,
    sql: SqlClient,
    store: StateStore,
    prefix: str,
    runner: SkillRunner,
    skill: Skill,
) -> ChangeProposal:
    """Ask the proposer skill for a change against the current configuration. Operations that do
    not fit it are dropped and listed, so what comes back always applies."""
    analysis = await failures.analysis(space_id, sql=sql, store=store, prefix=prefix)
    category = next((c for c in analysis.categories if c.id == category_id), None)
    if category is None:
        raise NotFound("No such failure category in this space.")
    observations = [o for o in analysis.items if category_id in o.category_ids]
    cases = [
        c
        for c in await evalset.cases(store, space_id)
        if c.status == "active" and category_id in c.category_ids
    ]
    result = await runner.run(skill, proposal_input(category, observations, cases, config))
    if result.output is None:
        raise UpstreamError("llm", f"the proposer returned no usable change: {result.error}")
    out: ProposeChangeOutput = result.output
    kept, dropped, current = [], [], config.serialized_space
    for op in out.operations:
        try:
            typed = ChangeBody(title="x", operations=[_typed(op)]).operations
            current = ops.apply(current, typed)
            kept.extend(typed)
        except (ValidationError, ValidationFailed) as exc:
            dropped.append(f"{op.op}: {str(exc).splitlines()[0][:200]}")
    draft = (
        ChangeBody(
            title=out.title[:200] or category.name,
            rationale=out.rationale,
            operations=kept,
            target_case_ids=[c.case_id for c in cases],
        )
        if kept
        else None
    )
    return ChangeProposal(
        diagnosis=out.diagnosis, draft=draft, dropped=dropped, skill=skill.ref, model=runner.model
    )
