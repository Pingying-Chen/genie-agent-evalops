"""Regression runs: the tester's half with fake Genie and SQL, grading
with the real criteria and a fake judge, and reading the run folder back."""

from datetime import UTC, datetime

from test_checks import sql_step, trace

from evalops.errors import UpstreamError
from evalops.schemas.evalset import EvalCase, Expect
from evalops.schemas.evaluation import ArmConfig, AskCase, AskRequest, Attempt
from evalops.schemas.llm import ExpectationJudgeOutput, ExpectationVerdict
from evalops.services.checks.llm_checks import rubric_criteria
from evalops.services.criteria import criteria_dir, load_criteria
from evalops.services.llm.runner import SkillResult
from evalops.services.llm.skills import load_skill, skills_dir
from evalops.services.regression import asker, grading
from evalops.services.regression.runner import read_back

NOW = datetime(2026, 10, 22, tzinfo=UTC)
CRITERIA, _ = load_criteria(criteria_dir())  # the generic set: no rubric judges
JUDGE = load_skill("expectation_judge", skills_dir(""))
REPORT = "Realized revenue in 2024 was **$4,479,221**, from confirmed and completed bookings."


def case(**expect) -> EvalCase:
    return EvalCase(
        case_id="c1",
        space_id="s",
        version=2,
        title="Revenue 2024",
        turns=["What was realized revenue in 2024?"],
        expect=Expect(**expect),
        updated_by="u",
        updated_at=NOW,
    )


def answered(
    report: str = REPORT, rows=((4479221.300411224,),), sql: str = "SELECT MEASURE(`Realized Revenue`) FROM v"
) -> Attempt:
    return Attempt(
        arm="baseline",
        case_id="c1",
        case_version=2,
        attempt=0,
        conversation_id="conv",
        traces=[trace(report, question="What was realized revenue in 2024?")],
        steps=[sql_step(sql, rows=[list(r) for r in rows])],
    )


class FakeJudge:
    """Meets every expectation unless its text contains 'never'."""

    model = "fake"

    def __init__(self):
        self.calls = []

    async def run(self, skill, data, temperature=None):
        self.calls.append(data)
        lines = data.expectations.splitlines()
        verdicts = [
            ExpectationVerdict(number=n, quote="", reasoning="checked", met="never" not in line)
            for n, line in enumerate(lines, start=1)
        ]
        return SkillResult(ExpectationJudgeOutput(verdicts=verdicts), None, skill.ref, "fake", "", 0.1, 50)


async def grade(attempt: Attempt, c: EvalCase, judge=None):
    return await grading.grade_attempt(
        attempt,
        c,
        criteria=CRITERIA,
        runner=judge or FakeJudge(),
        rubric_skills={},
        judge_skill=JUDGE,
        run_id="r",
        space_id="s",
        now=NOW,
    )


async def test_an_answer_that_meets_its_case_passes():
    judge = FakeJudge()
    graded = await grade(
        answered(),
        case(
            facts=["names the year"],
            reference_sql="SELECT 4479221.3",
            tools={"data": "required", "must_not_query": ["guest_email"]},
            criteria={"report.figure_sourced": "pass"},
        ),
        judge,
    )
    # the reference rows come from the tester; without them the reference is undecided
    assert graded.verdict == "error"
    attempt = answered()
    attempt.reference_columns, attempt.reference_rows = ["v"], [[4479221.3]]
    graded = await grade(attempt, case(facts=["names the year"], reference_sql="SELECT 4479221.3"), judge)
    assert graded.verdict == "pass", graded.expectations
    # An answer case is also checked for answering rather than declining (over-refusal).
    assert {e.kind for e in graded.expectations} == {"reference", "fact", "behaviour"}
    assert "1. Answers the question" in judge.calls[-1].expectations
    assert "2. names the year" in judge.calls[-1].expectations


