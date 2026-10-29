"""Tests for human review: queue selection, agreement maths, label read-back, blind review.

MLflow is replaced by `FakeBackend` (in memory) and Delta by `FakeDelta`, so these run
without a workspace and without MLflow installed.
"""

import json
import random
from datetime import UTC, datetime

import pytest

from evalops.clients.lakebase import InMemoryStore
from evalops.schemas.checks import AppliesTo, CheckResult, Criterion, Family, Gate, Layer, Verdict
from evalops.schemas.review import HumanLabel, QueueReason, ReviewItem, TraceReview
from evalops.schemas.trace import AccessStatus, Step, StepKind, ToolName, Trace
from evalops.services.review.agreement import cohen_kappa, compute_agreement, normalized, wilson_lower_bound
from evalops.services.review.backend import (
    FINDINGS,
    TASK_SUCCESS,
    VIEW_VERSION,
    Assessment,
    readable_citations,
    turn_view,
)
from evalops.services.review.selection import parse_schema_name, schema_name, select_queue
from evalops.services.review.service import human_labels, judge_to_attach, review_space, trace_reviews

NOW = datetime(2026, 10, 20, tzinfo=UTC)
CID = "rubric.out_of_scope_refusal"
CRITERION = Criterion(
    id=CID,
    version=1,
    family=Family.RUBRIC,
    layer=Layer.BEHAVIOUR,
    gate=Gate.MONITOR,
    applies_to=AppliesTo(level="report"),
    description="Refuses what it must refuse.",
    params={"skill": "rubric_judge", "rule": "R4", "failure": "- gives a name"},
)
JUDGE = {"skill": "rubric_judge@4", "model": "m", "model_params": {"samples": 3}}


def result(trace_id: str, verdict: str, version: int = 1, **evidence) -> CheckResult:
    return CheckResult(
        run_id="r1",
        trace_id=trace_id,
        criterion_id=CID,
        criterion_version=version,
        family=Family.RUBRIC,
        layer=Layer.BEHAVIOUR,
        gate=Gate.MONITOR,
        verdict=Verdict(verdict),
        evidence={**JUDGE, "reason": f"judge says {verdict}", **evidence},
        checked_at=NOW,
    )


def label(trace_id: str, value: str, version: int = 1) -> HumanLabel:
    return HumanLabel(
        space_id="s",
        trace_id=trace_id,
        mlflow_trace_id=f"tr-{trace_id}",
        criterion_id=CID,
        criterion_version=version,
        reviewer="expert@example.com",
        label=value,
        assessment_id=f"a-{trace_id}",
        labeled_at=NOW,
    )


# ------------------------------------------------------------------ naming and maths


def test_criterion_ids_map_to_valid_versioned_label_schema_names_and_back():
    """Regressions: MLflow rejected `rubric.x` (only letters, digits and `_`), and refused to
    overwrite a schema a session uses, so each rule version gets its own name."""
    assert schema_name(CID, 2) == "rubric__out_of_scope_refusal__v2"
    assert parse_schema_name(schema_name(CID, 2)) == (CID, 2)
    assert parse_schema_name("feedback") is None


@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [(10, 10, 0.72, 0.73), (16, 16, 0.80, 0.81), (15, 15, 0.79, 0.80), (22, 22, 0.85, 0.86)],
)
def test_wilson_lower_bound_matches_the_design_doc(successes, n, low, high):
    """10/10 is only 0.72; 16 with no disagreement is the minimum for 0.80, 22 for 0.85."""
    assert low <= wilson_lower_bound(successes, n) < high


def test_wilson_lower_bound_of_nothing_is_unknown_not_zero():
    assert wilson_lower_bound(0, 0) is None


# ------------------------------------------------------------------ queue


def test_uncertain_traces_come_first_then_judge_fails_and_half_the_budget_stays_random():
    results = [
        result("fail-1", "fail"),
        result("split", "na", agreement=0.67),
        result("error", "error"),
        *[result(f"ok-{i}", "pass") for i in range(6)],
    ]

    queue = select_queue(results, set(), budget=4, rng=random.Random(0))

    assert [why for _, why in queue] == [
        QueueReason.JUDGE_UNCERTAIN,
        QueueReason.JUDGE_UNCERTAIN,
        QueueReason.RANDOM,
        QueueReason.RANDOM,
    ]  # judge_fail lost its slot to the random reserve: the unbiased sample comes first


