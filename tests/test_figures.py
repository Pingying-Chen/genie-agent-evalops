"""Tests for evalops.services.checks.figures (the hallucination guardrail).

Most cases are regressions: each false alarm found on the demo's 23 real reports became a test.
"""

import pytest

from evalops.services.checks.figures import Evidence, echoed_from_question, extract_figures, source_figure


def texts(report: str) -> list[str]:
    return [f.text for f in extract_figures(report)]


def kinds(report: str) -> list[tuple[str, str]]:
    return [(f.text, f.kind) for f in extract_figures(report)]


# ------------------------------------------------------------------ extraction


def test_extracts_percent_counts_and_currency_with_abbreviations():
    report = "Rate was **19.5%** (4,339 of 22,232). Revenue rose from $971.9K to $4.76M."
    assert texts(report) == ["19.5%", "4,339", "22,232", "$971.9K", "$4.76M"]


def test_citation_links_are_not_figures():
    report = "There were 9,678 bookings. \\[[1](https://x.databricks.com/genie/rooms/01f1/chats/02?o=0&gra_focus=toolu_1)\\]"
    assert texts(report) == ["9,678"]


def test_a_markdown_heading_number_is_a_figure():
    """Regression: `#` in the label pattern once swallowed '## 9,678 Bookings'."""
    assert texts("## 9,678 Bookings in June 2025\n\nThere were **9,678 bookings**.") == ["9,678", "9,678"]


@pytest.mark.parametrize(
    "report",
    [
        "data through July 30, 2025",  # regression: '30' was flagged as unsourced in 3 reports
        "on 2025-06-01",
        "in 2024",
        "Q2 2025 and P3",
        "the Top 5 destinations",
        "#1 is Paris",
        "3 countries",
        "between 2023 and 2024",
    ],
)
def test_dates_years_labels_and_small_counts_are_skipped(report):
    assert texts(report) == []


def test_ranges_carry_the_unit_to_both_ends():
    """Regression: '21-23%' left '21' unitless and unsourced."""
    [figure] = extract_figures("clustering around **21-23%**")
    assert (figure.kind, figure.value, figure.high, figure.is_percent) == ("range", 21, 23, True)


def test_percentage_points_are_a_percent_unit():
    """Regression: '0.01 percentage points' (19.50% vs 19.49%) was compared as 0.01."""
    [figure] = extract_figures("only 0.01 percentage points")
    assert figure.is_percent


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("over 14,000 bookings", [("14,000", "lower")]),
        ("6,500+ bookings", [("6,500+", "lower")]),
        ("under 10%", [("10%", "upper")]),
        ("approximately 800 sites", [("800", "approx")]),
        ("expanded to $700K-$800K per month", [("$700K-$800K", "range")]),
    ],
)
def test_qualifiers_set_the_kind(report, expected):
    assert kinds(report) == expected


def test_numbers_from_the_question_are_constraints_not_claims():
    [figure] = extract_figures("Only these are above 20%: Paris 22.7%")[:1]
    assert echoed_from_question(figure, "only the ones above 20%")


# ------------------------------------------------------------------ sourcing


def evidence(*tables: list[list]) -> Evidence:
    return Evidence.from_results([{"rows": rows} for rows in tables], limit=400)


def status(report: str, ev: Evidence) -> list[tuple[str, str]]:
    return [(c.figure.text, c.status) for c in (source_figure(f, ev) for f in extract_figures(report))]


def test_a_rate_matches_its_fraction_at_the_displayed_precision():
    ev = evidence([[0.19516912558474273, 22232, 4339]])
    assert status("19.5% (4,339 of 22,232)", ev) == [
        ("19.5%", "sourced"),
        ("4,339", "sourced"),
        ("22,232", "sourced"),
    ]


def test_a_figure_off_by_more_than_rounding_is_unsourced():
    ev = evidence([[0.19516912558474273]])
    assert status("19.7%", ev) == [("19.7%", "unsourced")]


def test_percent_change_between_two_periods_is_derived():
    ev = evidence([["2024-04-01", 971871.0], ["2025-04-01", 4755086.0]])
    [(text, result)] = status("an increase of 389.3%", ev)
    assert result == "derived"


def test_first_k_sum_supports_a_combined_lower_bound():
    """'The top three account for over 14,000 bookings combined'."""
    ev = evidence([["Paris", 5100], ["Phuket", 4800], ["Gold Coast", 4400], ["Mallorca", 4000]])
    assert status("over 14,000 bookings combined", ev) == [("14,000", "derived")]


def test_a_lower_bound_is_not_satisfied_by_an_unrelated_huge_number():
    ev = evidence([[12_000_000.0]])
    assert status("over 14,000 bookings", ev) == [("14,000", "unsourced")]


def test_a_range_is_sourced_when_a_value_falls_inside():
    ev = evidence([[0.206], [0.218], [0.251]])
    assert status("around 21-23%", ev) == [("21-23%", "sourced")]


def test_pairs_are_only_formed_within_a_row_or_a_column():
    """Cross-table coincidences once hid injected errors (recall 84% -> 90%)."""
    ev = evidence([[100.0, 7.0]], [[3.0]])
    # 100 / 50 = 2 would need a 50 nobody has; 7 - 3 = 4 crosses tables, so it is not derived.
    assert status("a gap of 4.0 points", ev) == [("4.0", "unsourced")]
    assert status("a gap of 93.0 points", ev) == [("93.0", "derived")]  # same row: 100 - 7


def test_a_share_of_the_column_total_is_derived():
    """Regression (real traffic): 'Europe accounted for approximately 41% of all bookings'."""
    ev = evidence(
        [["Europe", 9131], ["Asia", 8590], ["North America", 2046], ["Oceania", 2023], ["Africa", 442]]
    )
    assert status("Europe accounted for approximately 41%, Asia close behind at 39%", ev) == [
        ("41%", "derived"),
        ("39%", "derived"),
    ]
