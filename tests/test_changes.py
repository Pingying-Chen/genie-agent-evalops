"""Space changes: operations applied to a configuration."""

import json
from datetime import UTC, datetime

import pytest

from evalops.errors import ValidationFailed
from evalops.schemas.changes import ChangeBody
from evalops.services.regression.changes import apply, describe

SPACE = {
    "version": 1,
    "instructions": {
        "text_instructions": [{"id": "a" * 32, "content": ["R1 one\n", "R2 two\n"]}],
        "example_question_sqls": [{"id": "e" * 32, "question": ["q"], "sql": ["SELECT 1\n"]}],
    },
    "data_sources": {"tables": [{"identifier": "c.s.v"}]},
}


def ops(*operations):
    return ChangeBody(title="t", operations=list(operations)).operations


def test_instruction_lines_are_added_replaced_and_removed_in_place():
    out = json.loads(
        apply(
            json.dumps(SPACE),
            ops(
                {"op": "replace_instruction", "old": "R2 two", "text": "R2 two, stated better"},
                {"op": "add_instruction", "text": "R3 three"},
                {"op": "remove_instruction", "old": "R1 one"},
            ),
        )
    )

    [block] = out["instructions"]["text_instructions"]
    assert block["id"] == "a" * 32  # the block keeps its id
    assert block["content"] == ["R2 two, stated better\n", "R3 three\n"]


def test_a_sql_example_and_column_metadata_are_added_and_the_input_is_not_changed(monkeypatch):
    from evalops.services.regression import changes

    monkeypatch.setattr(changes, "_new_id", lambda: "0" * 32)  # sorts before the existing "eee..."
    before = json.dumps(SPACE)
    out = json.loads(
        apply(
            before,
            ops(
                {"op": "add_sql_example", "question": "Rate above 20%?", "sql": "SELECT a\nFROM v"},
                {
                    "op": "set_column",
                    "table": "c.s.v",
                    "column": "cancellation_rate",
                    "synonyms": ["cancel rate", " "],
                },
            ),
        )
    )

    examples = out["instructions"]["example_question_sqls"]
    example = next(e for e in examples if e["question"] == ["Rate above 20%?"])
    assert example["sql"] == ["SELECT a\n", "FROM v\n"] and len(example["id"]) == 32
    # Genie refuses a configuration whose examples are not sorted by id (found on a live run).
    assert [e["id"] for e in examples] == sorted(e["id"] for e in examples)
    assert out["data_sources"]["tables"][0]["column_configs"] == [
        {"column_name": "cancellation_rate", "synonyms": ["cancel rate"]}
    ]
    assert json.dumps(SPACE) == before


def test_an_operation_that_does_not_fit_the_configuration_is_refused():
    with pytest.raises(ValidationFailed, match="no instruction line"):
        apply(json.dumps(SPACE), ops({"op": "remove_instruction", "old": "R9 nine"}))
    with pytest.raises(ValidationFailed, match="no table"):
        apply(
            json.dumps(SPACE), ops({"op": "set_column", "table": "x.y.z", "column": "c", "description": "d"})
        )


def test_each_operation_is_described_in_one_line():
    assert describe(
        ops(
            {"op": "add_instruction", "text": "R3"},
            {"op": "set_column", "table": "t", "column": "c", "description": "d"},
        )
    ) == [
        "Add instruction: R3",
        "Set description of t.c",
    ]


# ------------------------------------------------------------------ apply and roll back


from evalops.clients.lakebase import InMemoryStore  # noqa: E402
from evalops.errors import Conflict  # noqa: E402
from evalops.schemas.evaluation import Comparison  # noqa: E402
from evalops.schemas.genie import SpaceConfig  # noqa: E402
from evalops.services import space_changes  # noqa: E402


class SpaceGenie:
    """A space whose configuration the fake keeps, as Genie would."""

    def __init__(self, serialized: str):
        self.config = SpaceConfig(title="T", warehouse_id="wh", serialized_space=serialized)
        self.writes = []

    async def get_space_config(self, space_id):
        return self.config

    async def update_space(self, space_id, config):
        self.writes.append(config.serialized_space)
        self.config = config