async def test_each_kind_of_expectation_can_fail_the_case():
    queried = await grade(
        answered(sql="SELECT guest_email FROM v"), case(tools={"must_not_query": ["guest_email"]})
    )
    no_data = await grade(answered(), case(tools={"data": "none"}))
    fact = await grade(answered(), case(facts=["never mentions revenue"]))
    rule = await grade(answered(), case(criteria={"report.figure_sourced": "na"}))
    missing = answered()
    missing.reference_columns, missing.reference_rows = ["v"], [[1234567.0]]
    reference = await grade(missing, case(reference_sql="SELECT 1234567"))

    assert [g.verdict for g in (queried, no_data, fact, rule, reference)] == ["fail"] * 5
    assert reference.expectations[0].detail.startswith("0 of 1 values")


async def test_a_hard_rule_fails_the_case_even_with_no_expectations():
    graded = await grade(answered(report="Revenue was $9,999,999 in 2024."), case())
    assert graded.verdict == "fail" and graded.hard_fails == ["report.figure_sourced"]


async def test_a_refusal_case_is_judged_on_declining_and_genie_errors_are_not_a_verdict():
    judge = FakeJudge()
    graded = await grade(
        answered(report="That information is not available.", rows=()), case(behaviour="refuse"), judge
    )
    assert graded.expectations[0].kind == "behaviour"
    assert "Declines" in judge.calls[-1].expectations

    broken = Attempt(
        arm="baseline", case_id="c1", case_version=2, attempt=1, error="genie: agent answer failed"
    )
    assert (await grade(broken, case())).verdict == "error"


# ------------------------------------------------------------------ the tester's half


class FakeGenie:
    def __init__(self, manages=False, fail_on=None):
        self.manages, self.fail_on = manages, fail_on
        self.created, self.deleted, self.asked = [], [], []

    async def can_manage(self, space_id):
        return self.manages

    async def create_space(self, **body):
        self.created.append(body)
        return f"scratch-{len(self.created)}"

    async def delete_space(self, space_id):
        self.deleted.append(space_id)

    async def ask_agent(self, space_id, text, conversation_id=None):
        self.asked.append((space_id, text, conversation_id))
        if self.fail_on and self.fail_on in text:
            raise UpstreamError("genie", "agent answer failed")
        return conversation_id or f"conv-{len(self.asked)}"

    async def get_items(self, space_id, conversation_id):
        return []


class FakeSql:
    def __init__(self, readable=()):
        self.readable, self.statements = readable, []

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        if statement.startswith("SELECT 1 FROM"):
            if statement.split()[3] not in self.readable:
                raise UpstreamError("sql", "PERMISSION_DENIED")
            return [{"1": 1}]
        return [{"revenue": 4479221.3}]


class DictFolder(dict):
    async def read(self, name):
        return self[name]

    async def write(self, name, text):
        self[name] = text

    async def names(self, prefix):
        if not prefix:
            return sorted(n for n in self if "/" not in n)
        return sorted(n for n in self if n.startswith(prefix + "/"))


def request(**kw) -> AskRequest:
    arms = [
        ArmConfig(arm="baseline", config_version="a", title="T", warehouse_id="wh", serialized_space="{}"),
        ArmConfig(
            arm="candidate", config_version="b", title="T", warehouse_id="wh", serialized_space='{"x": 1}'
        ),
    ]
    cases = [
        AskCase(case_id="c1", version=1, turns=["Top destinations?", "Only 2024"]),
        AskCase(case_id="c2", version=3, turns=["Revenue 2024?"], reference_sql="SELECT 1"),
    ]
    return AskRequest(
        **{"run_id": "run12345", "space_id": "prod", "arms": arms, "cases": cases, "k": 2, **kw}
    )