def test_a_large_budget_takes_everything_once():
    results = [result("a", "fail"), result("b", "pass"), result("c", "pass")]

    queue = select_queue(results, {"c"}, budget=10, rng=random.Random(0))

    assert sorted(t for t, _ in queue) == ["a", "b"]  # c was already queued


# ------------------------------------------------------------------ agreement


def test_agreement_counts_pass_as_the_positive_class_as_the_playbook_does():
    """TPR = good answers the judge passed, TNR = bad answers it failed (the fail class's recall)."""
    labels = [label("t1", "fail"), label("t2", "fail"), label("t3", "pass"), label("t4", "na")]
    results = [result("t1", "fail"), result("t2", "na"), result("t3", "fail"), result("t4", "pass")]

    [row] = compute_agreement(labels, results, {}, space_id="s", now=NOW, min_lower_bound=0.8)

    # t1 caught (TN); t2 a judge na on a human fail lets a bad answer through (FP); t3 failed a
    # good answer (FN).
    assert (row.tp, row.fn, row.fp, row.tn) == (0, 1, 1, 1)
    assert (row.n, row.n_applies, row.exact) == (4, 3, 1)  # t4: the human said na, so not counted
    assert row.applicability_agree == 2  # t2 and t4 disagree on whether the rule applies
    assert (row.tpr, row.tnr, row.positive_class) == (0.0, 0.5, "pass") and row.calibrated is False


def test_a_row_stored_before_the_definitions_changed_is_read_in_todays_terms():
    old = {"tp": 4, "fn": 1, "fp": 2, "tn": 9, "tpr": 0.8, "tnr": 9 / 11, "tpr_lb": 0.4, "tnr_lb": 0.5}
    new = normalized(old)  # fail was the positive class: TP were caught failures
    assert (new["tp"], new["fn"], new["fp"], new["tn"]) == (9, 2, 1, 4)
    assert (new["tpr"], new["tnr"], new["tpr_lb"], new["tnr_lb"]) == (9 / 11, 0.8, 0.5, 0.4)
    assert normalized(new) == new  # already in today's terms


def test_turns_the_rule_does_not_apply_to_do_not_make_a_judge_look_good():
    """Most turns are na for any one rule. Agreeing on those says nothing about whether the
    judge leaves good answers alone, so they do not count towards TPR."""
    labels = [label(f"t{i}", "na") for i in range(10)] + [label("t10", "pass"), label("t11", "pass")]
    results = [result(f"t{i}", "na") for i in range(10)] + [result("t10", "fail"), result("t11", "pass")]

    [row] = compute_agreement(labels, results, {}, space_id="s", now=NOW, min_lower_bound=0.8)

    assert (row.n_applies, row.tp, row.fn, row.tpr) == (2, 1, 1, 0.5)  # not 11 of 12
    assert row.applicability_agree == 12


def test_kappa_is_zero_for_a_judge_that_passes_everything():
    """The playbook's lazy judge: 95 good answers, 5 bad, the judge passes all 100. Raw
    agreement is 95%, but it is no better than chance."""
    assert cohen_kappa(tp=0, fn=5, fp=0, tn=95) == pytest.approx(0.0)
    assert cohen_kappa(tp=5, fn=0, fp=0, tn=95) == pytest.approx(1.0)
    assert cohen_kappa(tp=0, fn=0, fp=0, tn=2) is None  # only "not fail" so far: undefined, not 0


def test_labels_are_not_compared_with_a_verdict_on_a_newer_rule_version():
    """The reviewer saw the old rule text, so their label says nothing about the new rule."""
    rows = compute_agreement(
        [label("t1", "fail", version=1)],
        [result("t1", "pass", version=2)],
        {},
        space_id="s",
        now=NOW,
        min_lower_bound=0.8,
    )
    assert rows == []


def test_each_judge_configuration_is_measured_separately_and_errors_are_excluded():
    labels = [label("t1", "fail"), label("t2", "pass")]
    results = [result("t1", "error"), result("t2", "pass", model_params={"samples": 1})]

    rows = compute_agreement(labels, results, {}, space_id="s", now=NOW, min_lower_bound=0.8)

    assert len(rows) == 2
    assert {(r.n, r.n_judge_error) for r in rows} == {(0, 1), (1, 0)}


# ------------------------------------------------------------------ label read-back


