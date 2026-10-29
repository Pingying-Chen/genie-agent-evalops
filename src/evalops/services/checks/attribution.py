"""Figure attribution, behind `report.figure_attributed`.

`report.figure_sourced` asks whether a figure appears in the results; this asks whether the
report says it of the right thing. "Asia had $4.2M" passes the first when $4.2M is Europe's
value, because the number is there. Here a figure that matches a result cell is checked against
that cell's row and column:

- the row's labels: its text cells ("Europe", "confirmed") and its period cells ("2024-06" also
  reads as "June 2024"; a numeric year or month column counts as a period);
- the column's name ("Realized Revenue", "total_revenue", also without "total", "avg" ...).

How reports are written decides where to look. A row label governs the figures after it on its
line until the next label: "Thailand ($1.78M, 9,209 bookings) and Australia ($1.46M, ...)". A
label right after a figure ("$4.2M for Europe", "$4.2M (Europe)") governs it instead. A column
name counts only next to the figure ("9,209 bookings", "revenue was $4.2M"), never elsewhere in
the sentence, where it is usually about another figure.

Naming one of the figure's own labels is consistent, naming only another row's labels or another
column's name is misattributed, naming neither says nothing. Precision comes first: a figure that
matches cells in several rows owns all their labels.

Pure functions; no I/O.
"""

import calendar
import re
from dataclasses import dataclass
from typing import Any, Literal

from evalops.services.checks.figures import Figure, _is_number

Status = Literal["consistent", "misattributed", "unclear"]

_PERIOD_COLUMN = re.compile(r"year|month|quarter|week|date|day|period", re.IGNORECASE)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")
_NOT_LABELS = {"null", "none", "other", "others", "unknown", "n/a", "na", "total", "all"}


@dataclass(frozen=True)
class Table:
    """One step's result: column names and rows, as the step kept them."""

    columns: list[str]
    rows: list[list[Any]]


