"""Failure analysis: observations, the categories owners give
them, and which category to work on first.

Observations come from Delta (reviews and check results); categories and tags are owner edits
in Lakebase. Priority = frequency (1-5) x impact (1-5), as in the AI Evals and Analytics
Playbook's Frequency x Impact prioritisation; frequency = tagged turns / reviewed turns.
"""

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime

import yaml

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import Conflict, NotFound
from evalops.schemas.api import (
    CategoryCreate,
    CategorySummary,
    CategoryUpdate,
    CriterionDraft,
    FailureCategory,
    Observation,
    Observations,
)
from evalops.schemas.llm import CategorizeInput, CategorizeOutput
from evalops.schemas.review import CategorySuggestion
from evalops.services import traces
from evalops.services.delta_store import DeltaWriter
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill

logger = logging.getLogger(__name__)


# Lower bound of each frequency score above 1. The course fixes the ends (<1% and >=30%); the
# bands between are this project's choice.
FREQUENCY_BANDS = ((0.30, 5), (0.15, 4), (0.05, 3), (0.01, 2))


def frequency_score(rate: float) -> int:
    return next((score for bound, score in FREQUENCY_BANDS if rate >= bound), 1)


def summarise(
    categories: list[FailureCategory],
    items: list[Observation],
    reviewed: int,
    false_alarms: dict[str, int] | None = None,
) -> list[CategorySummary]:
    """Each category's counts and priority, highest priority first (ties: most turns, then name).

    A category nothing is tagged with yet stays listed, with zero turns. For a category linked
    to a criterion, `linked_caught` says how many of its turns that criterion fails (does the
    new rule catch what it was written for?) and `linked_false_alarms` how many turns people
    passed it fails (does it flag good answers?).
    """
    false_alarms = false_alarms or {}
    out = []
    for c in categories:
        tagged = [i for i in items if c.id in i.category_ids]
        frequency = len(tagged) / reviewed if reviewed else None
        score = frequency_score(frequency) if frequency else None  # nothing tagged: no priority yet
        out.append(
            CategorySummary(
                **c.model_dump(),
                turns=len(tagged),
                failed=sum(1 for i in tagged if i.task_success == "fail"),
                caught=sum(1 for i in tagged if i.caught_by),
                frequency=frequency,
                frequency_score=score,
                priority=score * c.impact if score else None,
                linked_caught=sum(1 for i in tagged if c.criterion_id in i.caught_by)
                if c.criterion_id
                else None,
                linked_false_alarms=false_alarms.get(c.criterion_id, 0) if c.criterion_id else None,
            )
        )
    return sorted(out, key=lambda s: (-(s.priority or 0), -s.turns, s.name.lower()))


async def categories(store: StateStore, space_id: str) -> list[FailureCategory]:
    return [FailureCategory.model_validate(c) for c in await store.get_categories(space_id)]


async def analysis(space_id: str, *, sql: SqlClient, store: StateStore, prefix: str) -> Observations:
    """Observations with their tags, and the categories ranked by priority."""
    found = await traces.observations(space_id, sql=sql, prefix=prefix)
    tags = await store.get_tags(space_id)
    for item in found.items:
        item.category_ids = tags.get(item.trace_id, [])
    existing = await categories(store, space_id)
    named = {c.name.lower(): c.id for c in existing}
    for item in found.items:
        if item.category_ids:
            item.suggestion = None  # the owner already decided
        elif item.suggestion:
            item.suggestion.category_id = named.get(item.suggestion.category.lower())
    linked = sorted({c.criterion_id for c in existing if c.criterion_id})
    alarms = await traces.false_alarms(space_id, linked, sql=sql, prefix=prefix)
    found.categories = summarise(existing, found.items, found.reviewed, alarms)
    return found


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40].strip("_") or "new_rule"


def draft_criterion(category: FailureCategory, tagged: list[Observation]) -> CriterionDraft:
    """A rubric-judge criterion to start from: the category becomes the failure text and its
    reviewed answers are listed as examples. `rule` (when it applies, what the answer must do)
    is left for the owner, who knows the space; a code check replaces the judge where the
    failure can be detected deterministically."""
    criterion_id = f"rubric.{_slug(category.name)}"
    failure = category.description or category.name
    body = {
        "id": criterion_id,
        "title": category.name,
        "version": 1,
        "family": "rubric",
        "layer": "behaviour",
        "gate": "monitor",
        "applies_to": {"level": "report"},
        "description": f"Drafted from the failure category {category.name!r} (impact {category.impact}). {failure}",
        "params": {
            "skill": "rubric_judge",
            "rule": "TODO: when the rule applies, and what the answer must do then.",
            "failure": failure,
        },
    }
    lines = [
        f"# Draft criterion from the failure category {category.name!r}. Before adding it to the",
        "# criteria folder: write params.rule, check the failure text, and ask whether code could",
        "# detect this instead of an LLM judge (deterministic checks need no calibration).",
        "# Shadow (gate: monitor) until human labels calibrate the judge.",
        yaml.safe_dump(body, sort_keys=False, width=100, allow_unicode=True).rstrip(),
        "",
        "# Reviewed answers in this category: the judge should fail these.",
    ]
    for item in tagged:
        note = item.task_rationale or item.findings or ""
        lines.append(f"#   {item.trace_id}  {item.question or ''!r}  ({item.task_success}) {note}".rstrip())
    return CriterionDraft(
        criterion_id=criterion_id, filename=f"{criterion_id}.yaml", yaml="\n".join(lines) + "\n"
    )


