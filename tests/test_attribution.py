"""report.figure_attributed: a figure found in the results must be said of its own row and
column. The swapped-value errors `report.figure_sourced` cannot see."""

import pytest

from evalops.services.checks.attribution import Table, attribute_all
from evalops.services.checks.figures import extract_figures

REGIONS = Table(
    columns=["Continent", "Realized Revenue", "Bookings"],
    rows=[["Europe", 4_200_000, 9_100], ["Asia", 3_100_000, 7_300]],
)
MONTHS = Table(
    columns=["Booking Month", "Bookings"],
    rows=[["2024-06-01T00:00:00.000Z", 5_400], ["2024-05-01T00:00:00.000Z", 4_300]],
)
YEARS = Table(columns=["Booking Year", "Bookings"], rows=[[2024, 70_500], [2025, 31_200]])


def statuses(report: str, *tables: Table) -> list[str]:
    return [a.status for a in attribute_all(extract_figures(report), report, list(tables))]


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("Europe had $4.2M in revenue.", ["consistent"]),
        ("Asia had $4.2M in revenue.", ["misattributed"]),  # Europe's value, said of Asia
        ("Europe had $4.2M, Asia had $3.1M.", ["consistent", "consistent"]),
        ("Europe had $3.1M, Asia had $4.2M.", ["misattributed", "misattributed"]),  # swapped
        ("Revenue reached $4.2M overall.", ["unclear"]),  # names neither its row nor a column
        ("| Asia | $4.2M |", ["misattributed"]),  # a table row
        ("Europe's realized revenue was 9,100.", ["misattributed"]),  # its row, but the bookings column
        ("Europe had 9,100 bookings.", ["consistent"]),
    ],
)
def test_a_figure_is_checked_against_its_own_row_and_column(report, expected):
    assert statuses(report, REGIONS) == expected


COUNTRIES = Table(
    columns=["Country", "revenue", "bookings"],
    rows=[["Spain", 2_520_000, 13_079], ["Thailand", 1_780_000, 9_209], ["Australia", 1_460_000, 7_437]],
)


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        # A label governs the figures after it until the next label.
        (
            "Spain leads with $2.52M from 13,079 bookings, followed by Thailand ($1.78M, 9,209 bookings) "
            "and Australia ($1.46M, 7,437 bookings).",
            ["consistent"] * 6,
        ),
        (
            "Spain leads with $2.52M from 13,079 bookings, followed by Thailand ($1.46M, 7,437 bookings) "
            "and Australia ($1.78M, 9,209 bookings).",
            ["consistent", "consistent", "misattributed", "misattributed", "misattributed", "misattributed"],
        ),
        # A label right after a figure governs it.
        ("$1.78M for Thailand and $1.46M for Australia.", ["consistent", "consistent"]),
        ("$1.46M for Thailand and $1.78M for Australia.", ["misattributed", "misattributed"]),
        # The unit of the previous figure is not this figure's column.
        ("Thailand: 9,209 bookings ($1.78M).", ["consistent", "consistent"]),
        # A label does not carry into the next sentence.
        ("Australia had $1.46M. Most countries saw about 9,209 bookings.", ["consistent", "consistent"]),
    ],
)
def test_labels_govern_the_figures_that_follow_them(report, expected):
    assert statuses(report, COUNTRIES) == expected


def test_a_range_or_a_bound_is_about_a_group_and_is_not_judged():
    assert statuses("Australia had over $1.7M.", COUNTRIES) == []
    assert statuses("Australia's revenue was ranging from $1.4M to $1.8M.", COUNTRIES) == []


def test_a_column_named_inside_the_figures_own_column_is_not_another_column():
    diff = Table(columns=["difference", "difference_pct"], rows=[[7_800_000, 63.7]])
    # "difference" is in `difference_pct` too: no false alarm, and not enough to call it consistent.
    assert statuses("That is a 63.7% difference.", diff) == ["unclear"]


def test_periods_match_however_the_report_writes_them():
    assert statuses("In June 2024 there were 5,400 bookings.", MONTHS) == ["consistent"]
    assert statuses("In June 2024 there were 4,300 bookings.", MONTHS) == ["misattributed"]
    assert statuses("In 2025, bookings were 31,200.", YEARS) == ["consistent"]
    assert statuses("In 2025, bookings were 70,500.", YEARS) == ["misattributed"]


def test_a_value_shared_by_two_rows_belongs_to_both():
    shared = Table(columns=["Region", "Rate"], rows=[["North", 5.5], ["South", 5.5], ["East", 7.1]])
    assert statuses("South: 5.5", shared) == ["consistent"]
    assert statuses("East: 5.5", shared) == ["misattributed"]


def test_a_figure_that_matches_no_cell_is_left_to_figure_sourced():
    assert statuses("Europe had $9.9M.", REGIONS) == []


def test_the_check_fails_a_report_that_swaps_two_values():
    from datetime import UTC, datetime

    from test_checks import trace

    from evalops.schemas.checks import Verdict
    from evalops.schemas.trace import Step, StepKind, ToolName
    from evalops.services.checks import run_checks
    from evalops.services.criteria import criteria_dir, load_criteria

    criteria, _ = load_criteria(criteria_dir())
    [rule] = [c for c in criteria if c.id == "report.figure_attributed"]
    step = Step(
        trace_id="t",
        step_index=0,
        call_id="c",
        tool_name=ToolName.EXECUTE_SQL,
        step_kind=StepKind.ANSWER,
        sql="SELECT continent, MEASURE(`Realized Revenue`), MEASURE(Bookings) FROM v GROUP BY ALL",
        result_schema=[{"name": c} for c in REGIONS.columns],
        result_rows=REGIONS.rows,
    )

    def verdict(report):
        [result] = run_checks(
            trace(report, question="Revenue by continent?"),
            [step],
            [rule],
            run_id="r",
            now=datetime(2026, 10, 28, tzinfo=UTC),
        )
        return result.verdict

    assert verdict("Europe led with $4.2M; Asia followed with $3.1M.") == Verdict.PASS
    assert verdict("Asia led with $4.2M; Europe followed with $3.1M.") == Verdict.FAIL
    assert verdict("Revenue totalled $7.3M.") == Verdict.NA  # a derived figure: nothing to attribute