def verdict(v: str):
    async def latest(*args, **kwargs):
        return Comparison(verdict=v, reasons=[] if v == "accept" else ["broke “x”"], counts={}, cases=[])

    return latest


async def make_tested_change(store, genie):
    body = ChangeBody(title="R3", operations=[{"op": "add_instruction", "text": "R3 three"}])
    change = await space_changes.create_change(store, "s", body, "owner@x", genie.config)
    await space_changes.set_status(store, "s", change.change_id, "tested")
    return change


async def test_an_accepted_change_is_applied_and_rolled_back_to_the_exact_configuration(monkeypatch):
    store, genie = InMemoryStore(), SpaceGenie(json.dumps(SPACE))
    original = genie.config.serialized_space
    change = await make_tested_change(store, genie)
    monkeypatch.setattr(space_changes, "latest_gate", verdict("accept"))

    applied = await space_changes.apply_change(
        "s", change.change_id, genie=genie, sql=None, store=store, prefix="p"
    )
    assert applied.status == "applied" and applied.applied_version == genie.config.version
    assert "R3 three" in genie.writes[-1]

    rolled = await space_changes.rollback_change(
        "s", change.change_id, genie=genie, sql=None, store=store, prefix="p"
    )
    assert rolled.status == "rolled_back" and genie.writes[-1] == original
    assert "replaced_config" not in rolled.model_dump()  # kept in storage, never sent to clients


async def test_a_change_is_not_applied_unless_accepted_or_overridden_or_after_the_space_moved(monkeypatch):
    store, genie = InMemoryStore(), SpaceGenie(json.dumps(SPACE))
    change = await make_tested_change(store, genie)
    monkeypatch.setattr(space_changes, "latest_gate", verdict("reject"))

    with pytest.raises(Conflict, match="reject"):
        await space_changes.apply_change(
            "s", change.change_id, genie=genie, sql=None, store=store, prefix="p"
        )
    assert genie.writes == []

    genie.config = genie.config.model_copy(update={"serialized_space": json.dumps({**SPACE, "version": 2})})
    with pytest.raises(Conflict, match="space changed"):
        await space_changes.apply_change(
            "s", change.change_id, genie=genie, sql=None, store=store, prefix="p", override=True
        )


async def test_a_rollback_is_refused_once_someone_edited_the_space_after_the_apply(monkeypatch):
    store, genie = InMemoryStore(), SpaceGenie(json.dumps(SPACE))
    change = await make_tested_change(store, genie)
    monkeypatch.setattr(space_changes, "latest_gate", verdict("accept"))
    await space_changes.apply_change("s", change.change_id, genie=genie, sql=None, store=store, prefix="p")
    genie.config = genie.config.model_copy(update={"serialized_space": json.dumps({**SPACE, "edited": True})})

    with pytest.raises(Conflict, match="changed after"):
        await space_changes.rollback_change(
            "s", change.change_id, genie=genie, sql=None, store=store, prefix="p"
        )


