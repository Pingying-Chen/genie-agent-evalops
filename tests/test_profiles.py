"""Space profiles: each space changes the library's rules, models
and thresholds for itself, and every change is a version that can be compared and restored."""

from pathlib import Path

import pytest
from test_api import VIEWER, FakeSql, api, genie, sql  # noqa: F401  (fixtures)

from evalops.clients.lakebase import InMemoryStore
from evalops.config import Settings
from evalops.errors import Conflict, ValidationFailed
from evalops.schemas.checks import Gate
from evalops.schemas.profile import ProfileSave, SkillSetting, SpaceProfile
from evalops.services import profiles
from evalops.services.criteria import criteria_dir, load_criteria
from evalops.services.llm.skills import skills_dir

DEMO = Path(__file__).resolve().parents[1] / "demo" / "criteria"
CRITERIA, CRITERIA_SET = load_criteria(criteria_dir(), DEMO)
LIBRARY = profiles.Library(CRITERIA, CRITERIA_SET, skills_dir(""), Settings())
CUSTOM = {
    "id": "rubric.states_currency",
    "title": "Names the currency",
    "version": 1,
    "family": "rubric",
    "layer": "behaviour",
    "gate": "monitor",
    "applies_to": {"level": "report"},
    "description": "Money figures name their currency.",
    "params": {
        "skill": "rubric_judge",
        "rule": "Every money figure names its currency.",
        "failure": "A bare amount.",
    },
}


def save(**body) -> ProfileSave:
    return ProfileSave(**{"base_version": 0, **body})


def test_a_space_without_a_profile_gets_the_library_unchanged():
    eff = profiles.resolve(LIBRARY, SpaceProfile(space_id="s", version=0))
    assert [c.id for c in eff.criteria] == [c.id for c in CRITERIA]
    assert eff.criteria_set == CRITERIA_SET  # comparable with runs from before profiles existed
    assert eff.thresholds["eval_k"] == Settings().eval_k
    assert eff.skill("rubric_judge").ref == "rubric_judge@5" and eff.skill("rubric_judge").endpoint is None


def test_a_profile_turns_rules_off_changes_gates_and_params_and_adds_its_own_judges():
    profile = SpaceProfile(
        space_id="s",
        version=3,
        rules={
            "tool.explore_step": {"enabled": False},
            "report.states_period": {"gate": "hard"},
            "sql.restricted_columns": {"params": {"restricted_columns": ["ssn"]}},
        },
        custom_rules=[CUSTOM],
        thresholds={"eval_k": 5},
    )
    eff = profiles.resolve(LIBRARY, profile)
    by_id = {c.id: c for c in eff.criteria}

    assert "tool.explore_step" not in by_id
    assert by_id["report.states_period"].gate == Gate.HARD
    restricted = by_id["sql.restricted_columns"].params
    assert restricted["restricted_columns"] == ["ssn"] and "restricted_tables" in restricted  # merged
    assert "rubric.states_currency" in by_id and eff.criteria_set != CRITERIA_SET
    assert eff.thresholds["eval_k"] == 5 and eff.thresholds["review_budget"] == Settings().review_budget


def test_each_skill_can_have_its_own_model_and_an_override_is_its_own_version():
    profile = SpaceProfile(
        space_id="s",
        version=1,
        skills={
            "space_change_proposer": SkillSetting(
                model="databricks-gpt-oss-120b", reasoning_effort="high", version=1
            ),
            "intent_tagger": SkillSetting(
                model="databricks-gemma-3-12b"
            ),  # model only: same behaviour version
        },
    )
    eff = profiles.resolve(LIBRARY, profile)
    proposer, tagger = eff.skill("space_change_proposer"), eff.skill("intent_tagger")
    assert (
        proposer.endpoint == "databricks-gpt-oss-120b" and proposer.model_params["reasoning_effort"] == "high"
    )
    assert proposer.ref.endswith("~s1")  # calibrated and recorded apart from the library's prompt
    assert tagger.ref == "intent_tagger@1" and eff.model_of(tagger) == "databricks-gemma-3-12b"
    assert eff.manifest_settings()["skill_models"]["rubric_judge"] == Settings().judge_model


