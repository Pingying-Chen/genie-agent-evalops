"""Eval set API: cases are versioned owner edits, promoted from reviewed
answers, exported to YAML and imported back."""

import pytest
from test_api import VIEWER, FakeSql, api, genie, sql  # noqa: F401  (fixtures)

URL = "/api/spaces/space-ok/evalset"
CASE = {
    "title": "Revenue in 2024",
    "turns": ["What was realized revenue?", "Only 2024"],
    "expect": {"criteria": {"report.states_period": "pass"}, "facts": ["covers 2024 only"]},
}


class DraftSql(FakeSql):
    """Adds the three reads of a draft: the conversation's questions, the review, the labels."""

    async def execute(self, statement, parameters=None):
        if "me.turn_index" in statement:
            self.calls.append((statement, parameters or {}))
            return [
                {"question": "Top destinations?", "turn_index": 0, "conversation_id": "c1"},
                {"question": "and in Asia?", "turn_index": 1, "conversation_id": "c1"},
            ]
        if "max_by(task_success, updated_at) AS task_success,\n" in statement and "GROUP BY" not in statement:
            return [{"task_success": "fail", "task_rationale": "answered Europe, not Asia"}]
        if "FROM" in statement and "human_labels" in statement and "max_by(label" in statement:
            return [
                {"criterion_id": "report.states_period", "label": "fail"},
                {"criterion_id": "x", "label": "na"},
            ]
        return await super().execute(statement, parameters)


@pytest.fixture
def sql():  # noqa: F811
    return DraftSql()


def test_a_case_is_versioned_on_every_edit_and_a_stale_edit_is_refused(api):  # noqa: F811
    created = api.post(URL, json=CASE, headers=VIEWER)
    assert created.status_code == 201
    case = created.json()
    assert (case["version"], case["updated_by"], case["status"]) == (1, "jane@example.com", "active")

    edited = api.put(
        f"{URL}/{case['case_id']}",
        json={**CASE, "title": "Revenue, 2024 only", "base_version": 1},
        headers=VIEWER,
    )
    stale = api.put(
        f"{URL}/{case['case_id']}", json={**CASE, "title": "Mine", "base_version": 1}, headers=VIEWER
    )
    retired = api.put(
        f"{URL}/{case['case_id']}", json={**CASE, "status": "retired", "base_version": 2}, headers=VIEWER
    )

    assert edited.json()["version"] == 2
    assert stale.status_code == 409  # someone saved version 2 first
    assert retired.json()["status"] == "retired"
    history = api.get(f"{URL}/{case['case_id']}/history", headers=VIEWER).json()
    assert [h["version"] for h in history] == [1, 2, 3]
    [current] = api.get(URL, headers=VIEWER).json()["cases"]
    assert (current["version"], current["status"]) == (3, "retired")


def test_a_case_may_only_name_loaded_rules_and_the_spaces_own_categories(api):  # noqa: F811
    rule = api.post(URL, json={**CASE, "expect": {"criteria": {"no.such_rule": "pass"}}}, headers=VIEWER)
    category = api.post(URL, json={**CASE, "category_ids": ["nope"]}, headers=VIEWER)
    sql_write = api.post(URL, json={**CASE, "expect": {"reference_sql": "DELETE FROM t"}}, headers=VIEWER)
    two = api.post(URL, json={**CASE, "expect": {"reference_sql": "SELECT 1; SELECT 2"}}, headers=VIEWER)
    past = api.post(URL, json={**CASE, "expect": {"turn": 2}}, headers=VIEWER)

    assert [r.status_code for r in (rule, category, sql_write, two, past)] == [422] * 5
    assert "no.such_rule" in rule.json()["error"]["message"]


def test_a_draft_from_an_answer_has_the_conversation_up_to_it_and_what_people_said(api):  # noqa: F811
    draft = api.get(f"{URL}/draft?trace_id=t2", headers=VIEWER).json()

    assert draft["turns"] == ["Top destinations?", "and in Asia?"]
    assert draft["expect"]["criteria"] == {"report.states_period": "pass"}  # people said it applies
    assert draft["expect"]["facts"] == ["Fixes what the reviewer found: answered Europe, not Asia"]
    assert draft["source"] == {"trace_id": "t2", "conversation_id": "c1", "reviewer_verdict": "fail"}
    assert api.post(URL, json=draft, headers=VIEWER).status_code == 201  # saveable as it is