async def test_a_proposal_keeps_only_operations_that_fit_and_targets_the_categorys_cases(monkeypatch):
    from evalops.schemas.api import CategorySummary, Observation, Observations
    from evalops.schemas.evalset import EvalCaseBody
    from evalops.schemas.llm import ProposeChangeOutput, ProposedOperation
    from evalops.services import evalset, failures
    from evalops.services.llm.runner import SkillResult
    from evalops.services.llm.skills import load_skill, skills_dir

    now = datetime(2026, 10, 22, tzinfo=UTC)
    category = CategorySummary(
        id="cat",
        name="Drops earlier filters",
        description="Follow-ups lose turn 1's filters",
        impact=4,
        created_at=now,
        updated_by="o",
        updated_at=now,
        turns=1,
        failed=1,
        caught=0,
        frequency=0.1,
        frequency_score=3,
        priority=12,
    )
    seen = Observation(
        trace_id="t",
        question="only the ones above 20%",
        task_success="fail",
        task_rationale="lost the top-20",
        reviewer="r",
        category_ids=["cat"],
    )

    async def analysis(*args, **kwargs):
        return Observations(space_id="s", reviewed=10, passed=9, items=[seen], categories=[category])

    monkeypatch.setattr(failures, "analysis", analysis)
    store = InMemoryStore()
    await store.put_category("s", category.model_dump())
    case = await evalset.create_case(
        store,
        "s",
        EvalCaseBody(title="t", turns=["Top 20", "only above 20%"], category_ids=["cat"]),
        "o",
        set(),
    )
    blank = dict(old="", text="", question="", sql="", table="", column="", description="", synonyms=[])

    class Proposer:
        model = "fake"

        def __init__(self):
            self.inputs = []

        async def run(self, skill, data, temperature=None):
            self.inputs.append(data)
            out = ProposeChangeOutput(
                diagnosis="no rule keeps earlier filters",
                title="Keep filters",
                rationale="Follow-ups inherit filters.",
                operations=[
                    ProposedOperation(op="add_instruction", **{**blank, "text": "R3 Keep earlier filters."}),
                    ProposedOperation(op="replace_instruction", **{**blank, "old": "R9 nine", "text": "x"}),
                ],
            )
            return SkillResult(out, None, skill.ref, "fake", "", 0.1, 100)

    genie = SpaceGenie(json.dumps(SPACE))
    proposer = Proposer()
    proposal = await space_changes.propose(
        "s",
        "cat",
        config=genie.config,
        sql=None,
        store=store,
        prefix="p",
        runner=proposer,
        skill=load_skill("space_change_proposer", skills_dir("")),
    )

    assert [o.op for o in proposal.draft.operations] == ["add_instruction"]
    assert proposal.dropped == ["replace_instruction: Operation 1: no instruction line reads 'R9 nine'."]
    assert proposal.draft.target_case_ids == [case.case_id]
    sent = proposer.inputs[0]
    assert (
        "R1 one" in sent.instructions
        and "lost the top-20" in sent.failures
        and "Top 20 / only above 20%" in sent.cases
    )


# ------------------------------------------------------------------ leakage

from evalops.schemas.evalset import EvalCase  # noqa: E402
from evalops.services.regression.leakage import config_examples, leaks, similar  # noqa: E402


def test_the_same_question_matches_whatever_its_case_and_punctuation_but_a_different_one_does_not():
    assert similar("What was realized revenue in 2024?", "what was realized revenue in 2024")
    assert similar("Top destinations by bookings in 2025", "Top destinations by bookings, 2025")
    assert not similar(
        "What was realized revenue in 2024?", "What was realized revenue in 2023?"
    )  # another year
    assert not similar("Top destinations by revenue", "Top destinations by cancellation rate")
    assert not similar("", "anything")


def test_any_turn_of_an_active_case_can_leak_and_retired_cases_are_ignored():
    now = datetime(2026, 10, 22, tzinfo=UTC)

    def case(case_id, turns, status="active"):
        return EvalCase(
            case_id=case_id,
            space_id="s",
            version=1,
            title=turns[-1],
            turns=turns,
            status=status,
            updated_by="o",
            updated_at=now,
        )

    cases = [
        case("a", ["Top destinations by bookings in 2025", "only above 20%"]),
        case("b", ["Revenue 2024?"], "retired"),
    ]
    examples = config_examples(
        json.dumps(
            {
                "instructions": {
                    "example_question_sqls": [
                        {
                            "id": "x",
                            "question": ["Top destinations by bookings in 2025"],
                            "sql": ["SELECT 1"],
                        },
                        {"id": "y", "question": ["Revenue 2024"], "sql": ["SELECT 2"]},
                    ]
                }
            }
        )
    )

    found = leaks(examples, cases)

    assert [(f.case_id, f.turn) for f in found] == [("a", 0)]  # the earlier turn; "b" is retired
