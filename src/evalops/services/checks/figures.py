"""Figure extraction and sourcing: the hallucination guardrail behind `report.figure_sourced`.

Pipeline for one report:
1. `extract_figures`: find numeric claims in the text, after removing citations, links, code
   and dates (including "July 30, 2025"). Each claim has a kind:
   - `exact`   "19.5%", "9,678", "$4.76M"
   - `approx`  "approximately 800", "about $1.2M", "~20%"
   - `lower`   "over 14,000", "more than 5", "6,500+"
   - `upper`   "under 10%", "less than 100"
   - `range`   "21-23%", "$700K-$800K", "between 5 and 10"
   Years, list ordinals (Q2, P3, Top 5, #1) and small bare counts (<= 12) are skipped.
2. Skip figures echoed from the question ("above 20%", "Top 5", "in 2024").
3. `source_figure`: compare with the trace's values, at the figure's displayed precision:
   - direct values: every numeric result cell -> `sourced`
   - aggregates: column totals, first-k sums of a column ("the top three account for ..."),
     a cell's share of its column total ("41% of all bookings"), row counts ("20 destinations")
     -> `derived`
   - pairwise: difference, ratio, percent change of two cells **in the same column or the same
     row** -> `derived`. Pairing any two cells (all permutations) matched altered figures by
     coincidence: injected-error recall was 84% that way, see the notes.
   Otherwise -> `unsourced`.

Bounds are deliberately tight (a value within 25% of the bound) so "over 14,000" is not
satisfied by any unrelated large number.

Pure functions; no I/O. Precision was measured on the demo's 23 traces (see notes).
"""

import math
import re
from dataclasses import dataclass, replace
from itertools import permutations
from typing import Any, Literal

Kind = Literal["exact", "approx", "lower", "upper", "range"]

_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_LINK = re.compile(r"\\?\[\\?\[?\d+\]?\([^)]*\)\\?\]?|\[\d+\]")  # [1](url), \[[1](url)\], [1]
_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"```.*?```|`[^`]*`", re.DOTALL)
_DATE = re.compile(
    rf"\b\d{{4}}-\d{{2}}(-\d{{2}})?(T[\d:.]+Z?)?\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b"
    rf"|\b{_MONTHS}\s+\d{{1,2}}(st|nd|rd|th)?\b(,)?|\b\d{{1,2}}(st|nd|rd|th)?\s+(of\s+)?{_MONTHS}",
    re.IGNORECASE,
)
_NUM = r"(?P<{p}sign>[-−+])?(?P<{p}cur>[$€£])?(?P<{p}num>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_SUFFIX = r"(?:\s?(?P<{p}suffix>%|percent(?:age points?)?\b|percentage points?\b|pp\b|pts?\b|[KMB]\b|thousand\b|million\b|billion\b))?"
_FIGURE = re.compile(
    r"(?<![\w.])"
    + _NUM.format(p="a")
    + _SUFFIX.format(p="a")
    + r"(?:\s?(?P<sep>-|–|to|and)\s?"
    + _NUM.format(p="b")
    + _SUFFIX.format(p="b")
    + r")?(?P<plus>\+)?",
    re.IGNORECASE,
)
# A label right before the number: Q2, P3, FY2025, Top 5, #1 (but not a markdown heading "## 9,678").
_PREFIX_LABEL = re.compile(r"(?:\b[QPRSHW]|\bFY)$|\bTop\s$|(?<!#)#$", re.IGNORECASE)
_APPROX = re.compile(
    r"(approximately|approx\.?|about|around|roughly|nearly|almost|~|≈|close to)\s*$", re.IGNORECASE
)
_LOWER = re.compile(r"(over|more than|above|at least|exceeding|upwards of|>)\s*$", re.IGNORECASE)
_UPPER = re.compile(r"(under|less than|below|at most|fewer than|<)\s*$", re.IGNORECASE)
_BETWEEN = re.compile(r"between\s*$", re.IGNORECASE)
_RANGE_CONTEXT = re.compile(r"\b(rang(e|es|ing)|between|span(s|ning)?)\b[^.]*$", re.IGNORECASE)
_MULTIPLIER = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
_PERCENT = re.compile(r"^(%|percent|percentage points?|pp|pts?)$", re.IGNORECASE)


