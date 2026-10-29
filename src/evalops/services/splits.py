"""Dev/test split of the eval set.

Dev is what an owner iterates a change against, as often as they like; test is held out and
asked only to decide a gate. Both must come from the same distribution, so cases are split
**stratified** over the dimensions a space chooses (type, intent, category, turns), each
stratum getting the same test share.

Assignment is stable: a split is stored apart from the case, only unassigned cases are
assigned, and earlier assignments never move (otherwise a case seen as dev could later become
test). Within a stratum the new cases are ordered by a hash of (space, case id), so the same
eval set always splits the same way.
"""

import hashlib
import math
from collections import defaultdict
from datetime import datetime

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.errors import NotFound
from evalops.schemas.evalset import CaseSplit, EvalCase, SplitSettings, SplitSummary, StratumMix
from evalops.schemas.evaluation import RunScope
from evalops.schemas.profile import ProfileBody, ProfileSave
from evalops.services import profiles

SETTINGS_KEY = "split"
DIMENSIONS = ("type", "intent", "category", "turns")
TOO_FEW = "(too few cases for a test set)"
UNTAGGED = "untagged"


def case_intents(cases: list[EvalCase], intents: dict[tuple[str, str], str]) -> dict[str, str | None]:
    """A case promoted from an answer covers that answer's intent, whatever label the case got itself."""
    return {
        c.case_id: (intents.get(("trace", c.source.trace_id)) if c.source else None)
        or intents.get(("case", f"{c.case_id}@{c.version}"))
        for c in cases
    }


def dimension_values(case: EvalCase, intent: str | None, category_names: dict[str, str]) -> dict[str, str]:
    first = case.category_ids[0] if case.category_ids else None
    return {
        "type": case.type,
        "intent": intent or UNTAGGED,
        "category": category_names.get(first, first) if first else "none",
        "turns": "multi" if len(case.turns) > 1 else "single",
    }


def _order(space_id: str, case_id: str) -> str:
    return hashlib.sha256(f"{space_id}:{case_id}".encode()).hexdigest()


def label(dims: list[str], key: tuple[str, ...]) -> str:
    return " / ".join(f"{d}={v}" for d, v in zip(dims, key, strict=True)) or "all"


def assign(
    cases: list[EvalCase],
    existing: dict[str, CaseSplit],
    settings: SplitSettings,
    values: dict[str, dict[str, str]],
    *,
    space_id: str,
    now: datetime,
) -> list[CaseSplit]:
    """Pure: splits for the active cases that have none.

    Cases with the same intent always go to the same side (AI Evals Playbook, EvalOps: near-identical
    questions split across dev and test leak, since tuning on one teaches the other). A new case
    whose intent already has a split joins it. The rest go to the finest stratum (a prefix of
    `stratify_by`) with at least `min_stratum` active cases; within it, cases in hash order go to
    test until the stratum's test count reaches round(share x size). Finer strata are filled
    first, so a coarser one counts their new test cases."""
    active = [c for c in cases if c.status == "active"]
    new = [c for c in active if c.case_id not in existing]
    if not new:
        return []
    dims = list(settings.stratify_by)
    split_of = {cid: s.split for cid, s in existing.items()}

    def key(case_id: str, depth: int) -> tuple[str, ...]:
        return tuple(values[case_id][d] for d in dims[:depth])

    pools: dict[tuple[int, tuple[str, ...]] | None, list[EvalCase]] = defaultdict(list)
    for case in new:
        pool = None
        for depth in range(len(dims), -1, -1):
            k = key(case.case_id, depth)
            if sum(1 for a in active if key(a.case_id, depth) == k) >= settings.min_stratum:
                pool = (depth, k)
                break
        pools[pool].append(case)

    def group(case_id: str) -> str | None:
        intent = values[case_id].get("intent")
        return intent if intent and intent != UNTAGGED else None

    group_split = {group(cid): split for cid, split in split_of.items() if cid in values and group(cid)}

    out = []
    for pool in sorted(pools, key=lambda p: -1 if p is None else -p[0]):
        members = sorted(pools[pool], key=lambda c: _order(space_id, c.case_id))
        if pool is None:
            need, stratum = 0, TOO_FEW
        else:
            depth, k = pool
            in_pool = [a for a in active if key(a.case_id, depth) == k]
            target = math.floor(settings.test_share * len(in_pool) + 0.5)
            have = sum(1 for a in in_pool if split_of.get(a.case_id) == "test")
            need = max(0, min(len(members), target - have))
            stratum = label(dims[:depth], k)
        placed = 0
        for case in members:
            intent = group(case.case_id)
            if intent in group_split:  # its intent is already on one side: follow it
                split, where = group_split[intent], f"with its intent ({intent})"
            else:
                split, where = ("test" if placed < need else "dev"), stratum
                placed += split == "test"
                if intent:
                    group_split[intent] = split
            split_of[case.case_id] = split
            out.append(
                CaseSplit(
                    case_id=case.case_id, split=split, stratum=where, assigned_by="auto", assigned_at=now
                )
            )
    return out