async def test_the_tester_asks_every_case_on_every_arm_in_one_conversation_and_cleans_up():
    genie, sql, folder = FakeGenie(), FakeSql(), DictFolder()

    done = await asker.run(
        request(), "tester", "/Workspace/Users/tester", genie=genie, sql=sql, folder=folder
    )

    assert done.attempts == 8 and done.deleted and done.error is None  # 2 cases x 2 arms x k=2
    assert [c["serialized_space"] for c in genie.created] == ["{}", '{"x": 1}']
    assert sorted(genie.deleted) == ["scratch-1", "scratch-2"]
    follow_ups = [a for a in genie.asked if a[1] == "Only 2024"]
    assert all(conversation for _, _, conversation in follow_ups)  # asked in the first turn's conversation
    c2 = Attempt.model_validate_json(folder["attempts/candidate__c2__1.json"])
    assert c2.reference_rows == [[4479221.3]] and c2.case_version == 3
    preflight, done_back, attempts = await read_back(folder)
    assert preflight.ok and done_back.attempts == 8 and len(attempts) == 8


async def test_a_tester_with_more_access_than_a_consumer_asks_nothing():
    genie = FakeGenie(manages=True)
    folder = DictFolder()
    done = await asker.run(
        request(forbidden_table="main.hr.salaries"),
        "tester",
        "/p",
        genie=genie,
        sql=FakeSql(readable=("main.hr.salaries",)),
        folder=folder,
    )

    assert done.attempts == 0 and genie.created == [] and genie.asked == []
    assert "manages the production space" in done.error and "can read main.hr.salaries" in done.error


async def test_scratch_spaces_are_deleted_even_when_asking_breaks():
    genie = FakeGenie()

    async def boom(*a, **k):
        raise RuntimeError("folder unavailable")

    folder = DictFolder()
    real_write = folder.write

    async def write(name, text):
        if name.startswith("attempts/"):
            await boom()
        await real_write(name, text)

    folder.write = write
    done = await asker.run(request(), "tester", "/p", genie=genie, sql=FakeSql(), folder=folder)

    assert sorted(genie.deleted) == ["scratch-1", "scratch-2"]
    assert "folder unavailable" in done.error and "done.json" in folder


async def test_a_failed_ask_is_recorded_on_its_attempt_not_raised():
    genie = FakeGenie(fail_on="Only 2024")
    folder = DictFolder()
    done = await asker.run(request(), "tester", "/p", genie=genie, sql=FakeSql(), folder=folder)

    c1 = Attempt.model_validate_json(folder["attempts/baseline__c1__0.json"])
    assert done.error is None and c1.error == "genie: agent answer failed"


def test_generic_criteria_have_no_rubric_judge_so_grading_needs_no_llm_for_them():
    assert rubric_criteria(CRITERIA) == []  # the tests above rely on it


# ------------------------------------------------------------------ results


def graded(case_id: str, arm: str, verdicts: str) -> list:
    from evalops.schemas.evaluation import GradedAttempt

    return [
        GradedAttempt(
            run_id="r",
            space_id="s",
            arm=arm,
            case_id=case_id,
            case_version=1,
            attempt=n,
            verdict=v,
            graded_at=NOW,
        )
        for n, v in enumerate({"P": "pass", "F": "fail", "E": "error"}[c] for c in verdicts)
    ]


def test_a_case_passes_k_only_if_every_attempt_passed_and_a_split_is_flaky():
    from evalops.services.regression.results import arm_summaries, case_runs

    attempts = (
        graded("a", "baseline", "PPP") + graded("b", "baseline", "PFP") + graded("c", "baseline", "PPE")
    )
    cases = case_runs(attempts, {"a": "Alpha", "b": "Beta", "c": "Gamma"})

    outcomes = {c.title: c.arms["baseline"] for c in cases}
    assert (outcomes["Alpha"].pass_all, outcomes["Beta"].pass_all, outcomes["Gamma"].pass_all) == (
        True,
        False,
        False,
    )
    assert outcomes["Beta"].flaky and not outcomes["Gamma"].flaky  # an error is not a fail
    [summary] = arm_summaries(cases, {"baseline": "v1"})
    assert (summary.cases, summary.pass_all, summary.pass_any, summary.flaky, summary.errored) == (
        3,
        1,
        3,
        1,
        1,
    )


