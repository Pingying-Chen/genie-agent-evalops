"""Dev/test split of the eval set.

Dev is iterated on as often as the owner likes; test is held out for the gate. The tests below
show the four properties that make the split trustworthy: stratified (each stratum gets the same
test share), deterministic (same input, same split), stable (adding cases never moves old ones)
and honest about small sets (no pretend test set of one case).
"""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_api import VIEWER, FakeSql, api, genie, sql  # noqa: F401  (fixtures)
from test_regression import NOW, graded

from evalops.clients.lakebase import InMemoryStore
from evalops.schemas.evalset import CaseSplit, EvalCase, SplitSettings
from evalops.schemas.evaluation import ChangeTest, EvalStart, RunScope, scope_param
from evalops.services import splits
from evalops.services.regression.results import case_runs, compare, slices

URL = "/api/spaces/space-ok/evalset"


def make_case(case_id: str, type_: str = "core", turns: int = 1, status: str = "active") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        space_id="s",
        version=1,
        title=case_id,
        turns=[f"question {n} of {case_id}" for n in range(turns)],
        type=type_,
        status=status,
        updated_by="o",
        updated_at=NOW,
    )


def values_of(cases: list[EvalCase], intent=lambda c: f"intent of {c.case_id}") -> dict[str, dict[str, str]]:
    return {c.case_id: splits.dimension_values(c, intent(c), {}) for c in cases}


def split_map(rows: list[CaseSplit]) -> dict[str, str]:
    return {r.case_id: r.split for r in rows}


def test_each_stratum_gets_the_same_test_share_and_the_split_is_deterministic():
    cases = [make_case(f"core{n}") for n in range(10)] + [make_case(f"edge{n}", "edge") for n in range(10)]
    settings = SplitSettings(stratify_by=["type"], test_share=0.3)

    first = splits.assign(cases, {}, settings, values_of(cases), space_id="s", now=NOW)
    again = splits.assign(cases, {}, settings, values_of(cases), space_id="s", now=NOW)

    test = [r for r in first if r.split == "test"]
    assert sum(r.case_id.startswith("core") for r in test) == 3  # round(0.3 x 10) per type
    assert sum(r.case_id.startswith("edge") for r in test) == 3
    assert split_map(first) == split_map(again)  # seeded by (space, case id), not by chance
    assert {r.stratum for r in first} == {"type=core", "type=edge"}


def test_cases_with_the_same_intent_stay_on_one_side_so_nothing_leaks_from_dev_to_test():
    cases = [make_case(f"c{n}") for n in range(12)]
    intent = lambda c: "bookings by month" if c.case_id in ("c0", "c1", "c2", "c3") else c.case_id  # noqa: E731
    settings = SplitSettings(stratify_by=["type"], test_share=0.3)

    rows = splits.assign(cases, {}, settings, values_of(cases, intent), space_id="s", now=NOW)
    sides = {r.split for r in rows if r.case_id in ("c0", "c1", "c2", "c3")}
    assert len(sides) == 1  # the four near-identical questions are all dev or all test

    later = cases + [make_case("c12")]
    known = {r.case_id: r for r in rows}
    values = values_of(
        later, lambda c: "bookings by month" if c.case_id in ("c0", "c1", "c2", "c3", "c12") else c.case_id
    )
    [new] = splits.assign(later, known, settings, values, space_id="s", now=NOW)
    assert new.split in sides and new.stratum == "with its intent (bookings by month)"  # joins its intent


def test_intent_is_not_a_stratification_dimension_but_old_settings_still_load():
    assert SplitSettings().stratify_by == ["type"]
    assert SplitSettings(stratify_by=["type", "intent"]).stratify_by == ["type"]


def test_new_cases_never_move_old_ones_and_fill_their_stratum_up_to_its_share():
    old = [make_case(f"a{n}") for n in range(10)]
    settings = SplitSettings(stratify_by=["type"], test_share=0.3)
    existing = {r.case_id: r for r in splits.assign(old, {}, settings, values_of(old), space_id="s", now=NOW)}

    grown = old + [make_case(f"b{n}") for n in range(10)]
    new = splits.assign(grown, existing, settings, values_of(grown), space_id="s", now=NOW)

    assert {r.case_id for r in new} == {f"b{n}" for n in range(10)}  # only the unassigned ones
    all_splits = {**{k: v.split for k, v in existing.items()}, **split_map(new)}
    assert sum(s == "test" for s in all_splits.values()) == 6  # round(0.3 x 20), old 3 kept


