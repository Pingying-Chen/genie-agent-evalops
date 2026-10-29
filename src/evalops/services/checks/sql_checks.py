"""Step-level SQL checks, on the parsed syntax tree (sqlglot), not on regex.

Why a parser: "is this AVG() over a ratio?" depends on aliases and subqueries
(`SELECT AVG(rate) FROM (SELECT MEASURE(`Cancellation Rate`) AS rate ...)`), which a regex
cannot follow without false alarms.
"""

import sqlglot
from sqlglot import exp


def parse(sql: str) -> exp.Expression | None:
    """Parse Databricks SQL; None if it does not parse (the check then reports `error`)."""
    try:
        return sqlglot.parse_one(sql, read="databricks")
    except sqlglot.errors.ParseError:
        return None


def _measure_name(node: exp.Expression) -> str | None:
    """`MEASURE(`Cancellation Rate`)` -> "Cancellation Rate"."""
    if isinstance(node, exp.Anonymous) and node.name.upper() == "MEASURE" and node.expressions:
        arg = node.expressions[0]
        return arg.name if isinstance(arg, exp.Column | exp.Identifier) else arg.sql("databricks").strip("`")
    return None


def avg_over_ratio(sql: str, ratio_measures: list[str]) -> list[str] | None:
    """Ratio measures that an AVG() averages, directly or through an alias. None = unparsable.

    Example hit: `SELECT AVG(rate) FROM (SELECT MEASURE(`Cancellation Rate`) AS rate ...)`.
    """
    tree = parse(sql)
    if tree is None:
        return None
    ratios = {m.lower() for m in ratio_measures}
    aliases: dict[str, str] = {}
    for alias in tree.find_all(exp.Alias):
        name = _measure_name(alias.this)
        if name and name.lower() in ratios:
            aliases[alias.alias.lower()] = name
    hits: list[str] = []
    for avg in tree.find_all(exp.Avg):
        for node in avg.walk():
            name = _measure_name(node)
            if name and name.lower() in ratios:
                hits.append(name)
            elif isinstance(node, exp.Column) and node.name.lower() in aliases:
                hits.append(aliases[node.name.lower()])
    return sorted(set(hits))


def normalize(sql: str) -> str:
    """Canonical text for comparing two statements (formatting and case differences removed)."""
    tree = parse(sql)
    text = tree.sql("databricks", normalize=True, pretty=False) if tree is not None else sql
    return " ".join(text.lower().split())


def column_names(schema: list[dict] | None) -> list[str] | None:
    """A step's result columns, from its stored result schema; None when it has none."""
    return None if schema is None else [str(c.get("name", "")) for c in schema]


def _selects_star(select: exp.Select) -> bool:
    """`SELECT *` or `SELECT g.*` (not `COUNT(*)`)."""
    return any(
        isinstance(p, exp.Star) or (isinstance(p, exp.Column) and isinstance(p.this, exp.Star))
        for p in select.expressions
    )


def restricted_references(
    sql: str, tables: list[str], columns: list[str], result_columns: list[str] | None = None
) -> tuple[list[str], bool] | None:
    """Restricted tables and columns a statement reads (names compared by their last part, with
    or without a table alias), and whether that is certain. None = unparsable.

    `SELECT *` reads columns it does not name. In the outer query its result columns say which
    (`result_columns`, when the step kept them); in a subquery, or with no result, it is uncertain.
    """
    tree = parse(sql)
    if tree is None:
        return None
    restricted_tables = {t.split(".")[-1].lower() for t in tables}
    restricted_columns = {c.split(".")[-1].lower() for c in columns}
    hits = {f"table:{t.name}" for t in tree.find_all(exp.Table) if t.name.lower() in restricted_tables}
    hits |= {f"column:{c.name}" for c in tree.find_all(exp.Column) if c.name.lower() in restricted_columns}
    certain = True
    stars = [s for s in tree.find_all(exp.Select) if _selects_star(s)]
    if stars:
        if result_columns is not None and all(s is tree for s in stars):
            hits |= {f"column:{c}" for c in result_columns if c.lower() in restricted_columns}
        else:
            certain = False
    return sorted(hits), certain