@dataclass(frozen=True)
class Figure:
    text: str  # as written, e.g. "19.5%"
    value: float  # low end, in the unit written (19.5 for "19.5%")
    decimals: int  # displayed precision
    is_percent: bool
    multiplier: float  # 1, 1e3, 1e6, 1e9
    kind: Kind = "exact"
    high: float | None = None  # high end for ranges
    start: int = -1  # where it is written in the report (same offsets as the original text)
    end: int = -1

    @property
    def tolerance(self) -> float:
        """Half a unit in the last displayed place, in absolute terms (plus float slack)."""
        return 0.5 * 10 ** (-self.decimals) * self.multiplier + 1e-9

    def scales(self) -> list[float]:
        """19.5% may be stored as 19.5 or as 0.195."""
        return [1.0, 0.01] if self.is_percent else [1.0]

    def accepts(self, value: float) -> bool:
        """Does a value from the data support this claim?"""
        if not math.isfinite(value):
            return False
        for scale in self.scales():
            low = self.value * self.multiplier * scale
            tol = self.tolerance * scale
            if self.kind == "exact" and abs(value - low) <= tol:
                return True
            if self.kind == "approx" and abs(value - low) <= max(tol, 0.05 * abs(low)):
                return True
            if self.kind == "lower" and low - tol <= value <= low * 1.25 + tol:
                return True
            if self.kind == "upper" and low * 0.75 - tol <= value <= low + tol:
                return True
            if self.kind == "range" and self.high is not None:
                high = self.high * self.multiplier * scale
                if min(low, high) - tol <= value <= max(low, high) + tol:
                    return True
        return False


@dataclass(frozen=True)
class SourcedFigure:
    figure: Figure
    status: Literal["sourced", "derived", "unsourced"]
    how: str | None = None  # "cell", "column_total", "first_k_sum", "row_count", "difference", ...


def clean_report(text: str) -> str:
    """Blank out citations, links, URLs, code and dates: their numbers are not claims. Same length
    as the input, so a figure's offsets are offsets in the report as written."""
    for pattern in (_CODE, _LINK, _URL, _DATE):
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _parts(match: re.Match, p: str) -> tuple[float, int, str, bool, str] | None:
    raw = match.group(f"{p}num")
    if raw is None:
        return None
    sign = -1.0 if match.group(f"{p}sign") in ("-", "−") else 1.0
    decimals = len(raw.split(".")[1]) if "." in raw else 0
    return (
        sign * float(raw.replace(",", "")),
        decimals,
        (match.group(f"{p}suffix") or "").lower(),
        bool(match.group(f"{p}cur")),
        raw,
    )


def extract_figures(text: str, *, keep_small: bool = False) -> list[Figure]:
    """Numeric claims in a report, in order of appearance. Years and small bare counts are left
    out, since they are rarely data, unless `keep_small` (a check that knows the value it expects).

    "A-B" is a range; "A to B" / "A and B" only after "range/ranging/between". Otherwise
    "rose from $971.9K to $4.76M" is two separate claims, each checked on its own.
    """
    figures: list[Figure] = []
    cleaned = clean_report(text or "")
    for match in _FIGURE.finditer(cleaned):
        a = _parts(match, "a")
        b = _parts(match, "b")
        before = cleaned[max(0, match.start() - 40) : match.start()]
        if _PREFIX_LABEL.search(before[-4:]):
            continue
        sep = (match.group("sep") or "").lower()
        is_range = b is not None and (sep in ("-", "–") or bool(_RANGE_CONTEXT.search(before)))
        if b is not None and not is_range:
            text_a, text_b = _span(match, "a"), _span(match, "b") + (match.group("plus") or "")
            first = _single(a, before, plus=False, text=text_a, keep_small=keep_small)
            second = _single(b, "", plus=bool(match.group("plus")), text=text_b, keep_small=keep_small)
            figures.extend(
                replace(f, start=_start(match, side), end=_end(match, side))
                for f, side in ((first, "a"), (second, "b"))
                if f
            )
            continue
        if is_range:
            figure = _range(a, b, match.group(0).strip())
        else:
            figure = _single(
                a, before, plus=bool(match.group("plus")), text=match.group(0).strip(), keep_small=keep_small
            )
        if figure:
            figures.append(replace(figure, start=match.start(), end=match.end()))
    return figures


def _start(match: re.Match, p: str) -> int:
    return min(match.start(g) for g in (f"{p}sign", f"{p}cur", f"{p}num") if match.start(g) != -1)


def _end(match: re.Match, p: str) -> int:
    return max(match.end(g) for g in (f"{p}num", f"{p}suffix") if match.end(g) != -1)


def _span(match: re.Match, p: str) -> str:
    """The original text of one side ("a" or "b") of a match."""
    return match.string[_start(match, p) : _end(match, p)]