def summary(
    cases: list[EvalCase],
    splits: dict[str, CaseSplit],
    values: dict[str, dict[str, str]],
    settings: SplitSettings,
) -> SplitSummary:
    """The dev/test mix of the active cases per value of every dimension (stratified or not), and
    what makes a gate weak: no test set, or a value with enough cases that test lacks."""
    active = [c for c in cases if c.status == "active"]
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"dev": 0, "test": 0, "unassigned": 0}
    )
    for case in active:
        split = splits[case.case_id].split if case.case_id in splits else "unassigned"
        for dim in DIMENSIONS:
            counts[(dim, values[case.case_id][dim])][split] += 1
    mix = [
        StratumMix(dimension=dim, value=value, **n)
        for (dim, value), n in sorted(counts.items(), key=lambda kv: (DIMENSIONS.index(kv[0][0]), kv[0][1]))
    ]
    dev = sum(1 for c in active if c.case_id in splits and splits[c.case_id].split == "dev")
    test = sum(1 for c in active if c.case_id in splits and splits[c.case_id].split == "test")
    unassigned = len(active) - dev - test
    warnings = []
    if active and not test:
        warnings.append(
            f"No test cases yet: a gate needs at least {settings.min_stratum} active cases"
            if len(active) < settings.min_stratum
            else "No test cases: every active case is in dev, so nothing can pass a gate"
        )
    for m in mix:
        if m.dimension in settings.stratify_by and m.test == 0 and m.dev >= settings.min_stratum:
            warnings.append(f"{m.dimension}={m.value} has {m.dev} dev cases and none in test")
    if unassigned:
        warnings.append(f"{unassigned} active cases have no split yet: they are assigned at the next run")
    return SplitSummary(dev=dev, test=test, unassigned=unassigned, mix=mix, warnings=warnings)


async def get_settings(store: StateStore, space_id: str) -> SplitSettings:
    """The space profile's split settings (versioned); before a profile sets
    them, the settings stored by earlier versions; else the defaults."""
    profile = await profiles.current(store, space_id)
    if profile.split is not None:
        return profile.split
    return SplitSettings.model_validate(await store.get_setting(space_id, SETTINGS_KEY) or {})


async def put_settings(
    store: StateStore, library, space_id: str, settings: SplitSettings, user: str
) -> SplitSettings:
    """Saved as a new version of the space's profile, so split changes have a history too."""
    latest = await profiles.current(store, space_id)
    body = ProfileBody.model_validate(latest.model_dump()).model_dump()
    save = ProfileSave(
        **{**body, "split": settings}, base_version=latest.version, note="Dev/test split settings"
    )
    await profiles.save(store, library, space_id, save, user)
    return settings


async def stored(store: StateStore, space_id: str) -> dict[str, CaseSplit]:
    return {r["case_id"]: CaseSplit.model_validate(r) for r in await store.get_case_splits(space_id)}


async def values_for(
    space_id: str, cases: list[EvalCase], *, sql: SqlClient, store: StateStore, prefix: str
) -> dict[str, dict[str, str]]:
    """Every case's value on every dimension. Intents come from the review job's tagger; a case
    it has not tagged yet is `untagged`."""
    rows = await sql.execute(
        f"SELECT kind, ref_id, intent FROM {prefix}.question_intents WHERE space_id = :space_id",
        {"space_id": space_id},
    )
    intents = case_intents(cases, {(r["kind"], r["ref_id"]): r["intent"] for r in rows})
    names = {c["id"]: c["name"] for c in await store.get_categories(space_id)}
    return {c.case_id: dimension_values(c, intents.get(c.case_id), names) for c in cases}


async def ensure_assigned(
    space_id: str,
    cases: list[EvalCase],
    *,
    sql: SqlClient,
    store: StateStore,
    prefix: str,
    now: datetime,
) -> dict[str, CaseSplit]:
    """Assign every active case that has no split, store the new ones, return all splits."""
    existing = await stored(store, space_id)
    if all(c.case_id in existing for c in cases if c.status == "active"):
        return existing
    settings = await get_settings(store, space_id)
    values = await values_for(space_id, cases, sql=sql, store=store, prefix=prefix)
    new = assign(cases, existing, settings, values, space_id=space_id, now=now)
    if new:
        await store.put_case_splits(space_id, [s.model_dump() for s in new])
    return {**existing, **{s.case_id: s for s in new}}


async def reset(store: StateStore, space_id: str) -> int:
    """Clear automatic assignments (owner overrides stay), so the next assignment re-splits."""
    auto = [cid for cid, s in (await stored(store, space_id)).items() if s.assigned_by == "auto"]
    if auto:
        await store.delete_case_splits(space_id, auto)
    return len(auto)


async def override(
    store: StateStore,
    space_id: str,
    cases: list[EvalCase],
    case_id: str,
    split: str | None,
    user: str,
    now: datetime,
) -> None:
    """Put one case in a split as the owner's call, or (None) hand it back to automatic assignment."""
    if case_id not in {c.case_id for c in cases}:
        raise NotFound("No such eval case in this space.")
    if split is None:
        await store.delete_case_splits(space_id, [case_id])
        return
    row = CaseSplit(
        case_id=case_id, split=split, stratum="(set by the owner)", assigned_by=user, assigned_at=now
    )
    await store.put_case_splits(space_id, [row.model_dump()])


def select(
    cases: list[EvalCase],
    splits: dict[str, CaseSplit],
    which: str,
    targets: set[str],
    scope: RunScope | None = None,
    values: dict[str, dict[str, str]] | None = None,
) -> list[EvalCase]:
    """The active cases a run asks: `dev`, `test` (plus the change's targets, whatever their split),
    or `all`; on a dev run, optionally part of them (`scope`, already checked by `check_scope`;
    `values` = `values_for`, needed for a slice). The targets are asked whatever their split:
    they are known failures, not held-out questions."""
    active = [c for c in cases if c.status == "active"]
    if scope and scope.targets_only:
        return [c for c in active if c.case_id in targets]
    chosen = [
        c for c in active if which == "all" or (c.case_id in splits and splits[c.case_id].split == which)
    ]
    if which == "test":
        ids = {c.case_id for c in chosen}
        return chosen + [c for c in active if c.case_id in targets and c.case_id not in ids]
    if scope:
        chosen = [c for c in chosen if (values or {}).get(c.case_id, {}).get(scope.dimension) == scope.value]
    return chosen
