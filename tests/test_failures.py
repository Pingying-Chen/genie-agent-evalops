"""Tests for evalops.services.failures: how failure categories are ranked and suggested."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from evalops.clients.lakebase import InMemoryStore
from evalops.schemas.api import FailureCategory, Observation
from evalops.schemas.checks import Criterion
from evalops.schemas.llm import CategorizeOutput
from evalops.services.failures import draft_criterion, frequency_score, suggest_categories, summarise
from evalops.services.llm.runner import SkillResult
from evalops.services.llm.skills import load_skill

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def category(cid: str, name: str, impact: int) -> FailureCategory:
    return FailureCategory(id=cid, name=name, impact=impact, created_at=NOW, updated_by="o", updated_at=NOW)


def observation(trace_id: str, ids: list[str], *, failed: bool = True, caught: bool = False) -> Observation:
    return Observation(
        trace_id=trace_id,
        reviewer="r",
        task_success="fail" if failed else "pass",
        caught_by=["sql.avg_of_ratio"] if caught else [],
        category_ids=ids,
    )


def test_priority_is_frequency_score_times_impact_so_a_rarer_severe_failure_can_outrank_a_common_mild_one():
    wrong_filter, wording, unused = (
        category("a", "Wrong filter", 4),
        category("b", "Wording", 1),
        category("c", "Unused", 2),
    )
    items = [
        observation("t1", ["a"]),
        observation("t2", ["b"], failed=False),
        observation("t3", ["b"], caught=True),
        observation("t4", ["a", "b"], caught=True),
    ]

    ranked = summarise([wording, unused, wrong_filter], items, reviewed=10)

    assert [c.name for c in ranked] == ["Wrong filter", "Wording", "Unused"]
    top, mild, empty = ranked
    assert (top.turns, top.failed, top.caught) == (2, 2, 1)
    assert (top.frequency, top.frequency_score, top.priority) == (0.2, 4, 16)  # 20% -> 4, x impact 4
    assert (mild.turns, mild.failed, mild.frequency_score, mild.priority) == (3, 2, 5, 5)  # 30% -> 5, x 1
    assert (empty.turns, empty.priority) == (0, None)  # named but not used yet: listed, not ranked


def test_a_linked_category_shows_what_its_criterion_catches_and_its_false_alarms():
    wrong_filter = category("a", "Wrong filter", 4).model_copy(update={"criterion_id": "sql.avg_of_ratio"})
    items = [observation("t1", ["a"], caught=True), observation("t2", ["a"])]  # caught = sql.avg_of_ratio

    [linked, unlinked] = summarise(
        [wrong_filter, category("b", "Other", 1)], items, 10, {"sql.avg_of_ratio": 2}
    )

    assert (linked.linked_caught, linked.linked_false_alarms) == (1, 2)
    assert (unlinked.linked_caught, unlinked.linked_false_alarms) == (None, None)


def test_a_draft_is_a_valid_criterion_file_listing_the_reviewed_answers():
    leaks = category("a", "Exposes internal table names!", 5).model_copy(
        update={"description": "Shows schema or table names to the user."}
    )
    tagged = [
        observation("t9", ["a"], failed=False).model_copy(
            update={"question": "complaints?", "findings": "table names"}
        )
    ]

    d = draft_criterion(leaks, tagged)

    assert (d.criterion_id, d.filename) == (
        "rubric.exposes_internal_table_names",
        "rubric.exposes_internal_table_names.yaml",
    )
    parsed = Criterion.model_validate(yaml.safe_load(d.yaml))  # what load_criteria will do with it
    assert (parsed.family, parsed.gate, parsed.params["failure"]) == (
        "rubric",
        "monitor",
        "Shows schema or table names to the user.",
    )
    assert "TODO" in parsed.params["rule"]  # the owner writes when it applies
    assert "#   t9  'complaints?'  (pass) table names" in d.yaml


@pytest.mark.parametrize(
    ("rate", "score"),
    [(0.005, 1), (0.01, 2), (0.049, 2), (0.05, 3), (0.15, 4), (0.299, 4), (0.30, 5), (1.0, 5)],
)
def test_frequency_score_bands_run_from_under_1_percent_to_30_percent_or_more(rate, score):
    assert frequency_score(rate) == score


def test_without_reviews_there_is_no_frequency_to_rank_by():
    [only] = summarise([category("a", "Wrong filter", 3)], [], reviewed=0)

    assert (only.frequency, only.frequency_score, only.priority) == (None, None, None)


# ------------------------------------------------------------------ suggestions (review job)


SKILL = load_skill("categorize_observation", Path(__file__).resolve().parents[1] / "skills")
ROWS = [
    {
        "trace_id": "t1",
        "task_success": "fail",
        "task_rationale": "used a >20 filter",
        "findings": None,
        "reviewer": "r",
        "question": "only the ones above 20%",
        "turn_index": 2,
        "caught_by": [],
    },
    {
        "trace_id": "t2",
        "task_success": "pass",
        "task_rationale": None,
        "findings": "shows table names",
        "reviewer": "r",
        "question": "complaints?",
        "turn_index": 0,
        "caught_by": [],
    },
]


class ObservationSql:
    async def execute(self, statement, parameters=None):
        return [dict(r) for r in ROWS] if "AS caught_by" in statement else [{"reviewed": 13}]


class SuggestionDelta:
    def __init__(self):
        self.saved = {}

    async def load_suggestions(self, space_id):
        return dict(self.saved)

    async def merge_suggestions(self, suggestions):
        self.saved.update({s.trace_id: s for s in suggestions})


class ScriptedRunner:
    """Answers with the category named in `answers` for the question in the prompt; None = an error."""

    model = "fake-model"

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    async def run(self, skill, data, *, temperature=None):
        self.prompts.append(skill.render(data))
        answer = self.answers[data.question]
        output = (
            None
            if answer is None
            else CategorizeOutput(
                reasoning="because", category=answer, is_new=True, description="what it looks like"
            )
        )
        return SkillResult(output, None if output else "invalid output", skill.ref, self.model, "", 0.1, 10)


async def suggest(store, delta, runner):
    return await suggest_categories(
        "s", sql=ObservationSql(), store=store, delta=delta, prefix="p", runner=runner, skill=SKILL, now=NOW
    )


async def test_untagged_observations_get_a_suggestion_that_reuses_an_existing_category_by_name():
    store, delta = InMemoryStore(), SuggestionDelta()
    await store.put_category("s", category("a", "Leaks internals", 2).model_dump())
    runner = ScriptedRunner(
        {"only the ones above 20%": "Applies an unasked filter", "complaints?": "leaks INTERNALS"}
    )

    assert await suggest(store, delta, runner) == 2

    new, reused = delta.saved["t1"], delta.saved["t2"]
    assert (new.category, new.is_new, new.description) == (
        "Applies an unasked filter",
        True,
        "what it looks like",
    )
    assert (reused.category, reused.is_new, reused.description) == ("Leaks internals", False, "")
    assert "Leaks internals" in runner.prompts[0]  # the model sees the existing categories


async def test_suggestions_are_redone_only_when_the_input_changes_and_never_for_tagged_ones():
    store, delta = InMemoryStore(), SuggestionDelta()
    runner = ScriptedRunner({"only the ones above 20%": "Wrong filter", "complaints?": "Leaks internals"})
    await suggest(store, delta, runner)
    await store.set_tags("s", "t2", ["a"], "owner")

    assert await suggest(store, delta, runner) == 0  # nothing changed: no LLM call
    assert len(runner.prompts) == 2

    await store.put_category("s", category("b", "Wrong filter", 3).model_dump())
    assert await suggest(store, delta, runner) == 1  # a new category refreshes t1; t2 is tagged
    assert delta.saved["t1"].is_new is False


async def test_a_failed_llm_call_writes_no_suggestion():
    delta = SuggestionDelta()

    assert (
        await suggest(
            InMemoryStore(), delta, ScriptedRunner({"only the ones above 20%": None, "complaints?": "X"})
        )
        == 1
    )
    assert set(delta.saved) == {"t2"}