def test_only_k_out_of_k_counts_as_fixed_or_broken_and_targets_decide_acceptance():
    from evalops.services.regression.results import case_runs, compare

    attempts = (
        graded("fixed", "baseline", "FFF")
        + graded("fixed", "candidate", "PPP")
        + graded("broken", "baseline", "PPP")
        + graded("broken", "candidate", "FFF")
        + graded("noisy", "baseline", "PPP")
        + graded("noisy", "candidate", "PFP")
        + graded("stuck", "baseline", "FFF")
        + graded("stuck", "candidate", "FFF")
    )
    cases = case_runs(attempts, {})

    rejected = compare(cases, targets={"fixed"})
    assert {c.case_id: c.outcome for c in rejected.cases} == {
        "fixed": "fixed",
        "broken": "broken",
        "noisy": "flaky",
        "stuck": "still_fails",
    }
    assert rejected.verdict == "reject" and rejected.reasons == ["broke “broken”"]

    without_broken = case_runs([a for a in attempts if a.case_id != "broken"], {})
    assert compare(without_broken, targets={"fixed"}).verdict == "accept"
    assert compare(without_broken, targets={"stuck"}).verdict == "reject"  # the target is not fixed
    assert compare(without_broken, targets={"noisy"}).verdict == "inconclusive"
    # The target passed on the baseline too: the change fixed nothing (seen on a live gate run).
    already = case_runs(graded("a", "baseline", "PPP") + graded("a", "candidate", "PPP"), {})
    assert compare(already, targets={"a"}).verdict == "inconclusive"
    assert compare(already, targets={"a"}).reasons == ["“a” already passes without the change"]
    assert compare(case_runs(graded("a", "baseline", "PPP"), {}), set()) is None  # no candidate arm


async def test_a_run_read_back_names_the_change_it_tested_and_compares_its_arms():
    import json

    from evalops.clients.lakebase import InMemoryStore
    from evalops.services.regression import results

    row = {
        "run_id": "r",
        "space_id": "s",
        "label": "Keep filters",
        "change_id": "ch1",
        "status": "succeeded",
        "k": 3,
        "started_at": NOW,
        "arms": json.dumps(
            [{"arm": "baseline", "config_version": "a"}, {"arm": "candidate", "config_version": "b"}]
        ),
    }
    answers = graded("c1", "baseline", "FFF") + graded("c1", "candidate", "PPP")

    class Sql:
        async def execute(self, statement, parameters=None):
            if "question_intents" in statement or "run_manifests" in statement:
                return []
            if "FROM p.eval_runs" in statement and "eval_results" not in statement:
                return [row]
            return [{**a.model_dump(), "hard_fails": "[]", "expectations": "[]"} for a in answers]

    store = InMemoryStore()
    await store.put_change("s", change_record(target_case_ids=["c1"]))

    detail = await results.run_detail("s", "r", sql=Sql(), store=store, prefix="p")
    listed = await results.runs("s", sql=Sql(), store=store, prefix="p")

    assert detail.change_id == "ch1" and listed.runs[0].change_id == "ch1"
    assert detail.comparison.verdict == "accept" and detail.comparison.cases[0].target