async def draft(
    space_id: str, category_id: str, *, sql: SqlClient, store: StateStore, prefix: str
) -> CriterionDraft:
    found = await analysis(space_id, sql=sql, store=store, prefix=prefix)
    category = next((c for c in found.categories if c.id == category_id), None)
    if category is None:
        raise NotFound("No such category in this space.")
    tagged = [i for i in found.items if category_id in i.category_ids]
    return draft_criterion(FailureCategory.model_validate(category.model_dump()), tagged)


async def suggest_categories(
    space_id: str,
    *,
    sql: SqlClient,
    store: StateStore,
    delta: DeltaWriter,
    prefix: str,
    runner: SkillRunner,
    skill: Skill,
    now: datetime,
) -> int:
    """Suggest a category for each untagged observation whose input changed since its last
    suggestion (the input includes the category list, so a new category refreshes them all).
    Runs in the review job; returns how many suggestions were written."""
    found = await traces.observations(space_id, sql=sql, prefix=prefix)
    existing = await categories(store, space_id)
    listing = "\n".join(f"{c.name}: {c.description}" if c.description else c.name for c in existing)
    tags = await store.get_tags(space_id)
    previous = await delta.load_suggestions(space_id)
    todo = []
    for item in found.items:
        if tags.get(item.trace_id):
            continue
        data = CategorizeInput(
            categories=listing or "(none yet)",
            question=item.question or "",
            verdict=item.task_success or "not decided",
            rationale=item.task_rationale or "",
            findings=item.findings or "",
        )
        key = hashlib.sha256(f"{skill.ref}|{runner.model}|{data.model_dump_json()}".encode()).hexdigest()[:16]
        if item.trace_id in previous and previous[item.trace_id].input_hash == key:
            continue
        todo.append((item.trace_id, data, key))
    results = await asyncio.gather(*(runner.run(skill, data) for _, data, _ in todo))
    by_name = {c.name.lower(): c for c in existing}
    out = []
    for (trace_id, _, key), result in zip(todo, results, strict=True):
        if not isinstance(result.output, CategorizeOutput):
            logger.warning("no category suggestion for %s: %s", trace_id, result.error)
            continue
        name = " ".join(result.output.category.split())
        match = by_name.get(name.lower())  # trust the name, not the model's is_new flag
        out.append(
            CategorySuggestion(
                space_id=space_id,
                trace_id=trace_id,
                category=match.name if match else name,
                is_new=match is None,
                description="" if match else result.output.description.strip(),
                reason=result.output.reasoning.strip(),
                input_hash=key,
                skill=result.skill,
                model=result.model,
                created_at=now,
            )
        )
    await delta.merge_suggestions(out)
    return len(out)


def _check_name(existing: list[FailureCategory], name: str, keep_id: str | None = None) -> str:
    name = " ".join(name.split())
    if any(c.name.lower() == name.lower() and c.id != keep_id for c in existing):
        raise Conflict(f"A category named {name!r} already exists.")
    return name


async def create_category(
    store: StateStore, space_id: str, body: CategoryCreate, user: str
) -> FailureCategory:
    existing = await categories(store, space_id)
    now = datetime.now(UTC)
    category = FailureCategory(
        id=uuid.uuid4().hex,
        name=_check_name(existing, body.name),
        description=body.description.strip(),
        impact=body.impact,
        created_at=now,
        updated_by=user,
        updated_at=now,
    )
    await store.put_category(space_id, category.model_dump())
    return category


async def update_category(
    store: StateStore,
    space_id: str,
    category_id: str,
    body: CategoryUpdate,
    user: str,
    criterion_ids: set[str],
) -> FailureCategory:
    existing = await categories(store, space_id)
    current = next((c for c in existing if c.id == category_id), None)
    if current is None:
        raise NotFound("No such category in this space.")
    changes = body.model_dump(exclude_unset=True)
    if changes.get("name") is not None:
        changes["name"] = _check_name(existing, changes["name"], keep_id=category_id)
    if changes.get("description") is not None:
        changes["description"] = changes["description"].strip()
    if changes.get("criterion_id") is not None and changes["criterion_id"] not in criterion_ids:
        raise NotFound(f"No loaded criterion {changes['criterion_id']!r}.")
    changes = {k: v for k, v in changes.items() if v is not None or k == "criterion_id"}
    updated = current.model_copy(update={**changes, "updated_by": user, "updated_at": datetime.now(UTC)})
    await store.put_category(space_id, updated.model_dump())
    return updated


async def delete_category(store: StateStore, space_id: str, category_id: str) -> None:
    if not any(c.id == category_id for c in await categories(store, space_id)):
        raise NotFound("No such category in this space.")
    await store.delete_category(space_id, category_id)


async def tag(
    store: StateStore, space_id: str, trace_id: str, category_ids: list[str], user: str
) -> list[str]:
    """Replace one observation's categories; every id must be a category of this space."""
    known = {c.id for c in await categories(store, space_id)}
    unknown = [i for i in category_ids if i not in known]
    if unknown:
        raise NotFound(f"No such category in this space: {', '.join(unknown)}.")
    ids = list(dict.fromkeys(category_ids))
    await store.set_tags(space_id, trace_id, ids, user)
    return ids