@dataclass(frozen=True)
class Attributed:
    figure: str
    status: Status
    said: list[str]  # the labels its context names
    belongs_to: list[str]  # the labels of the cells it matches


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[_*`|]", " ", str(text)).lower().split())


def period_labels(value: Any, column: str = "") -> set[str]:
    """Ways a report may write a period value: 2024-06-01 -> "june 2024", "jun 2024", "2024-06"."""
    if _is_number(value):
        n = int(value)
        if float(value) != n:
            return set()
        name = column.lower()
        if "month" in name and 1 <= n <= 12:
            return {calendar.month_name[n].lower(), calendar.month_abbr[n].lower()}
        if "quarter" in name and 1 <= n <= 4:
            return {f"q{n}"}
        return {str(n)}
    m = _ISO_DATE.match(str(value))
    if not m:
        return set()
    year, month = m.group(1), int(m.group(2))
    if not 1 <= month <= 12:
        return set()
    full, short = calendar.month_name[month].lower(), calendar.month_abbr[month].lower()
    return {f"{full} {year}", f"{short} {year}", f"{year}-{m.group(2)}"}


def _label_columns(table: Table) -> list[int]:
    """Columns that name a row: text, dates, and numeric periods (a year or month column)."""
    out = []
    for i, name in enumerate(table.columns):
        values = [r[i] for r in table.rows if i < len(r) and r[i] is not None]
        if not values:
            continue
        if all(_is_number(v) for v in values):
            if _PERIOD_COLUMN.search(name):
                out.append(i)
        else:
            out.append(i)
    return out


def row_labels(table: Table, row: list[Any], label_columns: list[int]) -> set[str]:
    labels: set[str] = set()
    for i in label_columns:
        if i >= len(row) or row[i] is None:
            continue
        periods = period_labels(row[i], table.columns[i] if i < len(table.columns) else "")
        if periods:
            labels |= periods
            continue
        text = _norm(row[i])
        if 2 <= len(text) <= 40 and text not in _NOT_LABELS:
            labels.add(text)
    return labels


_AGGREGATE = re.compile(r"^(?:total|sum of|sum|avg|average|mean|count of|count|number of|num|n)\s+")
_LINK_WORDS = r"\s*(?:for|in|at|from|of|:|\(|-|–|—)?\s*"
# "revenue was $4.2M", "revenue: $4.2M": a column name right before a figure, joined by at most this.
_LEADS_INTO = r"\s*(?:was|were|is|are|of|at|totaled|totalled|reached|=|:)?\s*$"
_SENTENCE_END = re.compile(r"[.!?]\s+(?=\W*[A-Z0-9])")
NEAR = 30  # characters after a figure where its column name is read ("9,209 bookings")
JUDGED_KINDS = ("exact", "approx")  # a range or a bound ("over $1.69M") is about a group, not one row


def _named(labels: set[str], text: str) -> list[tuple[int, str]]:
    """Each label the text names, with where (labels inside longer words do not count)."""
    found = []
    for label in labels:
        for m in re.finditer(rf"(?<![\w-]){re.escape(label)}(?![\w-])", text):
            found.append((m.start(), label))
    return sorted(found)


def column_names(name: str) -> set[str]:
    """`total_bookings` -> {"total bookings", "bookings"}."""
    full = _norm(name)
    short = _AGGREGATE.sub("", full)
    return {n for n in (full, short) if len(n) >= 3}


@dataclass
class _Cells:
    """What one figure matches across the trace's tables."""

    own_rows: set[str]
    other_rows: set[str]
    own_columns: set[str]
    other_columns: set[str]


def _cells(figure: Figure, tables: list[Table]) -> _Cells | None:
    found = _Cells(set(), set(), set(), set())
    matched = False
    for table in tables:
        labels = _label_columns(table)
        value_columns = [i for i in range(len(table.columns)) if i not in labels]
        hit_rows, hit_columns = set(), set()
        for r, row in enumerate(table.rows):
            for i in value_columns:
                if i < len(row) and _is_number(row[i]) and figure.accepts(float(row[i])):
                    hit_rows.add(r)
                    hit_columns.add(i)
        if not hit_rows:
            continue
        matched = True
        for r, row in enumerate(table.rows):
            (found.own_rows if r in hit_rows else found.other_rows).update(row_labels(table, row, labels))
        for i in value_columns:
            names = column_names(table.columns[i]) if i < len(table.columns) else set()
            (found.own_columns if i in hit_columns else found.other_columns).update(names)
    if not matched:
        return None
    found.other_rows -= found.own_rows
    # "difference" next to "63.7%" names the figure's own `difference_pct` too: not another column.
    found.other_columns = {
        name
        for name in found.other_columns - found.own_columns
        if not any(re.search(rf"(?<!\w){re.escape(name)}(?!\w)", own) for own in found.own_columns)
    }
    return found


def _verdict(named: set[str], own: set[str], other: set[str]) -> Status | None:
    if named & own:
        return "consistent"
    if named & other:
        return "misattributed"
    return None


def attribute_all(figures: list[Figure], report: str, tables: list[Table]) -> list[Attributed]:
    """Every figure that matches a result cell, judged against its row and column."""
    text = _norm_keep_length(report)
    every_label = {label for t in tables for row in t.rows for label in row_labels(t, row, _label_columns(t))}
    out = []
    governing: set[str] = set()
    line_of_previous, end_of_previous = -1, 0
    for figure in sorted((f for f in figures if f.start >= 0), key=lambda f: f.start):
        line_start = text.rfind("\n", 0, figure.start) + 1
        if line_start != line_of_previous:  # a new line: labels do not carry over
            governing, end_of_previous, line_of_previous = set(), line_start, line_start
        ends = [m.end() for m in _SENTENCE_END.finditer(report, end_of_previous, figure.start)]
        if ends:  # nor into the next sentence
            governing, end_of_previous = set(), ends[-1]
        cells = _cells(figure, tables)
        line_end = text.find("\n", figure.end)
        after = text[figure.end : line_end if line_end != -1 else len(text)]
        right_after = {
            label for at, label in _named(every_label, after) if re.fullmatch(_LINK_WORDS, after[:at])
        }
        between = {label for _, label in _named(every_label, text[end_of_previous : figure.start])}
        if between:
            governing = between
        named_rows = right_after or governing
        end_of_previous = figure.end
        if cells is None or figure.kind not in JUDGED_KINDS:
            continue
        before = text[line_start : figure.start]
        near_after = re.split(r"\d", after[:NEAR])[0]
        columns = cells.own_columns | cells.other_columns
        named_columns = {label for _, label in _named(columns, near_after)}
        named_columns |= {
            name for name in columns if re.search(rf"(?<![\w-]){re.escape(name)}{_LEADS_INTO}", before)
        }
        row_status = _verdict(named_rows, cells.own_rows, cells.other_rows)
        column_status = _verdict(named_columns, cells.own_columns, cells.other_columns)
        statuses = {row_status, column_status}
        status: Status = (
            "misattributed"
            if "misattributed" in statuses
            else "consistent"
            if "consistent" in statuses
            else "unclear"
        )
        out.append(
            Attributed(
                figure=figure.text,
                status=status,
                said=sorted((named_rows & (cells.own_rows | cells.other_rows)) | named_columns),
                belongs_to=sorted(cells.own_rows | cells.own_columns),
            )
        )
    return out


def _norm_keep_length(text: str) -> str:
    """Lower case, with markdown marks and underscores as spaces: offsets stay those of `text`."""
    return re.sub(r"[_*`|]", " ", text).lower()
