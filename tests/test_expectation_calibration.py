"""Calibrating expectation_judge (step 3b): people check a sample of
regression answers against their case's expectations, and the judge is measured on them like
the rubric judges. Until it is calibrated, it reports but does not decide a gate."""

import json
from datetime import UTC, datetime

from test_review import FakeBackend, assessment

from evalops.clients.lakebase import InMemoryStore
from evalops.schemas.evalset import EvalCase, Expect
from evalops.schemas.manifest import ArtifactRef, Manifest
from evalops.schemas.review import HumanLabel, QueueReason, ReviewItem
from evalops.services.llm.skills import load_skill, skills_dir
from evalops.services.review import expectations as ex

NOW = datetime(2026, 10, 27, tzinfo=UTC)
JUDGE = load_skill("expectation_judge", skills_dir(""))


def judged(*passed, kinds=None):
    kinds = kinds or ["fact"] * len(passed)
    return json.dumps(
        [
            {"kind": k, "name": f"e{n}", "passed": p, "detail": ""}
            for n, (k, p) in enumerate(zip(kinds, passed, strict=True))
        ]
        + [{"kind": "data", "name": "queries data", "passed": False, "detail": ""}]  # not the judge's
    )


def row(trace_id, expectations, run_id="r1", case_id="c1"):
    return {
        "run_id": run_id,
        "case_id": case_id,
        "case_version": 1,
        "trace_id": trace_id,
        "conversation_id": "conv",
        "question": "Only 2024",
        "report_text": "Revenue in 2024 was $4.5M.",
        "expectations": expectations,
        "steps": "[]",
    }


def manifest(run_id, version):
    return Manifest(
        run_id=run_id,
        run_kind="eval",
        space_id="s",
        created_at=NOW,
        artifacts=[ArtifactRef(kind="skill", name="expectation_judge", version=version, hash="h")],
    )


def test_the_judge_verdict_is_about_its_own_expectations_only():
    assert (
        ex.judge_verdict(json.loads(judged(True, True))) == "pass"
    )  # the failed data check is not the judge's
    assert ex.judge_verdict(json.loads(judged(True, False))) == "fail"
    assert ex.judge_verdict(json.loads(judged(True, None))) is None  # it could not decide one
    assert ex.judge_verdict([{"kind": "data", "name": "x", "passed": True}]) is None  # nothing judged


def human(trace_id, value, reviewer="expert@example.com"):
    return HumanLabel(
        space_id="s",
        trace_id=trace_id,
        mlflow_trace_id=f"m-{trace_id}",
        criterion_id="expectations",
        criterion_version=1,
        reviewer=reviewer,
        label=value,
        rationale="",
        assessment_id="a",
        labeled_at=NOW,
    )


def test_agreement_counts_only_answers_graded_by_the_current_judge_version():
    attempts = {
        "a": row("a", judged(False)),  # judge fail, person fail: caught
        "b": row("b", judged(True)),  # judge pass, person fail: missed
        "c": row("c", judged(True)),  # judge pass, person pass
        "old": row("old", judged(False), run_id="r0"),  # graded by v0: not this judge
    }
    versions = ex.judge_versions([manifest("r1", "1"), manifest("r0", "0")])
    labels = [human("a", "fail"), human("b", "fail"), human("c", "pass"), human("old", "pass")]

    found = ex.agreement(
        labels,
        attempts,
        versions,
        current="1",
        judge="expectation_judge@1|m",
        space_id="s",
        now=NOW,
        min_lower_bound=0.8,
    )

    # a: failure caught (TN); b: a bad answer passed (FP); c: a good answer passed (TP).
    assert (found.tp, found.fn, found.fp, found.tn) == (1, 0, 1, 1)
    assert found.criterion_id == "expectations" and found.criterion_version == 1 and not found.calibrated


def test_synthetic_labels_are_named_on_the_judge_so_they_cannot_pass_for_real_ones():
    attempts = {"a": row("a", judged(False))}
    found = ex.agreement(
        [human("a", "fail", reviewer=ex.SYNTHETIC)],
        attempts,
        {"r1": "1"},
        current="1",
        judge="expectation_judge@1|m",
        space_id="s",
        now=NOW,
        min_lower_bound=0.8,
    )
    assert found.judge.endswith("|1 synthetic labels")