def assessment(name, value, source_type="HUMAN", trace="tr-t1", who="expert@example.com", ms=1):
    return Assessment(trace, name, source_type, who, value, "because", f"a{ms}", ms)


def test_only_the_latest_human_label_on_a_queued_trace_counts():
    items = {
        "tr-t1": ReviewItem(
            space_id="s",
            trace_id="t1",
            mlflow_trace_id="tr-t1",
            session_id="x",
            reason=QueueReason.RANDOM,
            queued_at=NOW,
        )
    }
    name = schema_name(CID, 1)
    found = [
        assessment(name, "pass", ms=1),
        assessment(name, "fail", ms=2),  # the reviewer changed their mind
        assessment(name, "fail", source_type="LLM_JUDGE", ms=3),
        assessment(name, "pass", trace="tr-unknown", ms=4),
        assessment(name, "maybe", who="other@example.com", ms=5),
    ]

    [got] = human_labels(found, items, {CID: CRITERION}, "s")

    assert (got.trace_id, got.label, got.reviewer) == ("t1", "fail", "expert@example.com")


def test_task_success_and_findings_become_one_review_per_turn_and_reviewer():
    items = {
        "tr-t1": ReviewItem(
            space_id="s",
            trace_id="t1",
            mlflow_trace_id="tr-t1",
            session_id="x",
            reason=QueueReason.RANDOM,
            queued_at=NOW,
        )
    }
    found = [
        assessment("task_success__v1", "pass", ms=1),  # answered under an older wording
        assessment(TASK_SUCCESS, "fail", ms=2),  # changed their mind: the latest answer counts
        assessment(FINDINGS, "Filtered 2023, the question asked for 2024.", ms=3),
        assessment(
            FINDINGS, "None.", who="other@example.com", ms=4
        ),  # "nothing to add", so the turn counts as done
        assessment(schema_name(CID, 1), "na", ms=5),  # a rule label, not a turn review
        assessment(TASK_SUCCESS, "pass", trace="tr-unknown", ms=6),
    ]

    reviews = {r.reviewer: r for r in trace_reviews(found, items, "s")}

    mine = reviews["expert@example.com"]
    assert (mine.trace_id, mine.task_success, mine.findings) == (
        "t1",
        "fail",
        "Filtered 2023, the question asked for 2024.",
    )
    assert mine.guideline == TASK_SUCCESS  # the wording the verdict was given under
    assert mine.task_rationale == "because"
    assert (reviews["other@example.com"].task_success, reviews["other@example.com"].findings) == (None, None)


def test_the_judge_is_revealed_only_after_a_human_label_and_only_once():
    labels = [label("t1", "fail")]
    results = [result("t1", "fail"), result("t2", "fail")]  # t2 has no human label yet

    assert [r.trace_id for _, r in judge_to_attach(labels, [], results)] == ["t1"]
    revealed = [assessment(schema_name(CID, 1), "fail", source_type="LLM_JUDGE")]
    assert judge_to_attach(labels, revealed, results) == []


def test_a_turn_is_shown_as_a_chat_with_its_sql_steps():
    t1 = Trace(
        trace_id="t1",
        space_id="s",
        conversation_id="c",
        turn_index=0,
        question="Top cities?",
        report_text="Paris.",
        access_status=AccessStatus.OK,
        parser_version="1",
        collected_at=NOW,
    )
    t2 = t1.model_copy(
        update={"trace_id": "t2", "turn_index": 1, "question": "and rates?", "report_text": "20%"}
    )
    steps = [
        Step(
            trace_id="t2",
            step_index=0,
            call_id="a",
            tool_name=ToolName.EXECUTE_SQL,
            step_kind=StepKind.ANSWER,
            title="Rates",
            sql="SELECT 1",
            result_schema=[{"name": "rate"}],
            result_rows=[[i] for i in range(50)],
            row_count=50,
        ),
        Step(
            trace_id="t2",
            step_index=1,
            call_id="b",
            tool_name=ToolName.GENERATE_VISUALIZATION,
            step_kind=StepKind.VIZ,
        ),
    ]

    t1_steps = [steps[0].model_copy(update={"trace_id": "t1", "title": "Cities", "call_id": "c1"})]

    view = turn_view(t2, steps, [(t1, t1_steps)])

    # One question and one answer: the Review App titles the item by the first user message
    # and would draw earlier turns as chat, the follow-up inside the previous answer's box.
    (message,) = view.messages
    assert message["role"] == "user" and message["content"].startswith("Turn 2: and rates?\n")
    assert "Turn 1: Top cities?" in message["content"] and "Paris." not in message["content"]
    context, review = view.steps  # one group per turn, the reviewed turn last
    assert context["name"] == "Turn 1 (context): Top cities?"
    assert context["children"][0]["name"] == "Cities"  # the earlier turn's SQL, to compare with
    assert review["name"] == "Turn 2 (under review): and rates?"
    assert len(review["children"][0]["outputs"]["rows"]) == 20
    assert review["children"][1]["name"] == "Chart: untitled"  # charts are summarised, not skipped
    assert view.tags["evalops.trace_id"] == "t2"
    assert view.title == "Turn 2: and rates?"  # not the first turn's question


