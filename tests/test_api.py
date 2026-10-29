"""API-layer tests: identity, permission gating, the read endpoints and the error envelope.

No Databricks: `dependency_overrides` swap the viewer's Genie client and the SQL client for
fakes, and the app identity is a placeholder, so no test can reach a real workspace.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from evalops import main
from evalops.api.deps import get_sql, get_viewer_genie, get_viewer_scim
from evalops.clients.auth import Auth
from evalops.schemas.genie import GenieSpace

VIEWER = {"x-forwarded-access-token": "obo-token", "x-forwarded-email": "jane@example.com"}
ASKED = datetime(2026, 10, 19, 9, tzinfo=UTC)


AGREEMENT_ROW = {
    "computed_at": ASKED,
    "space_id": "space-ok",
    "criterion_id": "rubric.out_of_scope_refusal",
    "criterion_version": 1,
    "judge": "rubric_judge@4|gpt-oss|samples=3,temperature=1.0",
    "n": 7,
    "n_judge_error": 0,
    "tp": 2,
    "fn": 0,
    "fp": 1,
    "tn": 4,
    "exact": 6,
    "tpr": 1.0,
    "tnr": 0.8,
    "tpr_lb": 0.34,
    "tnr_lb": 0.38,
    "calibrated": False,
    "n_by_reason": '{"random": 4, "judge_fail": 3}',  # stored as JSON text
    "positive_class": "pass",  # TPR = good answers passed (rows before 2026-10-27 have none)
}


class FakeGenie:
    """Viewer's view of Genie: sees three spaces, manages two of them."""

    def __init__(self):
        self.calls = 0

    async def list_spaces(self):
        return [GenieSpace(space_id=s, title=f"Title {s}") for s in ("space-ok", "space-other", "space-two")]

    async def can_manage(self, space_id: str) -> bool:
        self.calls += 1
        return space_id in ("space-ok", "space-two")


class FakeScim:
    async def my_groups(self):
        return ["users", "genie-reviewers-space-two"]


DISAGREEMENT = {
    "trace_id": "t1",
    "criterion_id": "rubric.ambiguous_metric",
    "criterion_version": 2,
    "reviewer": "r@x.com",
    "label": "fail",
    "rationale": "never says which measure",
    "verdict": "pass",
    "evidence": "{}",
    "question": "Which destinations are best?",
    "asked_at": None,
}


