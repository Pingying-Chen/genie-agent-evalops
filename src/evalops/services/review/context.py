"""What a reviewer needs besides the turn itself: when it was asked, and what the space knows.

Rules like "refuse what is out of scope" can only be judged knowing the data boundary (which
tables were deliberately left out), and relative periods ("this year") only knowing when the
question was asked. Both go at the top of every review trace.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import httpx
import yaml

from evalops.clients.auth import Auth
from evalops.clients.http import request_json
from evalops.clients.sql import SqlClient
from evalops.schemas.genie import SpaceDefinition


@dataclass(frozen=True)
class TableInfo:
    name: str
    comment: str | None = None
    measures: list[tuple[str, str | None]] = field(default_factory=list)  # (name, comment)
    dimensions: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)  # when measures and dimensions are not known apart


def table_info(name: str, describe_json: str) -> TableInfo:
    """From `DESCRIBE TABLE EXTENDED <t> AS JSON` (the tables API drops metric-view comments)."""
    d = json.loads(describe_json)
    columns = d.get("columns", [])
    return TableInfo(
        name=name,
        comment=d.get("comment"),
        measures=[(c["name"], c.get("comment")) for c in columns if c.get("is_measure")],
        dimensions=[c["name"] for c in columns if not c.get("is_measure")],
    )


def table_info_from_uc(name: str, body: dict) -> TableInfo:
    """From the Unity Catalog tables API. It drops a metric view's column comments, but keeps
    the view comment and the YAML definition, which names the measures and their comments."""
    definition = body.get("view_definition") or ""
    spec = yaml.safe_load(definition) if "measures:" in definition else None
    if isinstance(spec, dict):
        return TableInfo(
            name=name,
            comment=body.get("comment"),
            measures=[(m["name"], m.get("comment")) for m in spec.get("measures", [])],
            dimensions=[d["name"] for d in spec.get("dimensions", [])],
        )
    # BROWSE alone returns no view definition: list the columns without claiming which are measures.
    return TableInfo(
        name=name, comment=body.get("comment"), columns=[c["name"] for c in body.get("columns", [])]
    )


async def describe_tables(
    sql: SqlClient, http: httpx.AsyncClient, auth: Auth, names: list[str]
) -> list[TableInfo]:
    """Metadata only. `DESCRIBE` needs SELECT; the tables API needs only BROWSE, so it is the
    fallback (the review job should not be able to read the data it describes)."""
    out = []
    for name in names:
        try:
            rows = await sql.execute(f"DESCRIBE TABLE EXTENDED {name} AS JSON")
            out.append(table_info(name, next(iter(rows[0].values()))))
            continue
        except Exception:
            pass
        try:
            body = await request_json(
                http,
                auth,
                "GET",
                f"/api/2.1/unity-catalog/tables/{name}",
                service="unity-catalog",
                params={"include_browse": "true"},
            )
            out.append(table_info_from_uc(name, body))
        except Exception:
            out.append(TableInfo(name=name, comment="(metadata not readable by the review job)"))
    return out


def space_context(space: SpaceDefinition, tables: list[TableInfo]) -> str:
    lines = [f"Genie space: {space.title or '?'}"]
    if space.description:
        lines.append(space.description.strip())
    for t in tables:
        lines += ["", f"Data source: {t.name}"]
        if t.comment:
            lines.append(t.comment.strip())
        if t.measures:
            lines.append("Measures: " + "; ".join(f"{m} ({c})" if c else m for m, c in t.measures))
        if t.dimensions:
            lines.append("Dimensions: " + ", ".join(t.dimensions))
        if t.columns:
            lines.append("Columns: " + ", ".join(t.columns))
    if space.rules:
        lines += ["", "Space rules:", *space.rules]
    return "\n".join(lines)


def asked_line(asked_at: datetime | None, timezone: str) -> str:
    """`Asked 2026-10-19 16:10 MST (23:10 UTC)`: relative periods are judged from this moment."""
    if asked_at is None:
        return "Asked: unknown"
    at = asked_at if asked_at.tzinfo else asked_at.replace(tzinfo=UTC)
    local = at.astimezone(ZoneInfo(timezone))
    utc = at.astimezone(UTC)
    text = f"Asked {local:%Y-%m-%d %H:%M} {local.tzname()}"
    return text if timezone == "UTC" else f"{text} ({utc:%Y-%m-%d %H:%M} UTC)"