# ------------------------------------------------------------------ the whole pass


class FakeBackend:
    """MLflow in memory: traces, assessments, one session."""

    def __init__(self):
        self.turns: dict[str, object] = {}
        self.session: list[str] = []
        self.assessments_: list[Assessment] = []
        self.reviewers: list[str] = []
        self.judge_logs: list[tuple[str, str, str]] = []

    def ensure_experiment(self, space_id):
        return f"exp-{space_id}"

    def set_reviewers(self, experiment_id, reviewers):
        self.reviewers = reviewers

    def ensure_label_schemas(self, experiment_id, criteria):
        return [TASK_SUCCESS, FINDINGS] + [schema_name(c.id, c.version) for c in criteria]

    def ensure_session(self, experiment_id, name, reviewers, schemas):
        return "sess-1", "https://review/sess-1"

    def find_traces(self, experiment_id):
        return {
            turn.trace_id: mid
            for mid, turn in self.turns.items()
            if turn.tags.get("evalops.view") == VIEW_VERSION
        }

    def log_turn(self, experiment_id, turn):
        mid = f"tr-{turn.trace_id}"
        self.turns[mid] = turn
        return mid

    def add_to_session(self, experiment_id, session_id, ids):
        self.session += ids

    def assessments(self, experiment_id):
        return list(self.assessments_)

    def ensure_schema(self, experiment_id, name, title, instruction):
        self.schemas = [*getattr(self, "schemas", []), name]

    def log_judge(self, mlflow_trace_id, name, value, rationale, source_id):
        self.judge_logs.append((mlflow_trace_id, name, value))
        self.assessments_.append(
            Assessment(mlflow_trace_id, name, "LLM_JUDGE", source_id, value, rationale, "j", 9)
        )


class FakeDelta:
    def __init__(self, traces, results):
        self.traces, self.results = traces, results
        self.items: dict[str, ReviewItem] = {}
        self.labels: dict[tuple, HumanLabel] = {}
        self.agreement = []
        self.reviews: dict[tuple, TraceReview] = {}

    async def load_current_results(self, space_id, criterion_ids):
        return self.results

    async def load_review_items(self, space_id):
        return list(self.items.values())

    async def merge_review_items(self, items):
        self.items |= {i.trace_id: i for i in items}

    async def merge_human_labels(self, labels):
        self.labels |= {(lb.trace_id, lb.criterion_id, lb.reviewer): lb for lb in labels}

    async def merge_trace_reviews(self, reviews):
        self.reviews |= {(r.trace_id, r.reviewer): r for r in reviews}

    async def load_human_labels(self, space_id):
        return list(self.labels.values())

    async def write_agreement(self, rows):
        self.agreement += rows

    async def load_traces(self, space_id):
        return [(t, []) for t in self.traces]


def make_trace(tid):
    return Trace(
        trace_id=tid,
        space_id="s",
        conversation_id=tid,
        turn_index=0,
        question=f"q {tid}",
        report_text="report",
        access_status=AccessStatus.OK,
        parser_version="1",
        collected_at=NOW,
    )


async def run_pass(delta, backend, store):
    async def load_context(space_id):
        return f"Genie space: {space_id}"

    return await review_space(
        "s",
        delta=delta,
        store=store,
        backend=backend,
        criteria=[CRITERION],
        budget=10,
        min_lower_bound=0.8,
        now=NOW,
        seed=0,
        load_context=load_context,
    )


