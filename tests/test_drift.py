"""Eval set suggestions from production traffic."""

from datetime import UTC, datetime, timedelta

from evalops.clients.lakebase import InMemoryStore
from evalops.schemas.evalset import EvalCase, EvalCaseBody
from evalops.schemas.llm import IntentOutput, IntentTag
from evalops.services import drift, evalset
from evalops.services.llm.runner import SkillResult
from evalops.services.llm.skills import load_skill, skills_dir

NOW = datetime(2026, 10, 22, tzinfo=UTC)
SINCE = NOW - timedelta(days=30)


def case(case_id, turns, updated=SINCE - timedelta(days=5), status="active") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        space_id="s",
        version=1,
        title=turns[-1],
        turns=turns,
        updated_by="o",
        updated_at=updated,
        status=status,
    )


def asked(trace_id, question):
    return {"trace_id": trace_id, "question": question, "asked_at": NOW}


def test_users_intents_without_a_case_are_suggested_and_cases_nobody_asks_about_retired():
    traffic = [
        asked("t1", "Top countries"),
        asked("t2", "Best countries?"),
        asked("t3", "Revenue 2024"),
        asked("t4", "Host email"),
    ]
    intents = {
        ("trace", "t1"): "ranking of countries",
        ("trace", "t2"): "ranking of countries",
        ("trace", "t3"): "revenue for a period",
        ("trace", "t4"): "personal details of a host",
        ("case", "a@1"): "revenue for a period",
        ("case", "b@1"): "cancellation rate above a threshold",
        ("case", "c@1"): "cancellation rate above a threshold",
        ("case", "new@1"): "monthly trend",
    }
    cases = [
        case("a", ["Revenue 2024"]),
        case("b", ["Top 20", "above 20%"]),
        case("c", ["Top 10", "above 25%"]),
        case("new", ["Monthly trend"], updated=NOW),  # too new to retire
        case("gone", ["x"], status="retired"),
    ]

    s = drift.suggest(
        traffic,
        intents,
        cases,
        {"b": [True, True, False]},
        space_id="s",
        window_days=30,
        since=SINCE,
        min_questions=2,
    )

    assert [(a.intent, a.count) for a in s.add] == [
        ("ranking of countries", 2)
    ]  # the host email was asked once
    assert {r.case_id for r in s.retire} == {"b", "c"}
    assert [(f.case_id, f.flaky_runs, f.runs) for f in s.flaky] == [("b", 2, 3)]
    assert [d.case_ids for d in s.duplicates] == [["b", "c"]]
    assert (s.tagged_questions, s.untagged_questions) == (4, 0)


def test_a_follow_up_case_is_tagged_with_the_turn_it_follows():
    assert drift.case_question(case("b", ["Top 20", "above 20%"])) == "above 20% (follow-up to: Top 20)"


class Delta:
    def __init__(self, existing=()):
        self.existing, self.merged = list(existing), []

    async def load_intents(self, space_id):
        return self.existing

    async def merge_intents(self, rows):
        self.merged.extend(rows)


class Sql:
    async def execute(self, statement, parameters=None):
        return [asked("t1", "Top countries"), asked("t2", "Best countries?")]


class Tagger:
    model = "fake"

    def __init__(self):
        self.inputs = []

    async def run(self, skill, data, temperature=None):
        self.inputs.append(data)
        n = len(data.questions.splitlines())
        return SkillResult(
            IntentOutput(tags=[IntentTag(number=i, intent="Ranking of Countries") for i in range(1, n + 1)]),
            None,
            skill.ref,
            "fake",
            "",
            0.1,
            10,
        )


async def test_only_new_or_changed_questions_are_tagged_and_known_intents_are_offered_for_reuse():
    store = InMemoryStore()
    await evalset.create_case(store, "s", EvalCaseBody(title="t", turns=["Top countries"]), "o", set())
    already = {
        "kind": "trace",
        "ref_id": "t1",
        "question_hash": drift._hash("Top countries"),
        "intent": "ranking of countries",
    }
    delta, tagger = Delta([already]), Tagger()

    n = await drift.tag_intents(
        "s",
        sql=Sql(),
        store=store,
        delta=delta,
        prefix="p",
        runner=tagger,
        skill=load_skill("intent_tagger", skills_dir("")),
        window_days=30,
        now=NOW,
    )

    assert n == 2  # t2 and the case; t1 was tagged with the same question before
    assert sorted(
        (r["kind"], r["ref_id"].split("@")[0] if r["kind"] == "trace" else "case") for r in delta.merged
    ) == [
        ("case", "case"),
        ("trace", "t2"),
    ]
    assert all(r["intent"] == "ranking of countries" for r in delta.merged)  # normalised to lowercase
    assert "ranking of countries" in tagger.inputs[0].intents


def test_a_case_promoted_from_an_answer_covers_that_answers_intent():
    from evalops.schemas.evalset import CaseSource

    promoted = case("p", ["Top 20", "above 20%"]).model_copy(update={"source": CaseSource(trace_id="t1")})
    traffic = [asked("t1", "above 20%"), asked("t2", "only above 25%")]
    intents = {
        ("trace", "t1"): "top destinations filtered by threshold",
        ("trace", "t2"): "top destinations filtered by threshold",
        ("case", "p@1"): "cancellation rates filtered by threshold",  # labelled differently on its own
    }

    s = drift.suggest(
        traffic, intents, [promoted], {}, space_id="s", window_days=30, since=SINCE, min_questions=2
    )

    assert s.add == [] and s.retire == []