class FakeSql:
    """Answers each read query with canned rows and records every (statement, parameters)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters or {}))
        if "AS unanimous" in statement:
            row = {
                "criterion_id": "rubric.out_of_scope_refusal",
                "criterion_version": 1,
                "verdicts": 20,
                "na": 12,
            }
            return [
                {**row, "errors": 1, "sampled": 7, "unanimous": 5, "judged": 3, "tokens_per_verdict": 812.5}
            ]
        if "date_trunc('WEEK'" in statement:
            week = datetime(2026, 10, 19, tzinfo=UTC)  # a Monday
            if "AS applies" in statement:
                return [
                    {"week": week, "criterion_id": "rubric.out_of_scope_refusal", "applies": 10, "fails": 3}
                ]
            return [
                {
                    "week": week,
                    "answers": 9,
                    "graded": 8,
                    "checked": 8,
                    "hard_failed": 2,
                    "reviewed": 5,
                    "task_passed": 4,
                }
            ]
        if "count(*) AS n" in statement:
            return [{"access_status": "ok", "n": 21}, {"access_status": "owner_only", "n": 2}]
        if "count(DISTINCT i.trace_id)" in statement:
            return [{"queued": 25, "labeled": 7}]
        if "AS applies" in statement:
            return [{"criterion_id": "rubric.out_of_scope_refusal", "applies": 10, "fails": 3}]
        if "l.label <> r.verdict" in statement:
            judge = '{"skill": "rubric_judge@4", "model": "m", "model_params": {"samples": 3}'
            return [
                {**DISAGREEMENT, "trace_id": "t5", "label": "na", "verdict": "pass"},
                {
                    **DISAGREEMENT,
                    "trace_id": "t3",
                    "criterion_id": "rubric.out_of_scope_refusal",
                    "label": "pass",
                    "verdict": "fail",
                    "evidence": judge + ', "reason": "named a host"}',
                },
                {
                    **DISAGREEMENT,
                    "evidence": judge
                    + ', "reason": "states the measure", "votes": ["pass", "pass", "fail"]}',
                },
            ]
        if "AS caught_by" in statement:
            return [
                {
                    "trace_id": "t2",
                    "task_success": "fail",
                    "task_rationale": "answered Europe, not Asia",
                    "findings": "Region filter ignored",
                    "reviewer": "a@x.com",
                    "question": "and in Asia?",
                    "turn_index": 1,
                    "asked_at": None,
                    "caught_by": [],
                    "suggested_category": "Ignores the region",
                    "suggested_description": "Answers for another region than the one asked.",
                    "suggested_reason": "The reviewer says Europe was answered, not Asia.",
                },
                {
                    "trace_id": "t4",
                    "task_success": "pass",
                    "task_rationale": "ok",
                    "findings": "Shows internal table names",
                    "reviewer": "a@x.com",
                    "question": "q4",
                    "turn_index": 0,
                    "asked_at": None,
                    "caught_by": ["report.no_contact_details"],
                },
            ]
        if "AS false_alarms" in statement:
            return [{"criterion_id": "report.no_contact_details", "false_alarms": 2}]
        if "AS reviewed" in statement:
            return [{"reviewed": 13, "passed": 11}]
        if "trace_reviews" in statement:
            return [
                {
                    "trace_id": "t1",
                    "conversation_id": "c1",
                    "turn_index": 0,
                    "task_success": "fail",
                    "task_rationale": "wrong year",
                    "findings": None,
                    "question": "q1",
                    "caught": 1,
                },
                {
                    "trace_id": "t2",
                    "conversation_id": "c2",
                    "turn_index": 0,
                    "task_success": "fail",
                    "task_rationale": "answered Europe, not Asia",
                    "findings": "Region filter ignored",
                    "question": "q2",
                    "caught": 0,
                },
                {
                    "trace_id": "t3",
                    "conversation_id": "c1",
                    "turn_index": 1,
                    "task_success": "pass",
                    "task_rationale": "ok",
                    "findings": None,
                    "question": "q3",
                    "caught": 1,
                },
            ]
        if "judge_agreement" in statement:
            return [dict(AGREEMENT_ROW)]  # a fresh row per query, like a real result set
        if "SELECT space_id FROM" in statement:
            return [{"space_id": "space-other" if parameters["trace_id"] == "t-other" else "space-ok"}]
        if "collect_set" in statement:
            return [
                {
                    "trace_id": "t1",
                    "conversation_id": "c1",
                    "asked_at": ASKED,
                    "question": "q1",
                    "access_status": "ok",
                    "verdict": "fail",
                    "fails": ["report.figure_sourced"],
                },
                {
                    "trace_id": "t0",
                    "conversation_id": "c0",
                    "asked_at": ASKED,
                    "question": "q0",
                    "access_status": "ok",
                    "verdict": "pass",
                    "fails": [],
                },
            ][: int(statement.rsplit("LIMIT", 1)[1])]
        return []


@pytest.fixture
def genie() -> FakeGenie:
    return FakeGenie()


@pytest.fixture
def sql() -> FakeSql:
    return FakeSql()


@pytest.fixture
def api(genie, sql, monkeypatch):
    """A fresh app per test with fakes injected. `_app_auth` is stubbed so local credentials,
    if any, are never picked up."""
    monkeypatch.setattr(
        main, "_app_auth", lambda: Auth(host="https://unused", headers=lambda: {}, principal="test")
    )
    app = main.create_app()
    app.dependency_overrides[get_viewer_genie] = lambda: genie
    app.dependency_overrides[get_sql] = lambda: sql
    app.dependency_overrides[get_viewer_scim] = lambda: FakeScim()
    with TestClient(app) as client:
        yield client


# ------------------------------------------------------------------ identity and errors


def test_health_needs_no_identity(api):
    body = api.get("/api/health").json()
    assert body["state_store"] == "InMemoryStore" and body["criteria_version"].startswith("sha256:")


def test_missing_identity_is_401_in_the_error_envelope(api):
    response = api.get("/api/me")

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert body["error"]["request_id"] == response.headers["x-request-id"]


def test_a_caller_supplied_request_id_is_echoed(api):
    assert api.get("/api/health", headers={"x-request-id": "abc-123"}).headers["x-request-id"] == "abc-123"


def test_invalid_query_parameters_are_422_in_the_same_envelope(api):
    response = api.get("/api/spaces/space-ok/traces?limit=0", headers=VIEWER)

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_request"
    assert body["details"]["errors"][0]["loc"] == ["query", "limit"]


# ------------------------------------------------------------------ permission gating


def test_me_lists_only_the_spaces_the_viewer_manages(api):
    assert api.get("/api/me", headers=VIEWER).json() == {
        "email": "jane@example.com",
        "allowed_space_ids": ["space-ok", "space-two"],
        "review_space_ids": ["space-two"],
        "review_titles": {},
        "review_sessions": {},
    }


def test_me_registers_the_viewer_as_a_reviewer_and_links_their_session(api):
    store = api.app.state.store
    api.get("/api/me", headers=VIEWER)
    assert store.reviewers == {"space-two": {"jane@example.com"}}

    asyncio.run(store.put_review_session("space-two", "exp-1", "sess-1", "https://review/sess-1"))
    assert api.get("/api/me", headers=VIEWER).json()["review_sessions"] == {
        "space-two": "https://review/sess-1"
    }


def test_a_space_the_viewer_does_not_manage_is_403(api, sql):
    response = api.get("/api/spaces/space-other/summary", headers=VIEWER)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "space_not_allowed"
    assert sql.calls == []  # refused before any data was read


def test_the_permission_check_is_cached(api, genie):
    api.get("/api/spaces/space-ok/summary", headers=VIEWER)
    api.get("/api/spaces/space-ok/traces", headers=VIEWER)

    assert genie.calls == 1  # two requests, one Genie call


def test_a_trace_in_a_space_the_viewer_does_not_manage_is_404_not_403(api):
    response = api.get("/api/traces/t-other", headers=VIEWER)

    assert response.status_code == 404  # never reveal that the trace exists


# ------------------------------------------------------------------ read endpoints


def test_summary_returns_coverage_for_the_window(api, sql):
    body = api.get("/api/spaces/space-ok/summary?from=2026-10-01&to=2026-10-19", headers=VIEWER).json()

    assert body["coverage"] == {"total": 23, "by_status": {"ok": 21, "owner_only": 2}}
    assert body["window_from"] == "2026-10-01"
    _, params = sql.calls[0]
    assert params["space_id"] == "space-ok"  # the filter is a parameter, not string formatting


def test_trace_list_is_filtered_by_space_and_paginates(api, sql):
    first = api.get("/api/spaces/space-ok/traces?limit=1", headers=VIEWER).json()

    assert [i["trace_id"] for i in first["items"]] == ["t1"]
    assert first["items"][0]["failed_criteria"] == ["report.figure_sourced"]
    assert first["next_cursor"]  # there was a second row
    statement, params = sql.calls[-1]
    assert params["space_id"] == "space-ok"
    assert statement.rstrip().endswith("LIMIT 2")  # limit + 1 to detect a next page; an INT literal

    api.get(f"/api/spaces/space-ok/traces?limit=1&cursor={first['next_cursor']}", headers=VIEWER)
    _, params = sql.calls[-1]
    assert params["c_id"] == "t1"  # keyset: continue after the last item


def test_calibration_reports_review_progress_and_agreement(api, sql):
    body = api.get("/api/spaces/space-ok/calibration", headers=VIEWER).json()

    assert (body["queued"], body["labeled"]) == (25, 7)
    assert body["min_lower_bound"] == 0.8
    [judge] = body["judges"]
    assert judge["n_by_reason"] == {"random": 4, "judge_fail": 3}  # parsed from the JSON text
    assert judge["calibrated"] is False
    assert body["computed_at"].startswith("2026-10-19")
    assert all(params["space_id"] == "space-ok" for _, params in sql.calls)


def test_a_judge_row_shows_its_observed_fails_and_a_corrected_rate_once_it_beats_chance(api, monkeypatch):
    [judge] = api.get("/api/spaces/space-ok/calibration", headers=VIEWER).json()["judges"]
    assert (judge["observed_applies"], judge["observed_fails"]) == (10, 3)
    # It catches every failure but flags 20% of good answers, so some of its 30% are false alarms.
    # Rogan-Gladen with TPR 1.0 (good answers passed) and TNR 0.8 (bad answers failed).
    assert judge["corrected_fail_rate"] == pytest.approx((0.3 + 1.0 - 1) / (1.0 + 0.8 - 1))

    monkeypatch.setitem(AGREEMENT_ROW, "tpr", 0.5)
    monkeypatch.setitem(AGREEMENT_ROW, "tnr", 0.5)  # a coin: its verdicts say nothing
    [judge] = api.get("/api/spaces/space-ok/calibration", headers=VIEWER).json()["judges"]
    assert judge["corrected_fail_rate"] is None


def test_agreement_rows_written_before_applicability_existed_still_load(api, monkeypatch):
    monkeypatch.setitem(AGREEMENT_ROW, "n_applies", None)  # NULL in an old Delta row

    [judge] = api.get("/api/spaces/space-ok/calibration", headers=VIEWER).json()["judges"]

    assert judge["n_applies"] == 0


def test_calibration_measures_rule_coverage_of_failed_answers(api):
    body = api.get("/api/spaces/space-ok/calibration", headers=VIEWER).json()

    assert (body["reviewed"], body["task_failed"], body["caught"]) == (3, 2, 1)
    [spot] = body["blind_spots"]  # failed by the reviewer, passed by every check
    assert (spot["trace_id"], spot["findings"]) == ("t2", "Region filter ignored")


def test_agent_performance_has_every_week_and_corrects_the_judge_rate(api, sql, monkeypatch):
    monkeypatch.setattr(
        "evalops.api.routers.spaces.date",
        type("D", (), {"today": staticmethod(lambda: datetime(2026, 10, 22).date())}),
    )
    weeks = api.get("/api/spaces/space-ok/performance/agent?weeks=3", headers=VIEWER).json()["weeks"]

    assert [w["week"] for w in weeks] == ["2026-10-05", "2026-10-12", "2026-10-19"]
    assert weeks[0]["answers"] == 0 and weeks[0]["task_low"] is None  # an empty week is still drawn
    last = weeks[-1]
    assert (last["checked"], last["hard_failed"], last["reviewed"], last["task_passed"]) == (8, 2, 5, 4)
    assert 0 < last["task_low"] < 0.8 < last["task_high"] < 1  # 4 of 5 is a wide interval
    [judge] = last["judges"]
    # Rogan-Gladen with TPR 1.0 (good answers passed) and TNR 0.8 (bad answers failed).
    assert judge["corrected_fail_rate"] == pytest.approx((0.3 + 1.0 - 1) / (1.0 + 0.8 - 1))
    assert all(params["space_id"] == "space-ok" for _, params in sql.calls)


def test_judge_performance_gives_each_judge_a_track_of_review_runs(api, monkeypatch):
    later = {**AGREEMENT_ROW, "computed_at": datetime(2026, 10, 21, tzinfo=UTC), "tp": 3, "kappa": None}
    newer_judge = {**later, "judge": "rubric_judge@5|gpt-oss|samples=3,temperature=1.0"}
    rerun = {**later, "computed_at": datetime(2026, 10, 22, tzinfo=UTC)}  # no new label: not a new point
    first = {**AGREEMENT_ROW, "n_applies": 6}
    before_8e = {**AGREEMENT_ROW, "computed_at": datetime(2026, 10, 18, tzinfo=UTC), "n_applies": None}
    later, newer_judge, rerun = ({**r, "n_applies": 6} for r in (later, newer_judge, rerun))
    rows = [later, newer_judge, rerun, first, before_8e]  # out of order, as Delta may return them
    monkeypatch.setattr(FakeSql, "execute", _answer_judge_agreement(FakeSql.execute, rows))

    body = api.get("/api/spaces/space-ok/performance/judges", headers=VIEWER).json()

    old, new = body["tracks"]  # a new judge version starts its own line
    assert [p["tp"] for p in old["points"]] == [2, 3]
    assert old["judge"].startswith("rubric_judge@4") and len(new["points"]) == 1
    [latest] = body["latest"]
    assert (latest["sampled"], latest["unanimous"], latest["judged"]) == (7, 5, 3)
    assert body["min_lower_bound"] == 0.8


def _answer_judge_agreement(execute, rows):
    async def patched(self, statement, parameters=None):
        if "FROM" in statement and "judge_agreement WHERE space_id = :space_id" in statement:
            self.calls.append((statement, parameters or {}))
            return [dict(r) for r in rows]
        return await execute(self, statement, parameters)

    return patched


def test_performance_is_for_owners_only(api):
    for part in ("agent", "judges", "reviewers"):
        assert api.get(f"/api/spaces/space-other/performance/{part}", headers=VIEWER).status_code == 403


def test_observations_list_failed_turns_and_findings_with_what_caught_them(api, sql):
    body = api.get("/api/spaces/space-ok/observations", headers=VIEWER).json()

    assert body["reviewed"] == 13  # the denominator for how often a failure happens
    blind, noted = body["items"]
    assert (blind["task_success"], blind["turn_index"], blind["caught_by"]) == ("fail", 1, [])
    assert (noted["task_success"], noted["findings"]) == ("pass", "Shows internal table names")
    statement, params = next(c for c in sql.calls if "AS caught_by" in c[0])
    assert params == {"space_id": "space-ok"}
    assert (
        "tr.task_success = 'fail' OR tr.findings IS NOT NULL" in statement
    )  # passes without notes add nothing


def test_an_owner_names_categories_tags_observations_and_sees_them_ranked(api):
    url = "/api/spaces/space-ok"
    filters = api.post(f"{url}/categories", json={"name": "Wrong filter", "impact": 3}, headers=VIEWER)
    assert filters.status_code == 201
    names = api.post(
        f"{url}/categories", json={"name": "Leaks internals", "impact": 2}, headers=VIEWER
    ).json()
    clash = api.post(f"{url}/categories", json={"name": "  wrong   FILTER "}, headers=VIEWER)
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "conflict"

    filters = filters.json()
    assert filters["updated_by"] == "jane@example.com"
    tagged = api.put(
        f"{url}/observations/t2/categories", json={"category_ids": [filters["id"]]}, headers=VIEWER
    )
    assert tagged.json() == {"category_ids": [filters["id"]]}
    api.put(f"{url}/observations/t4/categories", json={"category_ids": [names["id"]]}, headers=VIEWER)

    body = api.get(f"{url}/observations", headers=VIEWER).json()
    assert {i["trace_id"]: i["category_ids"] for i in body["items"]} == {
        "t2": [filters["id"]],
        "t4": [names["id"]],
    }
    top, second = body["categories"]  # 1/13 = 8% -> 3; 3 x 3 = 9 before 3 x 2 = 6
    assert (top["priority"], second["priority"]) == (9, 6)
    assert (top["name"], top["turns"], top["failed"], top["caught"]) == ("Wrong filter", 1, 1, 0)
    assert (second["name"], second["caught"]) == ("Leaks internals", 1)

    api.delete(f"{url}/categories/{filters['id']}", headers=VIEWER)
    body = api.get(f"{url}/observations", headers=VIEWER).json()
    assert [c["name"] for c in body["categories"]] == ["Leaks internals"]
    assert {i["trace_id"]: i["category_ids"] for i in body["items"]}["t2"] == []  # its tags went with it


def test_a_suggestion_shows_until_the_owner_tags_and_points_at_a_category_once_it_exists(api):
    url = "/api/spaces/space-ok"

    def t2():
        return next(
            i for i in api.get(f"{url}/observations", headers=VIEWER).json()["items"] if i["trace_id"] == "t2"
        )

    assert (
        t2()["suggestion"]["category"] == "Ignores the region" and t2()["suggestion"]["category_id"] is None
    )
    cid = api.post(f"{url}/categories", json={"name": "ignores the REGION"}, headers=VIEWER).json()["id"]
    assert t2()["suggestion"]["category_id"] == cid  # accepting it is now a tag, not a new category
    api.put(f"{url}/observations/t2/categories", json={"category_ids": [cid]}, headers=VIEWER)
    assert t2()["suggestion"] is None


def test_a_category_links_only_to_a_loaded_criterion_and_can_be_unlinked(api):
    url = "/api/spaces/space-ok/categories"
    cid = api.post(url, json={"name": "Wrong filter"}, headers=VIEWER).json()["id"]

    assert api.patch(f"{url}/{cid}", json={"criterion_id": "no.such_rule"}, headers=VIEWER).status_code == 404
    linked = api.patch(
        f"{url}/{cid}", json={"criterion_id": "report.states_period", "impact": 3}, headers=VIEWER
    ).json()
    assert (linked["criterion_id"], linked["impact"], linked["name"]) == (
        "report.states_period",
        3,
        "Wrong filter",
    )
    unlinked = api.patch(f"{url}/{cid}", json={"criterion_id": None}, headers=VIEWER).json()
    assert (unlinked["criterion_id"], unlinked["impact"]) == (None, 3)  # only the fields sent change


def test_linking_a_category_to_a_criterion_shows_its_catches_and_false_alarms_and_drafts_a_rule(api):
    url = "/api/spaces/space-ok"
    cid = api.post(f"{url}/categories", json={"name": "Leaks internals", "impact": 4}, headers=VIEWER).json()[
        "id"
    ]
    api.put(f"{url}/observations/t4/categories", json={"category_ids": [cid]}, headers=VIEWER)
    api.patch(f"{url}/categories/{cid}", json={"criterion_id": "report.no_contact_details"}, headers=VIEWER)

    [linked] = api.get(f"{url}/observations", headers=VIEWER).json()["categories"]
    assert (linked["linked_caught"], linked["linked_false_alarms"]) == (1, 2)  # t4 is caught by it

    draft = api.get(f"{url}/categories/{cid}/draft", headers=VIEWER).json()
    assert draft["filename"] == "rubric.leaks_internals.yaml" and "#   t4  'q4'" in draft["yaml"]
    assert api.get(f"{url}/categories/nope/draft", headers=VIEWER).status_code == 404


def test_disagreements_put_missed_failures_first_with_both_reasons(api):
    items = api.get("/api/spaces/space-ok/disagreements", headers=VIEWER).json()["items"]

    assert [(d["trace_id"], d["kind"]) for d in items] == [
        ("t1", "missed_failure"),
        ("t3", "false_alarm"),
        ("t5", "applicability"),
    ]
    missed = items[0]
    assert (missed["rationale"], missed["judge_reason"]) == ("never says which measure", "states the measure")
    assert missed["votes"] == ["pass", "pass", "fail"] and missed["judge"] == "rubric_judge@4|m|samples=3"


def test_an_owner_marks_why_a_judge_disagrees_and_can_clear_it(api):
    url = "/api/spaces/space-ok/disagreements/t1/rubric.ambiguous_metric"

    marked = api.put(
        url, json={"cause": "rule_unclear", "note": " 'best' needs a measure "}, headers=VIEWER
    ).json()
    assert (marked["cause"], marked["note"]) == ("rule_unclear", "'best' needs a measure")
    [first, *_] = api.get("/api/spaces/space-ok/disagreements", headers=VIEWER).json()["items"]
    assert first["cause"] == "rule_unclear"

    api.put(url, json={"cause": None}, headers=VIEWER)
    [first, *_] = api.get("/api/spaces/space-ok/disagreements", headers=VIEWER).json()["items"]
    assert first["cause"] is None


def test_only_a_current_disagreement_can_be_marked(api):
    url = "/api/spaces/space-ok/disagreements/t9/rubric.ambiguous_metric"

    assert api.put(url, json={"cause": "judge_wrong"}, headers=VIEWER).status_code == 404
    assert api.put(url, json={"cause": "made_up"}, headers=VIEWER).status_code == 422


def test_disagreements_are_for_owners_only(api):
    # They show judge verdicts, which reviewers must not see before labelling.
    assert api.get("/api/spaces/space-other/disagreements", headers=VIEWER).status_code == 403


def test_tags_must_name_categories_of_the_same_space(api):
    response = api.put(
        "/api/spaces/space-ok/observations/t2/categories", json={"category_ids": ["nope"]}, headers=VIEWER
    )

    assert response.status_code == 404


def test_categories_are_for_owners_only(api):
    other = "/api/spaces/space-other"
    assert api.post(f"{other}/categories", json={"name": "x"}, headers=VIEWER).status_code == 403
    assert (
        api.put(f"{other}/observations/t1/categories", json={"category_ids": []}, headers=VIEWER).status_code
        == 403
    )


def test_observations_are_for_owners_only(api):
    assert api.get("/api/spaces/space-other/observations", headers=VIEWER).status_code == 403


def test_conversations_pass_strictly_only_if_every_reviewed_turn_passed(api):
    body = api.get("/api/spaces/space-ok/calibration", headers=VIEWER).json()

    # c1: turn 1 failed, turn 2 passed (recovered); c2: one failed turn.
    assert (body["conversations"], body["multi_turn"]) == (2, 1)
    assert (body["conversations_strict"], body["conversations_outcome"]) == (0, 1)


def test_calibration_is_for_owners_only(api):
    # Judge verdicts are hidden from reviewers until they label (blind review); only CAN_MANAGE sees this.
    assert api.get("/api/spaces/space-other/calibration", headers=VIEWER).status_code == 403


def test_spaces_lists_cards_for_managed_spaces(api):
    cards = api.get("/api/spaces", headers=VIEWER).json()

    assert [c["space_id"] for c in cards] == ["space-ok", "space-two"]
    # The collector has not checked them yet: unknown, not "grant CAN_MANAGE" (which may be done).
    assert all(c["monitored"] is None for c in cards)


def test_collect_without_a_configured_job_is_a_config_error(api):
    response = api.post("/api/spaces/space-ok/collect", headers=VIEWER)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "config_error"


def test_openapi_documents_the_error_responses(api):
    spec = api.get("/openapi.json").json()
    summary = spec["paths"]["/api/spaces/{space_id}/summary"]["get"]

    assert summary["responses"]["403"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "ErrorResponse"
    )
    assert "SpaceSummary" in spec["components"]["schemas"]