async def test_first_pass_queues_blind_then_a_label_reveals_only_its_judge_verdict():
    delta = FakeDelta([make_trace("t1"), make_trace("t2")], [result("t1", "fail"), result("t2", "pass")])
    backend, store = FakeBackend(), InMemoryStore()
    await store.sync_reviewer("expert@example.com", ["s"])

    first = await run_pass(delta, backend, store)

    assert first.queued == 2 and sorted(backend.session) == ["tr-t1", "tr-t2"]
    assert backend.reviewers == ["expert@example.com"]
    assert backend.judge_logs == []  # blind: nothing about the judge is in MLflow yet
    assert backend.turns["tr-t1"].messages[0]["content"].endswith("Genie space: s")
    assert await store.get_review_sessions(["s"]) == {"s": "https://review/sess-1"}

    backend.assessments_.append(assessment(schema_name(CID, 1), "fail", trace="tr-t1"))
    backend.assessments_.append(assessment(TASK_SUCCESS, "fail", trace="tr-t1"))
    second = await run_pass(delta, backend, store)

    assert backend.judge_logs == [("tr-t1", schema_name(CID, 1), "fail")]  # t1 only: t2 is unlabelled
    assert second.queued == 0  # nothing is queued twice
    assert (second.agreement[0].tn, second.agreement[0].n) == (1, 1)  # a failure caught
    assert [r.task_success for r in delta.reviews.values()] == ["fail"]
    assert backend.judge_logs[0][1] != TASK_SUCCESS  # no judge for task success: nothing to reveal


async def test_without_registered_reviewers_nothing_is_queued():
    delta = FakeDelta([make_trace("t1")], [result("t1", "fail")])
    backend = FakeBackend()

    stats = await run_pass(delta, backend, InMemoryStore())

    assert stats.queued == 0 and backend.session == []


def test_citations_name_the_cited_step_instead_of_linking_to_the_askers_conversation():
    """Regression: the reviewer clicked a citation and got "does not exist" (someone else's chat),
    and a heading glued to the citation rendered as "]### Key Trends"."""
    link = "https://host/genie/rooms/s/chats/c?o=1&gra_focus=toolu_1"
    report = f"Up 159%. \\[[1]({link})\\]### Key Trends\n- Jan [2](https://host/x?gra_focus=gone)"
    steps = [
        Step(
            trace_id="t",
            step_index=0,
            call_id="toolu_1",
            tool_name=ToolName.EXECUTE_SQL,
            step_kind=StepKind.ANSWER,
            title="Monthly bookings",
        )
    ]

    out = readable_citations(report, steps)

    assert out == "Up 159%. [1: Monthly bookings]\n\n### Key Trends\n- Jan [2]"
    assert "http" not in out


# ------------------------------------------------------------------ what the reviewer reads first


def test_the_space_definition_comes_from_the_serialized_space():
    from evalops.schemas.genie import SpaceDefinition

    body = {
        "title": "Bookings",
        "description": "One metric view.",
        "serialized_space": json.dumps(
            {
                "data_sources": {"tables": [{"identifier": "c.s.booking_metrics"}]},
                "instructions": {
                    "text_instructions": [{"content": ["R1 Revenue = realized.\n", "R4 Privacy.\n"]}]
                },
            }
        ),
    }

    space = SpaceDefinition.from_api(body)

    assert space.rules == ["R1 Revenue = realized.", "R4 Privacy."]
    assert space.tables == ["c.s.booking_metrics"]


def test_the_context_states_the_data_boundary_measures_and_rules():
    """Regression: "What do guests complain about most?" could not be judged without knowing that
    reviews were deliberately left out of the space."""
    from evalops.schemas.genie import SpaceDefinition
    from evalops.services.review.context import space_context, table_info

    describe = json.dumps(
        {
            "comment": "Data ends 2025-07-30. PRIVACY BOUNDARY: reviews are deliberately not joined.",
            "columns": [
                {"name": "Country", "is_measure": None},
                {"name": "Cancellation Rate", "is_measure": True, "comment": "A ratio."},
            ],
        }
    )
    space = SpaceDefinition(title="Bookings", rules=["R4 Privacy."], tables=["c.s.mv"])

    text = space_context(space, [table_info("c.s.mv", describe)])

    assert "PRIVACY BOUNDARY: reviews are deliberately not joined" in text
    assert "Measures: Cancellation Rate (A ratio.)" in text and "Dimensions: Country" in text
    assert text.endswith("Space rules:\nR4 Privacy.")