def _single(part, before: str, *, plus: bool, text: str, keep_small: bool = False) -> Figure | None:
    value, decimals, suffix, currency, raw = part
    if not keep_small and not (currency or suffix) and decimals == 0 and "," not in raw:
        if 1900 <= abs(value) <= 2100 or abs(value) <= 12:
            return None  # a year, or a small count / ordinal
    kind: Kind = "exact"
    if plus or _LOWER.search(before):
        kind = "lower"
    elif _UPPER.search(before):
        kind = "upper"
    elif _APPROX.search(before):
        kind = "approx"
    shown = text or (
        ("$" if currency else "") + raw + (suffix if suffix in ("%",) else (" " + suffix if suffix else ""))
    )
    return Figure(
        text=shown,
        value=value,
        decimals=decimals,
        is_percent=bool(_PERCENT.match(suffix)) if suffix else False,
        multiplier=_MULTIPLIER.get(suffix, 1.0),
        kind=kind,
    )


def _range(a, b, text: str) -> Figure | None:
    value, decimals, suffix, currency, _ = a
    suffix = suffix or b[2]
    currency = currency or b[3]
    if not (currency or suffix) and all(1900 <= abs(v) <= 2100 for v in (value, b[0])):
        return None  # "2023-2024"
    return Figure(
        text=text,
        value=value,
        decimals=max(decimals, b[1]),
        is_percent=bool(_PERCENT.match(suffix)) if suffix else False,
        multiplier=_MULTIPLIER.get(suffix, 1.0),
        kind="range",
        high=b[0],
    )


def echoed_from_question(figure: Figure, question: str | None) -> bool:
    """True when the same number appears in the question (a constraint, not a claim)."""
    if not question:
        return False
    numbers = {float(m.group("anum").replace(",", "")) for m in _FIGURE.finditer(question)}
    return abs(figure.value) in numbers


@dataclass
class Evidence:
    """All values a trace's results can support: direct cells and aggregates."""

    cells: list[float]
    aggregates: list[tuple[float, str]]  # (value, how)
    pairs: list[tuple[float, float]]  # structurally related cells: same column, or same row

    @classmethod
    def from_results(cls, results: list[dict[str, Any]], *, limit: int) -> "Evidence":
        cells: list[float] = []
        aggregates: list[tuple[float, str]] = []
        pairs: list[tuple[float, float]] = []
        for result in results:
            rows = result.get("rows") or []
            aggregates.append((float(len(rows)), "row_count"))
            width = max((len(r) for r in rows), default=0)
            numeric_rows = [[float(v) for v in r if _is_number(v)] for r in rows[:50]]
            for row in numeric_rows:  # same row: e.g. GBV - revenue, cancelled / total
                pairs.extend(permutations(row, 2))
            for col in range(width):
                column = [float(r[col]) for r in rows if col < len(r) and _is_number(r[col])]
                cells.extend(column)
                pairs.extend(permutations(column[:50], 2))  # same column: period vs period
                if len(column) > 1:
                    total = sum(column)
                    aggregates.append((total, "column_total"))
                    if total:  # "Europe accounted for 41% of all bookings": a cell's share of its column
                        aggregates.extend((v / total, "share_of_total") for v in column[:50])
                    running = 0.0
                    for k, v in enumerate(column[:10], start=1):
                        running += v
                        if k > 1:
                            aggregates.append((running, "first_k_sum"))
        return cls(cells=cells[:limit], aggregates=aggregates, pairs=pairs)


def numeric_cells(results: list[dict[str, Any]], *, limit: int) -> Evidence:
    """Backward-compatible name: build the evidence for a trace's step results."""
    return Evidence.from_results(results, limit=limit)


def source_figure(figure: Figure, evidence: Evidence) -> SourcedFigure:
    """Classify one figure against the trace's values."""
    if any(figure.accepts(c) for c in evidence.cells):
        return SourcedFigure(figure, "sourced", "cell")
    for value, how in evidence.aggregates:
        if figure.accepts(value):
            return SourcedFigure(figure, "derived", how)
    for a, b in evidence.pairs:
        if figure.accepts(a - b):
            return SourcedFigure(figure, "derived", "difference")
        if b and figure.accepts(a / b):
            return SourcedFigure(figure, "derived", "ratio")
        if b and figure.accepts((a - b) / b):
            return SourcedFigure(figure, "derived", "pct_change")
    return SourcedFigure(figure, "unsourced")


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value))