def test_a_small_stratum_falls_back_to_a_coarser_one_and_a_tiny_set_has_no_test_set():
    # Every case has its own intent: (type, intent) strata of 1 fall back to (type).
    cases = [make_case(f"c{n}") for n in range(8)]
    settings = SplitSettings(stratify_by=["type", "intent"], test_share=0.25, min_stratum=4)
    rows = splits.assign(cases, {}, settings, values_of(cases, lambda c: c.case_id), space_id="s", now=NOW)
    assert {r.stratum for r in rows} == {"type=core"}
    assert sum(r.split == "test" for r in rows) == 2

    tiny = cases[:3]
    rows = splits.assign(tiny, {}, settings, values_of(tiny), space_id="s", now=NOW)
    assert {r.split for r in rows} == {"dev"} and {r.stratum for r in rows} == {splits.TOO_FEW}
    found = splits.summary(tiny, {r.case_id: r for r in rows}, values_of(tiny), settings)
    assert found.test == 0 and "No test cases yet" in found.warnings[0]


def test_without_stratifying_it_is_a_plain_random_split_and_retired_cases_are_left_out():
    cases = [make_case(f"c{n}", "edge" if n % 3 == 0 else "core") for n in range(10)]
    cases.append(make_case("old", status="retired"))
    rows = splits.assign(
        cases, {}, SplitSettings(stratify_by=[], test_share=0.3), values_of(cases), space_id="s", now=NOW
    )
    assert "old" not in split_map(rows)
    assert sum(r.split == "test" for r in rows) == 3 and {r.stratum for r in rows} == {"all"}


def test_the_summary_shows_the_mix_per_dimension_and_warns_when_test_lacks_a_value():
    cases = [make_case(f"c{n}") for n in range(5)] + [make_case("m", turns=2)]
    assigned = {
        c.case_id: CaseSplit(case_id=c.case_id, split="dev", stratum="x", assigned_by="auto", assigned_at=NOW)
        for c in cases
    }
    found = splits.summary(cases, assigned, values_of(cases), SplitSettings(stratify_by=["type"]))

    turns = {m.value: (m.dev, m.test) for m in found.mix if m.dimension == "turns"}
    assert turns == {"single": (5, 0), "multi": (1, 0)}
    assert "type=core has 6 dev cases and none in test" in found.warnings


def test_a_gate_run_asks_test_cases_and_the_changes_targets_a_dev_run_only_dev():
    cases = [make_case(c) for c in ("d1", "d2", "t1")] + [make_case("new")]
    assigned = {
        cid: CaseSplit(case_id=cid, split=s, stratum="x", assigned_by="auto", assigned_at=NOW)
        for cid, s in (("d1", "dev"), ("d2", "dev"), ("t1", "test"))
    }
    ids = lambda chosen: [c.case_id for c in chosen]  # noqa: E731
    assert ids(splits.select(cases, assigned, "dev", {"d1"})) == ["d1", "d2"]
    assert ids(splits.select(cases, assigned, "test", {"d1"})) == ["t1", "d1"]
    assert ids(splits.select(cases, assigned, "all", set())) == ["d1", "d2", "t1", "new"]


def test_a_dev_run_can_ask_a_slice_or_the_targets_but_a_gate_run_is_whole():
    cases = [make_case("d1", "guardrail"), make_case("d2"), make_case("t1", "guardrail")]
    assigned = {
        cid: CaseSplit(case_id=cid, split=s, stratum="x", assigned_by="auto", assigned_at=NOW)
        for cid, s in (("d1", "dev"), ("d2", "dev"), ("t1", "test"))
    }
    ids = lambda chosen: [c.case_id for c in chosen]  # noqa: E731
    guardrails = RunScope(dimension="type", value="guardrail")

    assert ids(splits.select(cases, assigned, "dev", set(), guardrails, values_of(cases))) == ["d1"]  # not t1
    assert ids(splits.select(cases, assigned, "dev", {"t1"}, RunScope(targets_only=True))) == ["t1"]
    assert guardrails.describe() == "type = guardrail"

    with pytest.raises(ValidationError, match="dev run"):
        ChangeTest(split="test", scope=guardrails)
    with pytest.raises(ValidationError, match="Choose one"):
        RunScope()
    with pytest.raises(ValidationError, match="Only a change has targets"):
        EvalStart(scope=RunScope(targets_only=True))
    assert json.loads(scope_param(guardrails)) == {"dimension": "type", "value": "guardrail"}
    assert scope_param(None) == ""


async def test_an_override_survives_a_resplit_and_an_unknown_case_is_404():
    store = InMemoryStore()
    cases = [make_case(f"c{n}") for n in range(10)]
    for c in cases:
        await store.add_eval_case_version("s", c.model_dump())
    await splits.override(store, "s", cases, "c0", "test", "owner@x", NOW)
    await splits.ensure_assigned("s", cases, sql=FakeSql(), store=store, prefix="p", now=NOW)

    assert await splits.reset(store, "s") == 9  # every automatic one, not the override
    kept = await splits.stored(store, "s")
    assert list(kept) == ["c0"] and kept["c0"].assigned_by == "owner@x"

    from evalops.errors import NotFound

    with pytest.raises(NotFound):
        await splits.override(store, "s", cases, "nope", "dev", "owner@x", NOW)