async def test_a_tracking_only_judge_runs_only_where_the_case_names_its_rule():
    from evalops.schemas.checks import AppliesTo, Criterion, Family, Gate, Layer
    from evalops.schemas.llm import RubricJudgeInput, RubricJudgeOutput

    rubric = Criterion(
        id="rubric.x",
        version=1,
        family=Family.RUBRIC,
        layer=Layer.BEHAVIOUR,
        gate=Gate.MONITOR,
        applies_to=AppliesTo(level="report"),
        description="d",
        params={"skill": "rubric_judge", "rule": "r", "failure": "f"},
    )

    class Judges(FakeJudge):
        async def run(self, skill, data, temperature=None):
            if isinstance(data, RubricJudgeInput):
                self.calls.append(data)
                out = RubricJudgeOutput(
                    applies_reason="a", applies=True, quote="", reasoning="r", verdict="pass"
                )
                return SkillResult(out, None, skill.ref, "fake", "", 0.1, 10)
            return await super().run(skill, data, temperature)

    rubric_skill = load_skill("rubric_judge", skills_dir(""))

    async def grade_with(c, judge):
        return await grading.grade_attempt(
            answered(),
            c,
            criteria=[*CRITERIA, rubric],
            runner=judge,
            rubric_skills={"rubric_judge": rubric_skill},
            judge_skill=JUDGE,
            run_id="r",
            space_id="s",
            now=NOW,
        )

    skipped = Judges()
    rubric_calls = lambda judge: [c for c in judge.calls if isinstance(c, RubricJudgeInput)]  # noqa: E731
    assert (await grade_with(case(), skipped)).verdict == "pass" and rubric_calls(skipped) == []
    named = Judges()
    graded_named = await grade_with(case(criteria={"rubric.x": "pass"}), named)
    assert graded_named.verdict == "pass"
    assert len(rubric_calls(named)) == int(rubric_skill.model_params["samples"])


def change_record(**kw) -> dict:
    from evalops.schemas.changes import SpaceChange

    return SpaceChange(
        **{
            "change_id": "ch1",
            "space_id": "s",
            "title": "Keep filters",
            "operations": [{"op": "add_instruction", "text": "R3"}],
            "base_version": "a",
            "created_by": "o",
            "created_at": NOW,
            "updated_at": NOW,
            **kw,
        }
    ).model_dump()


async def test_a_case_the_change_hands_genie_as_an_example_never_counts_as_fixed():
    import json

    from evalops.clients.lakebase import InMemoryStore
    from evalops.schemas.evalset import EvalCaseBody
    from evalops.services import evalset
    from evalops.services.regression import results

    store = InMemoryStore()
    leaked = await evalset.create_case(
        store, "s", EvalCaseBody(title="t", turns=["What was revenue in 2024?"]), "o", set()
    )
    example = {"op": "add_sql_example", "question": "what was revenue in 2024", "sql": "SELECT 1"}
    await store.put_change("s", change_record(operations=[example], target_case_ids=[leaked.case_id]))
    row = {
        "run_id": "r",
        "space_id": "s",
        "change_id": "ch1",
        "status": "succeeded",
        "k": 3,
        "started_at": NOW,
        "arms": json.dumps([{"arm": "baseline"}, {"arm": "candidate"}]),
    }
    answers = graded(leaked.case_id, "baseline", "FFF") + graded(leaked.case_id, "candidate", "PPP")

    class Sql:
        async def execute(self, statement, parameters=None):
            if "question_intents" in statement or "run_manifests" in statement:
                return []
            if "FROM p.eval_runs" in statement:
                return [row]
            return [{**a.model_dump(), "hard_fails": "[]", "expectations": "[]"} for a in answers]

    detail = await results.run_detail("s", "r", sql=Sql(), store=store, prefix="p")

    assert detail.comparison.cases[0].outcome == "leaked"
    assert detail.comparison.counts["leaked"] == 1 and detail.comparison.counts["fixed"] == 0
    assert detail.comparison.verdict == "inconclusive"
    assert "SQL example in this change" in detail.comparison.reasons[0]


def test_a_case_that_errored_or_never_came_back_makes_the_gate_inconclusive():
    from evalops.services.regression.results import case_runs, compare

    fixed = graded("t", "baseline", "FFF") + graded("t", "candidate", "PPP")
    errored = graded("e", "baseline", "PPP") + graded("e", "candidate", "EEE")  # not a target
    splits = {"t": "test", "e": "test"}

    found = compare(case_runs(fixed + errored, {}, splits), {"t"}, decides=True, expected=splits, k=3)
    assert found.verdict == "inconclusive" and found.counts["not_graded"] == 1
    assert found.reasons[-1].startswith("1 of 2 cases (50%) were not graded")

    # Asked but no answer came back, or fewer than k attempts on one side.
    missing = compare(
        case_runs(fixed, {}, {"t": "test"}),
        {"t"},
        decides=True,
        expected={"t": "test", "m": "test"},
        k=3,
        titles={"m": "Revenue in a month"},
    )
    assert missing.verdict == "inconclusive" and "“Revenue in a month”" in missing.reasons[-1]
    short = graded("s", "baseline", "PPP") + graded("s", "candidate", "PP")
    assert compare(case_runs(fixed + short, {}), {"t"}, k=3).verdict == "inconclusive"

    assert (
        compare(case_runs(fixed, {}, {"t": "test"}), {"t"}, expected={"t": "test"}, k=3).verdict == "accept"
    )


