"""Tests for evalops.services.judge_data: human labels as a time-split judge dataset."""

from datetime import UTC, datetime, timedelta

from evalops.schemas.checks import Criterion
from evalops.schemas.llm import RubricJudgeInput
from evalops.schemas.review import HumanLabel
from evalops.schemas.trace import AccessStatus, Step, StepKind, ToolName, Trace
from evalops.services.judge_data import examples_from_labels, time_split
from evalops.services.review.agreement import score

T0 = datetime(2026, 10, 21, tzinfo=UTC)
CRITERION = Criterion.model_validate(
    {
        "id": "rubric.ambiguous_metric",
        "version": 2,
        "family": "rubric",
        "layer": "behaviour",
        "gate": "monitor",
        "applies_to": {"level": "report"},
        "description": "x",
        "params": {"skill": "rubric_judge", "rule": "Ask or state the measure.", "failure": "Neither."},
    }
)


def trace(tid: str, turn: int = 0, report: str = "Paris leads.") -> Trace:
    return Trace(
        trace_id=tid,
        space_id="s",
        conversation_id="c" if turn else f"c-{tid}",
        turn_index=turn,
        access_status=AccessStatus.OK,
        parser_version="2",
        collected_at=T0,
        question=f"q {tid}",
        report_text=report,
    )


def label(tid: str, value: str, minutes: int, version: int = 2, reviewer: str = "r") -> HumanLabel:
    return HumanLabel(
        space_id="s",
        trace_id=tid,
        mlflow_trace_id=f"m-{tid}",
        criterion_id=CRITERION.id,
        criterion_version=version,
        reviewer=reviewer,
        label=value,
        rationale=f"why {value}",
        assessment_id=f"a-{tid}-{minutes}",
        labeled_at=T0 + timedelta(minutes=minutes),
    )


def test_each_labelled_turn_becomes_the_judges_exact_input_with_the_latest_label():
    step = Step(
        trace_id="t2",
        step_index=0,
        call_id="x",
        tool_name=ToolName.EXECUTE_SQL,
        step_kind=StepKind.ANSWER,
        title="Top destinations",
        sql="SELECT 1",
    )
    loaded = [(trace("t1", turn=0), []), (trace("t2", turn=1), [step])]
    loaded[0][0].conversation_id = "c"
    labels = [label("t2", "pass", 1), label("t2", "fail", 5, reviewer="other")]  # later label wins

    [example] = examples_from_labels(labels, loaded, CRITERION)

    assert (example["name"], example["expected"], example["rationale"]) == ("t2", "fail", "why fail")
    data = RubricJudgeInput(**example["input"])
    assert data.rule == "Ask or state the measure." and data.steps == "Top destinations\nSELECT 1"
    assert "Turn 1 question: q t1" in data.context  # a follow-up carries what it inherits


def test_labels_under_another_rule_text_and_turns_without_a_report_are_left_out():
    loaded = [(trace("t1"), []), (trace("t2", report=" "), []), (trace("t3"), [])]
    labels = [
        label("t1", "fail", 1, version=1),
        label("t2", "fail", 2),
        label("t3", "na", 3),
        label("t9", "fail", 4),
    ]

    assert [e["name"] for e in examples_from_labels(labels, loaded, CRITERION)] == ["t3"]


def test_the_newest_labels_are_held_out():
    examples = [{"name": f"t{i}", "labeled_at": f"2026-10-2{i}"} for i in (3, 1, 4, 2, 5)]

    dev, holdout = time_split(examples, 0.3)

    assert [e["name"] for e in dev] == ["t1", "t2", "t3"]
    assert [e["name"] for e in holdout] == ["t4", "t5"]  # ceil(5 x 0.3) = 2, the newest
    assert time_split(examples[:1]) == (examples[:1], [])  # one example: nothing to hold out


def test_offline_scores_count_like_the_calibration_card():
    m = score(
        [("fail", "fail"), ("fail", "na"), ("pass", "fail"), ("pass", "pass"), ("na", "na"), ("na", "pass")]
    )

    assert (m["tp"], m["fn"], m["fp"], m["tn"], m["n_applies"]) == (1, 1, 1, 1, 4)
    assert m["applicability_agree"] == 4  # fail/na and na/pass disagree on whether it applies