async def test_every_save_is_a_version_and_a_stale_edit_is_refused():
    store = InMemoryStore()
    first = await profiles.save(store, LIBRARY, "s", save(thresholds={"eval_k": 5}, note="k=5"), "owner@x")
    second = await profiles.save(
        store, LIBRARY, "s", save(base_version=1, thresholds={"eval_k": 1}), "owner@x"
    )
    assert (first.version, second.version, second.thresholds.eval_k) == (1, 2, 1)

    with pytest.raises(Conflict):  # edited version 1, but 2 was saved since
        await profiles.save(store, LIBRARY, "s", save(base_version=1), "other@x")

    restored = await profiles.restore(store, LIBRARY, "s", 1, "owner@x")
    assert (restored.version, restored.thresholds.eval_k, restored.note) == (3, 5, "Restored version 1")
    assert [p.version for p in await profiles.history(store, "s")] == [3, 2, 1]  # history only grows

    found = profiles.diff(first, second)
    assert found.changes == [{"path": "thresholds.eval_k", "before": 5, "after": 1}]


async def test_a_judge_that_changes_without_a_new_version_is_refused():
    store = InMemoryStore()
    await profiles.save(store, LIBRARY, "s", save(custom_rules=[CUSTOM]), "owner@x")
    reworded = {**CUSTOM, "params": {**CUSTOM["params"], "rule": "Money figures name a currency code."}}
    with pytest.raises(ValidationFailed, match="raise its version"):
        await profiles.save(store, LIBRARY, "s", save(base_version=1, custom_rules=[reworded]), "owner@x")
    ok = await profiles.save(
        store, LIBRARY, "s", save(base_version=1, custom_rules=[{**reworded, "version": 2}]), "o"
    )
    assert ok.custom_rules[0].version == 2

    with pytest.raises(ValidationFailed, match="needs a version"):
        await profiles.save(
            store, LIBRARY, "s", save(base_version=2, skills={"rubric_judge": {"temperature": 0.7}}), "o"
        )
    await profiles.save(
        store,
        LIBRARY,
        "s",
        save(base_version=2, skills={"rubric_judge": {"temperature": 0.7, "version": 1}}),
        "o",
    )
    with pytest.raises(ValidationFailed, match="raise it"):
        await profiles.save(
            store,
            LIBRARY,
            "s",
            save(base_version=3, skills={"rubric_judge": {"temperature": 0.2, "version": 1}}),
            "o",
        )


def test_a_profile_must_name_what_the_library_has_and_keep_the_prompts_placeholders():
    previous = SpaceProfile(space_id="s", version=0)
    bad = [
        (save(rules={"rule.nope": {"enabled": False}}), "Unknown rules"),
        (save(rules={"rubric.ambiguous_metric": {"params": {"rule": "x"}}}), "custom rule"),
        (save(custom_rules=[{**CUSTOM, "id": "rubric.ambiguous_metric"}]), "already used"),
        (save(skills={"rubric_judge": {"prompt": "Judge it.", "version": 1}}), "must keep"),
        (save(skills={"magic": {}}), "Unknown skills"),
    ]
    for body, message in bad:
        with pytest.raises(ValidationFailed, match=message):
            profiles.validate(LIBRARY, body, previous)


def test_the_profile_round_trips_through_yaml():
    profile = SpaceProfile(
        space_id="s", version=4, thresholds={"eval_k": 2}, gate={"min_test_pass_rate": 0.8}
    )
    back = profiles.parse_yaml(profiles.export_yaml(profile))
    assert back.thresholds.eval_k == 2 and back.gate.min_test_pass_rate == 0.8


def test_the_profile_api_saves_versions_and_shows_what_applies(api):  # noqa: F811
    url = "/api/spaces/space-ok/profile"
    assert api.get(url, headers=VIEWER).json()["version"] == 0
    saved = api.put(
        url, json={"base_version": 0, "rules": {"tool.explore_step": {"enabled": False}}}, headers=VIEWER
    )
    assert saved.status_code == 200 and saved.json()["version"] == 1
    assert api.put(url, json={"base_version": 0}, headers=VIEWER).status_code == 409

    effective = api.get(f"{url}/effective", headers=VIEWER).json()
    explore = next(r for r in effective["rules"] if r["id"] == "tool.explore_step")
    assert (explore["enabled"], explore["source"]) == (False, "override")
    assert (
        api.get(f"{url}/diff?a=0&b=1", headers=VIEWER).json()["changes"][0]["path"]
        == "rules.tool.explore_step.enabled"
    )
    assert "rubric_judge" in {s["name"] for s in api.get(f"{url}/library", headers=VIEWER).json()["skills"]}
    assert api.get("/api/spaces/space-no/profile", headers=VIEWER).status_code in (403, 404)