def test_only_a_gate_run_decides_and_a_dev_case_handed_as_an_example_is_only_marked():
    attempts = graded("d", "baseline", "FFF") + graded("d", "candidate", "PPP")
    attempts += graded("t", "baseline", "FFF") + graded("t", "candidate", "PPP")
    cases = case_runs(attempts, {}, {"d": "dev", "t": "test"})

    dev_run = compare(cases, targets={"d"}, leaked={"d"})
    assert {c.case_id: c.outcome for c in dev_run.cases} == {"d": "fixed", "t": "fixed"}
    assert dev_run.cases[0].example_match and not dev_run.decides
    assert "only a gate run" in dev_run.notes[0]

    gate = compare(cases, targets={"t"}, leaked={"t"}, decides=True)
    assert {c.case_id: c.outcome for c in gate.cases}["t"] == "leaked"  # a test case: a real leak
    assert gate.decides and any("tuned" in n or "test set" in n for n in gate.notes)


def test_slices_give_pass_k_per_value_with_an_interval_and_flag_small_ones():
    attempts = (
        graded("a", "baseline", "PPP") + graded("b", "baseline", "FFF") + graded("c", "baseline", "PPP")
    )
    runs = case_runs(attempts, {}, {"a": "test", "b": "test", "c": "dev"})
    cases = [make_case("a"), make_case("b", "edge"), make_case("c")]

    found = {(s.dimension, s.value): s for s in slices(runs, values_of(cases), min_stratum=2)}

    assert (found[("split", "test")].cases, found[("split", "test")].pass_all) == (2, 1)
    assert found[("type", "edge")].too_small and not found[("type", "core")].too_small
    core = found[("type", "core")]
    assert core.pass_all == 2 and 0 < core.low < 1 and core.high == pytest.approx(1.0)


async def test_apply_reads_only_gate_runs():
    from evalops.services import space_changes

    class Sql:
        statements: list[str] = []

        async def execute(self, statement, parameters=None):
            self.statements.append(statement)
            return []

    sql_ = Sql()
    assert await space_changes.latest_gate("s", "ch", sql=sql_, store=InMemoryStore(), prefix="p") is None
    assert "split = 'test'" in sql_.statements[0]


def test_the_owner_sets_the_split_settings_assigns_and_overrides_through_the_api(api):  # noqa: F811
    for n in range(6):
        body = {"title": f"case {n}", "turns": [f"q{n}"], "type": "edge" if n < 2 else "core"}
        assert api.post(URL, json=body, headers=VIEWER).status_code == 201
    before = api.get(URL, headers=VIEWER).json()
    assert before["split_summary"]["unassigned"] == 6 and before["splits"] == []

    settings = {"stratify_by": [], "test_share": 0.5, "min_stratum": 2}
    assert api.put(f"{URL}/split-settings", json=settings, headers=VIEWER).json() == settings
    after = api.post(f"{URL}/split", json={}, headers=VIEWER).json()
    assert after["split_summary"]["test"] == 3 and after["split_summary"]["dev"] == 3

    case_id = after["splits"][0]["case_id"]
    flipped = "dev" if after["splits"][0]["split"] == "test" else "test"
    moved = api.put(f"{URL}/{case_id}/split", json={"split": flipped}, headers=VIEWER).json()
    mine = next(s for s in moved["splits"] if s["case_id"] == case_id)
    assert mine["split"] == flipped and mine["assigned_by"] != "auto"

    assert api.put(f"{URL}/nope/split", json={"split": "dev"}, headers=VIEWER).status_code == 404
    assert (
        api.put(f"{URL}/split-settings", json={"stratify_by": ["type", "type"]}, headers=VIEWER).status_code
        == 422
    )


def test_a_split_row_round_trips_through_json():
    row = CaseSplit(
        case_id="c", split="test", stratum="all", assigned_by="auto", assigned_at=datetime.now(UTC)
    )
    assert CaseSplit.model_validate(json.loads(row.model_dump_json())) == row


async def test_a_space_with_eval_cases_and_no_traffic_is_still_found_for_tagging_and_splitting():
    store = InMemoryStore()
    await store.add_eval_case_version("new-space", make_case("c").model_dump())
    assert await store.get_eval_case_spaces() == ["new-space"]


async def test_a_partial_gate_run_counts_as_a_look_at_test_but_never_decides():
    from evalops.services import space_changes

    class Sql:
        def __init__(self):
            self.statements: list[str] = []

        async def execute(self, statement, parameters=None):
            self.statements.append(statement)
            return [{"change_id": "ch", "n": 2}] if "count(*)" in statement else []

    sql_ = Sql()
    assert await space_changes.gate_runs("s", sql=sql_, prefix="p") == {"ch": 2}
    await space_changes.latest_gate("s", "ch", sql=sql_, store=InMemoryStore(), prefix="p")
    counted, deciding = sql_.statements
    assert "'partial'" in counted and "'partial'" not in deciding and "status = 'succeeded'" in deciding