def test_asked_time_is_shown_in_the_reviewers_zone_and_in_utc():
    from evalops.services.review.context import asked_line

    at = datetime(2026, 10, 19, 23, 10, tzinfo=UTC)
    assert asked_line(at, "America/Phoenix") == "Asked 2026-10-19 16:10 MST (2026-10-19 23:10 UTC)"
    assert asked_line(at, "UTC") == "Asked 2026-10-19 23:10 UTC"
    assert asked_line(None, "UTC") == "Asked: unknown"


def test_the_chat_opens_with_the_context_and_charts_are_summarised():
    """Regression: the reviewer could not see when a turn was asked, and "see the chart" pointed
    at nothing because chart steps were skipped."""
    trace = make_trace("t1")
    steps = [
        Step(
            trace_id="t1",
            step_index=0,
            call_id="q",
            tool_name=ToolName.EXECUTE_SQL,
            step_kind=StepKind.ANSWER,
            title="Monthly revenue",
            sql="SELECT 1",
        ),
        Step(
            trace_id="t1",
            step_index=1,
            call_id="v",
            tool_name=ToolName.GENERATE_VISUALIZATION,
            step_kind=StepKind.VIZ,
            title="Revenue by month",
            source_call_id="q",
        ),
    ]

    view = turn_view(trace, steps, [], context="Genie space: Bookings", asked="Asked 2026-10-19 16:10 MST")

    assert view.messages[0] == {
        "role": "system",
        "content": "Asked 2026-10-19 16:10 MST\n\nGenie space: Bookings",
    }
    assert view.messages[-1]["role"] == "user"
    assert view.title == "q t1"  # a single turn keeps its own question as the title
    assert view.steps[1] == {
        "name": "Chart: Revenue by month",
        "inputs": {"kind": "viz"},
        "outputs": {"chart": "Revenue by month", "plots the rows of": "Monthly revenue"},
    }


def test_a_metric_view_described_through_the_tables_api_keeps_its_boundary_and_measures():
    """Regression: with BROWSE only, DESCRIBE failed and the reviewer lost the privacy boundary.
    The tables API drops column comments, so measures come from the view's YAML definition."""
    from evalops.services.review.context import table_info_from_uc

    body = {
        "comment": "PRIVACY BOUNDARY: reviews are deliberately not joined.",
        "view_definition": "version: 1.1\ndimensions:\n  - name: Country\n    expr: c\nmeasures:\n"
        "  - name: Cancellation Rate\n    expr: x\n    comment: A ratio.\n",
        "columns": [{"name": "Country"}, {"name": "Cancellation Rate"}],
    }

    info = table_info_from_uc("c.s.mv", body)

    assert info.comment.startswith("PRIVACY BOUNDARY")
    assert info.measures == [("Cancellation Rate", "A ratio.")] and info.dimensions == ["Country"]


def test_without_the_view_definition_columns_are_not_called_dimensions():
    """Regression: with BROWSE only, the measures were listed under "Dimensions"."""
    from evalops.schemas.genie import SpaceDefinition
    from evalops.services.review.context import space_context, table_info_from_uc

    info = table_info_from_uc(
        "c.s.mv", {"comment": "c", "columns": [{"name": "Country"}, {"name": "Bookings"}]}
    )
    text = space_context(SpaceDefinition(title="t"), [info])

    assert "Columns: Country, Bookings" in text and "Dimensions" not in text


def test_every_review_instruction_fits_the_review_apps_limit():
    """Regression: the Review App rejects a label schema whose instruction is over 1000
    characters, and the review job failed on the reworded task-success question."""
    from pathlib import Path

    from evalops.services.checks.llm_checks import rubric_criteria
    from evalops.services.criteria import load_criteria
    from evalops.services.review.backend import (
        FINDINGS_INSTRUCTION,
        TASK_SUCCESS_INSTRUCTION,
        label_instruction,
    )

    root = Path(__file__).parents[1]
    criteria, _ = load_criteria(root / "criteria", root / "demo" / "criteria")
    texts = [TASK_SUCCESS_INSTRUCTION, FINDINGS_INSTRUCTION] + [
        label_instruction(c) for c in rubric_criteria(criteria)
    ]
    assert len(texts) > 2 and all(len(t) <= 1000 for t in texts)
