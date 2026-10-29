"""Tests for evalops.services.parser against real (sanitized) Genie responses in tests/fixtures/genie/."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalops.schemas.genie import ItemsPage
from evalops.schemas.trace import AccessStatus, StepKind, ToolName
from evalops.services.parser import access_failure_trace, classify_step, parse_conversation, split_turns

FIXTURES = Path(__file__).parent / "fixtures" / "genie"
NOW = datetime(2026, 10, 20, tzinfo=UTC)


def load_items(name: str):
    return ItemsPage.model_validate(json.loads((FIXTURES / f"items_{name}.json").read_text())).data


def parse(name: str):
    return parse_conversation("space-1", f"conv-{name}", load_items(name), collected_at=NOW)


def test_single_sql_turn_becomes_one_trace_with_one_answer_step():
    [(trace, steps)] = parse("single_sql")

    assert trace.question == "How many bookings were made in June 2025?"
    assert "9,678" in trace.report_text
    assert trace.n_sql_steps == 1 and trace.n_tool_calls == 1
    assert trace.access_status == AccessStatus.OK
    assert trace.asked_at is not None and trace.asked_at <= trace.completed_at
    [step] = steps
    assert step.tool_name == ToolName.EXECUTE_SQL
    assert step.step_kind == StepKind.ANSWER
    assert step.sql.startswith("SELECT") and "June" not in step.sql  # the SQL, not the title
    assert step.statement_id  # needed to fetch the result rows later


def test_charts_are_steps_too_but_have_no_statement():
    [(trace, steps)] = parse("multi_step_viz")

    assert [s.tool_name for s in steps] == [ToolName.EXECUTE_SQL] * 2 + [ToolName.GENERATE_VISUALIZATION] * 2
    assert [s.step_index for s in steps] == [0, 1, 2, 3]
    assert trace.n_sql_steps == 2 and trace.n_tool_calls == 4
    assert all(s.statement_id is None and s.step_kind == StepKind.VIZ for s in steps[2:])
    # A chart call and its output share one item id; pairing by call_id still finds the output.
    assert len({s.call_id for s in steps}) == 4


def test_a_chart_keeps_the_query_it_plots():
    """The API returns no chart spec, only its title and `query_attachment_id`, which is the
    call_id of the SQL step it plots: reviewers see that query's rows instead of the chart."""
    for _, steps in parse("multi_turn"):
        sql_ids = {s.call_id for s in steps if s.tool_name == ToolName.EXECUTE_SQL}
        charts = [s for s in steps if s.tool_name == ToolName.GENERATE_VISUALIZATION]
        assert charts and all(c.source_call_id in sql_ids for c in charts)
        assert all(s.source_call_id is None for s in steps if s.tool_name == ToolName.EXECUTE_SQL)


def test_a_refusal_has_a_report_and_no_steps():
    [(trace, steps)] = parse("refusal_no_sql")

    assert steps == []
    assert trace.n_sql_steps == 0
    assert "not available" in trace.report_text


def test_a_multi_turn_conversation_becomes_one_trace_per_turn_in_order():
    turns = parse("multi_turn")

    assert [t.turn_index for t, _ in turns] == [0, 1, 2]
    assert [t.question for t, _ in turns] == [
        "Top destinations by bookings in 2025",
        "and their cancellation rates?",
        "only the ones above 20%",
    ]
    assert len({t.trace_id for t, _ in turns}) == 3  # one Genie message id per turn
    for trace, steps in turns:
        assert all(s.trace_id == trace.trace_id for s in steps)  # steps never leak across turns


def test_split_turns_drops_items_without_a_message_id():
    items = load_items("single_sql")
    items[0].metadata.message_id = None

    turns = split_turns(items)

    assert sum(len(v) for v in turns.values()) == len(items) - 1


@pytest.mark.parametrize(
    ("title", "sql", "tool", "expected"),
    [
        ("Sample booking metrics data", "SELECT * FROM v LIMIT 3", "execute_sql", StepKind.EXPLORE),
        (
            "Sample booking data to check available fields",
            "SELECT a, MEASURE(b) FROM v GROUP BY ALL LIMIT 3",
            "execute_sql",
            StepKind.EXPLORE,
        ),
        ("Peek", "SELECT a FROM v LIMIT 5", "execute_sql", StepKind.EXPLORE),
        (
            "Top 5 destinations",
            "SELECT d, MEASURE(r) FROM v GROUP BY ALL ORDER BY 2 DESC LIMIT 5",
            "execute_sql",
            StepKind.ANSWER,
        ),
        (
            "Total bookings in June 2025",
            "SELECT MEASURE(`Bookings`) FROM v WHERE m = '2025-06-01'",
            "execute_sql",
            StepKind.ANSWER,
        ),
        ("Bookings by month", None, "generate_visualization", StepKind.VIZ),
    ],
)
def test_classify_step(title, sql, tool, expected):
    assert classify_step(title, sql, tool) == expected


def test_unreadable_conversation_becomes_a_placeholder_trace():
    trace = access_failure_trace(
        "space-1", "conv-9", AccessStatus.OWNER_ONLY, asked_at=None, collected_at=NOW
    )

    assert trace.trace_id == "conv-9:unreadable"
    assert trace.access_status == AccessStatus.OWNER_ONLY
    assert trace.question is None and trace.n_sql_steps == 0


def test_a_genie_internal_error_is_captured_not_mistaken_for_a_refusal():
    """Regression: a consumer without access to the data got 'An internal error occurred'
    (a `system` message) and the trace had silently passed with no report."""
    [(trace, steps)] = parse("genie_error")

    assert trace.report_text is None
    assert trace.genie_error == "An internal error occurred"
    assert steps == []