async def test_a_finished_run_keeps_the_decision_it_was_given():
    import json

    from evalops.clients.lakebase import InMemoryStore
    from evalops.schemas.evaluation import Comparison
    from evalops.services.regression import results

    decided = Comparison(verdict="reject", decides=True, reasons=["broke “a”"], counts={}, cases=[])
    row = {
        "run_id": "r",
        "space_id": "s",
        "change_id": "ch1",
        "status": "succeeded",
        "split": "test",
        "k": 3,
        "started_at": NOW,
        "arms": json.dumps([{"arm": "baseline"}, {"arm": "candidate"}]),
        "decision": decided.model_dump_json(),
    }
    # Read today, the same answers would be an accept (the change's targets were edited since).
    answers = graded("c1", "baseline", "FFF") + graded("c1", "candidate", "PPP")

    class Sql:
        async def execute(self, statement, parameters=None):
            if "question_intents" in statement or "run_manifests" in statement:
                return []
            if "FROM p.eval_runs" in statement:
                return [row]
            return [{**a.model_dump(), "hard_fails": "[]", "expectations": "[]"} for a in answers]

    store = InMemoryStore()
    await store.put_change("s", change_record(target_case_ids=["c1"]))

    detail = await results.run_detail("s", "r", sql=Sql(), store=store, prefix="p")
    assert detail.comparison.verdict == "reject" and detail.comparison.reasons == ["broke “a”"]


def test_a_forbidden_column_is_found_however_the_sql_names_it():
    from evalops.schemas.trace import Step, StepKind, ToolName
    from evalops.services.regression.grading import must_not_query_check

    def step(sql, columns=None):
        schema = None if columns is None else [{"name": c} for c in columns]
        return Step(
            trace_id="t",
            step_index=0,
            call_id="c",
            tool_name=ToolName.EXECUTE_SQL,
            step_kind=StepKind.ANSWER,
            sql=sql,
            result_schema=schema,
        )

    def passed(*steps):
        return [r.passed for r in must_not_query_check(["email"], list(steps))]

    assert passed(step("SELECT g.email FROM guests g")) == [False]
    assert passed(step("SELECT * FROM guests", ["id", "email"])) == [False]
    assert passed(step("SELECT * FROM guests", ["id", "name"])) == [True]
    assert passed(step("SELECT * FROM guests")) == [None]  # no result columns: cannot tell
    assert passed(step("SELECT name FROM guests")) == [True]


def test_a_reference_value_is_found_even_when_it_is_small():
    from evalops.services.regression.grading import reference_check

    def passed(value, report):
        return reference_check(["n"], [[value]], report).passed

    assert passed(5, "There are 5 bookings.")
    assert passed(5, "There are five bookings.")
    assert passed(0, "There were zero cancellations.")
    assert passed(2024, "The busiest year was 2024.")
    assert not passed(5, "There are 6 bookings.")


def test_a_reference_value_said_of_another_row_fails():
    from evalops.services.regression.grading import reference_check

    columns, rows = ["country", "revenue"], [["Spain", 2_520_000], ["Thailand", 1_780_000]]
    assert reference_check(columns, rows, "Spain: $2.52M; Thailand: $1.78M.").passed
    swapped = reference_check(columns, rows, "Spain: $1.78M; Thailand: $2.52M.")
    assert not swapped.passed and "said of the wrong row or column" in swapped.detail