def test_the_sample_is_half_judge_fails_and_skips_queued_and_other_versions():
    rows = [row(f"f{n}", judged(False)) for n in range(5)] + [row(f"p{n}", judged(True)) for n in range(5)]
    rows += [row("queued", judged(False)), row("v0", judged(False), run_id="r0")]
    chosen = ex.select(rows, {"r1": "1", "r0": "0"}, current="1", queued={"queued"}, budget=4, seed=1)
    ids = [r["trace_id"] for r in chosen]
    assert sum(i.startswith("f") for i in ids) == 2 and sum(i.startswith("p") for i in ids) == 2
    few_fails = ex.select(rows[5:] + rows[:1], {"r1": "1"}, current="1", queued=set(), budget=4, seed=1)
    assert len(few_fails) == 4  # the pass class fills what the fail class cannot


def test_the_reviewer_sees_the_expectations_before_the_answer_and_the_earlier_turns():
    case = EvalCase(
        case_id="c1",
        space_id="s",
        version=1,
        title="Revenue then 2024",
        turns=["What was revenue?", "Only 2024"],
        expect=Expect(behaviour="answer", facts=["covers 2024 only"]),
        updated_by="o",
        updated_at=NOW,
    )
    turn = ex.view(row("a", judged(True)), case, "s")
    assert (
        "1. e0" in turn.messages[0]["content"]
        and "Expectations for this answer" in turn.messages[0]["content"]
    )
    assert turn.messages[-1]["content"].startswith("Turn 2: Only 2024")
    assert turn.tags["evalops.kind"] == "expectations" and turn.title.startswith("[eval case]")


class Delta:
    def __init__(self, attempts, manifests):
        self.attempts, self.manifests = attempts, manifests
        self.items, self.labels, self.agreements = [], [], []

    async def load_review_items(self, space_id):
        return list(self.items)

    async def merge_human_labels(self, labels):
        self.labels += labels

    async def load_human_labels(self, space_id):
        return list(self.labels)

    async def load_judged_attempts(self, space_id):
        return self.attempts

    async def load_manifests(self, space_id, run_kind):
        return [{"manifest": m.model_dump_json()} for m in self.manifests]

    async def write_agreement(self, rows):
        self.agreements += rows

    async def merge_review_items(self, items):
        self.items += items


async def test_a_review_run_queues_answers_then_measures_the_judge_on_their_labels():
    store, backend = InMemoryStore(), FakeBackend()
    delta = Delta([row("a", judged(False)), row("b", judged(True))], [manifest("r1", str(JUDGE.version))])
    args = dict(
        delta=delta,
        store=store,
        backend=backend,
        experiment_id="e",
        reviewers=["expert@example.com"],
        judge=JUDGE,
        model="m",
        budget=10,
        min_lower_bound=0.8,
        now=NOW,
        seed=1,
    )

    measured, queued = await ex.review_expectations("s", assessments=[], **args)
    assert (measured, queued) == (None, 2) and {i.reason for i in delta.items} == {
        QueueReason.EXPECTATION_CHECK
    }
    assert ex.SCHEMA in backend.schemas and len(backend.session) == 2

    labelled = [assessment(ex.SCHEMA, "fail", trace=i.mlflow_trace_id) for i in delta.items]
    measured, queued = await ex.review_expectations("s", assessments=labelled, **args)
    assert queued == 0  # nothing new to queue
    assert (measured.tn, measured.fp) == (1, 1)  # caught one failure, passed one answer people failed
    assert delta.agreements[-1] is measured


async def test_a_production_label_on_another_trace_is_not_an_expectation_label():
    items = {
        "m-a": ReviewItem(
            space_id="s",
            trace_id="a",
            mlflow_trace_id="m-a",
            session_id="x",
            reason=QueueReason.EXPECTATION_CHECK,
            queued_at=NOW,
        )
    }
    found = ex.labels(
        [assessment("task_success__v3", "fail", trace="m-a"), assessment(ex.SCHEMA, "pass", trace="m-a")],
        items,
        "s",
    )
    assert [(f.trace_id, f.label) for f in found] == [("a", "pass")]