def test_export_then_import_creates_nothing_new_and_an_edit_becomes_a_version(api):  # noqa: F811
    api.post(URL, json=CASE, headers=VIEWER)
    text = api.get(f"{URL}/export", headers=VIEWER).text
    assert "Only 2024" in text

    same = api.post(f"{URL}/import", content=text, headers={**VIEWER, "content-type": "application/yaml"})
    edited = text.replace("covers 2024 only", "covers calendar 2024 only")
    changed = api.post(
        f"{URL}/import", content=edited, headers={**VIEWER, "content-type": "application/yaml"}
    )
    new = "cases:\n- title: New one\n  turns: ['How many bookings?']\n"
    added = api.post(f"{URL}/import", content=new, headers={**VIEWER, "content-type": "application/yaml"})
    broken = api.post(
        f"{URL}/import", content="cases: [{title: x}]", headers={**VIEWER, "content-type": "application/yaml"}
    )

    assert same.json() == {"created": 0, "updated": 0, "unchanged": 1}
    assert changed.json() == {"created": 0, "updated": 1, "unchanged": 0}
    assert added.json()["created"] == 1
    assert broken.status_code == 422  # no turns: nothing from the file is saved
    assert len(api.get(URL, headers=VIEWER).json()["cases"]) == 2


def test_coverage_lists_categories_without_an_active_case_first(api):  # noqa: F811
    covered = api.post(
        "/api/spaces/space-ok/categories", json={"name": "Wrong filter", "impact": 3}, headers=VIEWER
    ).json()
    api.post("/api/spaces/space-ok/categories", json={"name": "Leaks internals", "impact": 2}, headers=VIEWER)
    api.post(URL, json={**CASE, "category_ids": [covered["id"]]}, headers=VIEWER)

    coverage = api.get(URL, headers=VIEWER).json()["coverage"]

    assert [(c["name"], c["active_cases"]) for c in coverage] == [("Leaks internals", 0), ("Wrong filter", 1)]


def test_the_eval_set_is_for_owners_only(api):  # noqa: F811
    assert api.get("/api/spaces/space-other/evalset", headers=VIEWER).status_code == 403
    assert api.post("/api/spaces/space-other/evalset", json=CASE, headers=VIEWER).status_code == 403


def test_regression_runs_are_for_owners_only(api):  # noqa: F811
    assert api.get("/api/spaces/space-other/evaluations", headers=VIEWER).status_code == 403
    assert api.post("/api/spaces/space-other/evaluations", json={}, headers=VIEWER).status_code == 403


SPACE_CONFIG = {
    "title": "T",
    "warehouse_id": "wh",
    "serialized_space": '{"instructions": {"text_instructions": [{"id": "a", "content": ["R1 one\\n"]}]}, "data_sources": {"tables": [{"identifier": "c.s.v"}]}}',
}


@pytest.fixture
def genie():  # noqa: F811
    from test_api import FakeGenie

    from evalops.schemas.genie import SpaceConfig

    class WithConfig(FakeGenie):
        async def get_space_config(self, space_id):
            return SpaceConfig.model_validate(SPACE_CONFIG)

    return WithConfig()


def test_a_change_is_checked_against_the_live_configuration_and_can_be_discarded(api):  # noqa: F811
    url = "/api/spaces/space-ok/changes"
    case = api.post(URL, json=CASE, headers=VIEWER).json()
    body = {
        "title": "State the threshold",
        "operations": [{"op": "add_instruction", "text": "R2 Keep the threshold the user gave."}],
        "target_case_ids": [case["case_id"]],
    }
    created = api.post(url, json=body, headers=VIEWER)
    assert created.status_code == 201
    change = created.json()
    assert change["steps"] == ["Add instruction: R2 Keep the threshold the user gave."]
    assert change["status"] == "draft" and len(change["base_version"]) == 12

    assert change["leaks"] == []
    example = {"op": "add_sql_example", "question": "Only 2024", "sql": "SELECT 1"}  # turn 2 of the case
    leaky = api.post(url, json={**body, "operations": [example]}, headers=VIEWER).json()
    assert [(lk["case_id"], lk["turn"]) for lk in leaky["leaks"]] == [
        (case["case_id"], 1)
    ]  # a warning, still saved

    misfit = api.post(
        url, json={**body, "operations": [{"op": "remove_instruction", "old": "R9"}]}, headers=VIEWER
    )
    unknown = api.post(url, json={**body, "target_case_ids": ["nope"]}, headers=VIEWER)
    assert misfit.status_code == 422 and unknown.status_code == 422

    discarded = api.post(f"{url}/{change['change_id']}/discard", headers=VIEWER).json()
    assert discarded["status"] == "discarded"
    assert api.post(f"{url}/{change['change_id']}/test", headers=VIEWER).status_code == 409
    assert api.get("/api/spaces/space-other/changes", headers=VIEWER).status_code == 403
