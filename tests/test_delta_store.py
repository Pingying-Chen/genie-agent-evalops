"""Tests for evalops.services.delta_store: generated SQL, row serialization, batching."""

import json
import re
from datetime import UTC, datetime

from evalops.schemas.trace import AccessStatus, Step, StepKind, ToolName, Trace
from evalops.services.delta_store import (
    BATCH_ROWS,
    STEP_COLUMNS,
    STEP_RESULT_COLUMNS,
    TRACE_COLUMNS,
    DeltaWriter,
    Snapshot,
    ddl,
    merge_statement,
    step_row,
    trace_row,
)

NOW = datetime(2026, 10, 20, tzinfo=UTC)


def trace(i: int) -> Trace:
    return Trace(
        trace_id=f"t{i}",
        space_id="s",
        conversation_id="c",
        turn_index=0,
        access_status=AccessStatus.OK,
        parser_version="1",
        collected_at=NOW,
    )


def test_merge_statement_takes_one_json_parameter_and_upserts_on_the_key():
    statement = merge_statement("cat.sch.steps", STEP_COLUMNS, ["trace_id", "step_index"])

    assert "from_json(:rows, 'ARRAY<STRUCT<trace_id: STRING," in statement
    assert "ON t.trace_id = s.trace_id AND t.step_index = s.step_index" in statement
    assert "WHEN MATCHED THEN UPDATE SET *" in statement and "WHEN NOT MATCHED THEN INSERT *" in statement


def test_re_merging_a_step_without_its_result_keeps_the_stored_rows():
    """Genie stops serving old results; a re-collect must not overwrite the rows the checks use."""
    statement = merge_statement(
        "cat.sch.steps", STEP_COLUMNS, ["trace_id", "step_index"], keep=STEP_RESULT_COLUMNS
    )

    assert "result_rows = coalesce(s.result_rows, t.result_rows)" in statement
    assert "sql = s.sql," in statement  # everything else still takes the new value
    assert "UPDATE SET *" not in statement


def test_rows_have_exactly_the_table_columns():
    """from_json + `INSERT *` need the JSON keys to match the table columns one to one."""
    step = Step(
        trace_id="t1",
        step_index=0,
        call_id="c",
        tool_name=ToolName.EXECUTE_SQL,
        step_kind=StepKind.ANSWER,
        result_schema=[{"name": "n", "type_name": "INT"}],
        result_rows=[[1]],
    )

    assert set(trace_row(trace(1))) == {c for c, _ in TRACE_COLUMNS}
    row = step_row(step, "space-1")
    assert set(row) == {c for c, _ in STEP_COLUMNS}
    assert json.loads(row["result_rows"]) == [[1]]  # nested data is stored as JSON text


def test_snapshot_hash_ignores_key_order():
    a = Snapshot("s", "c", "items", NOW, {"x": 1, "y": 2}).row()
    b = Snapshot("s", "c", "items", NOW, {"y": 2, "x": 1}).row()

    assert a["payload_sha256"] == b["payload_sha256"]


def test_ddl_is_idempotent():
    assert all("IF NOT EXISTS" in s for s in ddl("workspace.genie_evalops"))


def test_table_comments_are_one_well_formed_string_literal():
    """Regression: "Reviewer's ..." ended the literal early and the review job failed with
    PARSE_SYNTAX_ERROR. Every quote inside a comment must be escaped."""
    literal = re.compile(r"COMMENT '(?:[^'\\]|\\.)*'$")
    tables = [s for s in ddl("workspace.genie_evalops") if s.startswith("CREATE TABLE")]
    assert tables and all(literal.search(s) for s in tables)


class RecordingSql:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        return [{"num_inserted_rows": len(json.loads(parameters["rows"])), "num_updated_rows": 0}]


async def test_large_merges_are_split_into_batches_and_counts_add_up():
    sql = RecordingSql()
    writer = DeltaWriter(sql, "cat.sch")

    counts = await writer.merge_traces([trace(i) for i in range(BATCH_ROWS + 5)])

    assert len(sql.calls) == 2
    assert counts.inserted == BATCH_ROWS + 5


class SchemaSql:
    """Pretends the traces table predates the genie_error column; records every statement."""

    def __init__(self):
        self.statements: list[str] = []

    async def execute(self, statement, parameters=None):
        self.statements.append(statement)
        if "information_schema.columns" in statement:
            columns = TRACE_COLUMNS if parameters["table"] == "traces" else []
            return [{"c": c} for c, _ in columns if c != "genie_error"] if columns else []
        return []


async def test_ensure_tables_adds_columns_an_older_table_lacks():
    sql = SchemaSql()

    await DeltaWriter(sql, "cat.sch").ensure_tables()

    alters = [s for s in sql.statements if s.startswith("ALTER TABLE")]
    assert "ALTER TABLE cat.sch.traces ADD COLUMNS (genie_error STRING)" in alters


def test_every_table_the_code_writes_is_created():
    # A table in TABLE_COLUMNS without DDL is only noticed in a deployed job ("table not found").
    from evalops.services.delta_store import TABLE_COLUMNS, ddl

    created = {s.split(".", 2)[2].split()[0] for s in ddl("c.s") if s.startswith("CREATE TABLE")}
    assert set(TABLE_COLUMNS) == created


async def test_a_field_with_no_column_is_refused_not_dropped():
    """MERGE silently ignores a source field the table lacks; a run's decision was lost that way."""
    import pytest

    writer = DeltaWriter(RecordingSql(), "cat.sch")
    await writer.merge_eval_run({"run_id": "r", "space_id": "s", "decision": '{"verdict": "accept"}'})
    with pytest.raises(ValueError, match="no such column"):
        await writer.merge_eval_run({"run_id": "r", "verdict": "accept"})
