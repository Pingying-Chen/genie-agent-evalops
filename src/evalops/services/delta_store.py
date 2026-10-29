"""Delta tables for captured data: DDL and idempotent batch writes.

Writes send a batch as ONE JSON string parameter and expand it in SQL:

    MERGE INTO <t> USING (SELECT inline(from_json(:rows, 'ARRAY<STRUCT<...>>'))) s ON <key> ...

so no value is ever formatted into SQL text, and a batch of 200 rows is one statement
instead of 200. `MERGE` makes reruns (and the collector's lookback window) idempotent.
"""

import asyncio
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from evalops.clients.sql import SqlClient
from evalops.schemas.checks import CheckResult, CheckRun
from evalops.schemas.evaluation import GradedAttempt
from evalops.schemas.review import Agreement, CategorySuggestion, HumanLabel, ReviewItem, TraceReview
from evalops.schemas.trace import Step, Trace

BATCH_ROWS = 200
MAX_RESULT_ROWS = 200

TRACE_COLUMNS: list[tuple[str, str]] = [
    ("trace_id", "STRING"),
    ("space_id", "STRING"),
    ("conversation_id", "STRING"),
    ("turn_index", "INT"),
    ("user_id", "STRING"),
    ("asked_at", "TIMESTAMP"),
    ("completed_at", "TIMESTAMP"),
    ("question", "STRING"),
    ("report_text", "STRING"),
    ("genie_error", "STRING"),
    ("n_sql_steps", "INT"),
    ("n_tool_calls", "INT"),
    ("access_status", "STRING"),
    ("source", "STRING"),
    ("parser_version", "STRING"),
    ("collected_at", "TIMESTAMP"),
]
STEP_COLUMNS: list[tuple[str, str]] = [
    ("trace_id", "STRING"),
    ("space_id", "STRING"),
    ("step_index", "INT"),
    ("call_id", "STRING"),
    ("tool_name", "STRING"),
    ("step_kind", "STRING"),
    ("title", "STRING"),
    ("sql", "STRING"),
    ("statement_id", "STRING"),
    ("status", "STRING"),
    ("row_count", "BIGINT"),
    ("result_schema", "STRING"),  # JSON text: [{name, type_name}]
    ("result_rows", "STRING"),  # JSON text: first MAX_RESULT_ROWS rows, typed
    ("error", "STRING"),
    ("source_call_id", "STRING"),
]
SNAPSHOT_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("conversation_id", "STRING"),
    ("endpoint", "STRING"),
    ("fetched_at", "TIMESTAMP"),
    ("payload", "STRING"),
    ("payload_sha256", "STRING"),
]


CHECK_RUN_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "STRING"),
    ("space_id", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("criteria_version", "STRING"),
    ("n_traces", "INT"),
    ("status", "STRING"),
    ("deferred", "STRING"),  # JSON text: trace ids whose judging was deferred to the next run
]
CHECK_RESULT_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "STRING"),
    ("trace_id", "STRING"),
    ("step_index", "INT"),
    ("criterion_id", "STRING"),
    ("criterion_version", "INT"),
    ("family", "STRING"),
    ("layer", "STRING"),
    ("gate", "STRING"),
    ("verdict", "STRING"),
    ("evidence", "STRING"),  # JSON text
    ("checked_at", "TIMESTAMP"),
]
# The latest verdict per (trace, rule, step) (v1 scaling): replaced when a trace is
# re-checked, so pages read current state without every run covering every trace.
CURRENT_RESULT_COLUMNS: list[tuple[str, str]] = [("space_id", "STRING"), *CHECK_RESULT_COLUMNS]
REVIEW_ITEM_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("trace_id", "STRING"),
    ("mlflow_trace_id", "STRING"),
    ("session_id", "STRING"),
    ("reason", "STRING"),
    ("queued_at", "TIMESTAMP"),
]
HUMAN_LABEL_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("trace_id", "STRING"),
    ("mlflow_trace_id", "STRING"),
    ("criterion_id", "STRING"),
    ("criterion_version", "INT"),
    ("reviewer", "STRING"),
    ("label", "STRING"),
    ("rationale", "STRING"),
    ("assessment_id", "STRING"),
    ("labeled_at", "TIMESTAMP"),
]
TRACE_REVIEW_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("trace_id", "STRING"),
    ("mlflow_trace_id", "STRING"),
    ("reviewer", "STRING"),
    ("task_success", "STRING"),
    ("task_rationale", "STRING"),
    ("findings", "STRING"),
    ("updated_at", "TIMESTAMP"),
    ("guideline", "STRING"),  # e.g. task_success__v3; added last, as ALTER TABLE appends
]
SUGGESTION_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("trace_id", "STRING"),
    ("category", "STRING"),
    ("is_new", "BOOLEAN"),
    ("description", "STRING"),
    ("reason", "STRING"),
    ("input_hash", "STRING"),
    ("skill", "STRING"),
    ("model", "STRING"),
    ("created_at", "TIMESTAMP"),
]
SPACE_VERSION_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("version", "STRING"),  # content hash of serialized_space
    ("captured_at", "TIMESTAMP"),
    ("previous_version", "STRING"),
    ("title", "STRING"),
    ("description", "STRING"),
    ("warehouse_id", "STRING"),
    ("serialized_space", "STRING"),
    ("counts", "STRING"),  # JSON text: instructions, sql_examples, snippets, tables, column_configs
]
INTENT_COLUMNS: list[tuple[str, str]] = [
    ("space_id", "STRING"),
    ("kind", "STRING"),  # trace | case
    ("ref_id", "STRING"),  # trace_id, or case_id@version
    ("question", "STRING"),
    ("question_hash", "STRING"),
    ("intent", "STRING"),
    ("skill", "STRING"),
    ("model", "STRING"),
    ("tagged_at", "TIMESTAMP"),
]
EVAL_RUN_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "STRING"),
    ("space_id", "STRING"),
    ("label", "STRING"),
    ("change_id", "STRING"),
    ("k", "INT"),
    ("arms", "STRING"),  # JSON text: [{arm, config_version, change}]
    ("cases", "STRING"),  # JSON text: [{case_id, version, split}]
    ("status", "STRING"),  # running | succeeded | partial | failed
    ("tester", "STRING"),
    ("criteria_version", "STRING"),
    ("preflight", "STRING"),  # JSON text
    ("error", "STRING"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("n_results", "INT"),
    ("split", "STRING"),  # dev | test | all; added last, as ALTER TABLE appends
    ("scope", "STRING"),  # the part a dev run asked, e.g. "type = guardrail"; null = all of the split
    ("decision", "STRING"),  # JSON Comparison, fixed when the run ends; null = no change tested
]
EVAL_RESULT_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "STRING"),
    ("space_id", "STRING"),
    ("arm", "STRING"),
    ("case_id", "STRING"),
    ("case_version", "INT"),
    ("attempt", "INT"),
    ("verdict", "STRING"),
    ("hard_fails", "STRING"),  # JSON text
    ("expectations", "STRING"),  # JSON text
    ("trace_id", "STRING"),
    ("conversation_id", "STRING"),
    ("question", "STRING"),
    ("report_text", "STRING"),
    ("seconds", "DOUBLE"),
    ("error", "STRING"),
    ("graded_at", "TIMESTAMP"),
    ("failed_by", "STRING"),  # JSON text: evaluator ids; added last, as ALTER appends
    ("steps", "STRING"),  # JSON text: the graded turn's steps, for reviewing expectation verdicts
]
# Version manifests: texts stored once by content hash, one manifest per run and space.
ARTIFACT_COLUMNS: list[tuple[str, str]] = [
    ("hash", "STRING"),
    ("kind", "STRING"),  # criterion | skill | guideline
    ("name", "STRING"),
    ("version", "STRING"),
    ("content", "STRING"),
    ("first_seen_at", "TIMESTAMP"),
]
MANIFEST_COLUMNS: list[tuple[str, str]] = [
    ("run_id", "STRING"),
    ("space_id", "STRING"),
    ("run_kind", "STRING"),  # check | eval | review
    ("created_at", "TIMESTAMP"),
    ("label", "STRING"),
    ("manifest", "STRING"),  # JSON text: schemas.manifest.Manifest
]
AGREEMENT_COLUMNS: list[tuple[str, str]] = [
    ("computed_at", "TIMESTAMP"),
    ("space_id", "STRING"),
    ("criterion_id", "STRING"),
    ("criterion_version", "INT"),
    ("judge", "STRING"),
    ("n", "INT"),
    ("n_judge_error", "INT"),
    ("tp", "INT"),
    ("fn", "INT"),
    ("fp", "INT"),
    ("tn", "INT"),
    ("exact", "INT"),
    ("tpr", "DOUBLE"),
    ("tnr", "DOUBLE"),
    ("tpr_lb", "DOUBLE"),
    ("tnr_lb", "DOUBLE"),
    ("calibrated", "BOOLEAN"),
    ("n_by_reason", "STRING"),  # JSON text
    ("kappa", "DOUBLE"),
    ("n_applies", "INT"),
    ("applicability_agree", "INT"),
    ("positive_class", "STRING"),  # "pass" from 2026-10-27; NULL rows used fail (agreement.normalized)
]


def ddl(prefix: str) -> list[str]:
    """Idempotent DDL for the Delta tables under `<catalog>.<schema>`."""

    def table(name: str, columns: list[tuple[str, str]], comment: str) -> str:
        cols = ",\n  ".join(f"{c} {t}" for c, t in columns)
        quoted = comment.replace("\\", "\\\\").replace("'", "\\'")  # a ' in the text ends the literal
        return f"CREATE TABLE IF NOT EXISTS {prefix}.{name} (\n  {cols}\n) COMMENT '{quoted}'"

    return [
        f"CREATE SCHEMA IF NOT EXISTS {prefix}",
        table("raw_snapshots", SNAPSHOT_COLUMNS, "Verbatim Genie API responses, for re-parsing."),
        table("traces", TRACE_COLUMNS, "One row per agent turn (trace_id = Genie message id)."),
        table("steps", STEP_COLUMNS, "One row per tool call in a trace; key (trace_id, step_index)."),
        table("check_runs", CHECK_RUN_COLUMNS, "One row per (check run, space)."),
        table("current_results", CURRENT_RESULT_COLUMNS, "The latest verdict per (trace, rule, step)."),
        table(
            "check_results", CHECK_RESULT_COLUMNS, "One verdict per (run, trace, step or report, criterion)."
        ),
        table("review_items", REVIEW_ITEM_COLUMNS, "Traces queued for human review, and why."),
        table("human_labels", HUMAN_LABEL_COLUMNS, "One label per (trace, criterion, reviewer)."),
        table("judge_agreement", AGREEMENT_COLUMNS, "Judge vs human per criterion and judge version."),
        table(
            "trace_reviews", TRACE_REVIEW_COLUMNS, "Reviewer's task-success verdict and findings per turn."
        ),
        table(
            "observation_suggestions", SUGGESTION_COLUMNS, "Suggested failure category per observation (LLM)."
        ),
        table(
            "space_versions", SPACE_VERSION_COLUMNS, "A space configuration each time its content changed."
        ),
        table(
            "question_intents", INTENT_COLUMNS, "Intent label per production question and eval case (drift)."
        ),
        table("eval_runs", EVAL_RUN_COLUMNS, "One row per regression run of a space's eval set."),
        table("eval_results", EVAL_RESULT_COLUMNS, "One graded answer per (run, arm, case, attempt)."),
        table("artifacts", ARTIFACT_COLUMNS, "Rule, prompt and guideline texts, once each, by content hash."),
        table("run_manifests", MANIFEST_COLUMNS, "What produced each run's numbers, per (run, space)."),
    ]


@dataclass
class MergeCounts:
    inserted: int = 0
    updated: int = 0

    def __iadd__(self, other: "MergeCounts") -> "MergeCounts":
        self.inserted += other.inserted
        self.updated += other.updated
        return self


@dataclass(frozen=True)
class Snapshot:
    space_id: str
    conversation_id: str
    endpoint: str
    fetched_at: datetime
    payload: Any

    def row(self) -> dict[str, Any]:
        text = json.dumps(self.payload, sort_keys=True, default=str)
        return {
            "space_id": self.space_id,
            "conversation_id": self.conversation_id,
            "endpoint": self.endpoint,
            "fetched_at": self.fetched_at.isoformat(),
            "payload": text,
            "payload_sha256": hashlib.sha256(text.encode()).hexdigest(),
        }


class TraceWriter(Protocol):
    """Where the collector writes. `DeltaWriter` in production, a list-backed fake in tests."""

    async def write_snapshots(self, snapshots: Sequence[Snapshot]) -> None: ...

    async def merge_traces(self, traces: Sequence[Trace]) -> MergeCounts: ...

    async def merge_steps(self, steps: Sequence[Step], space_id: str) -> MergeCounts: ...


TABLE_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "raw_snapshots": SNAPSHOT_COLUMNS,
    "traces": TRACE_COLUMNS,
    "steps": STEP_COLUMNS,
    "check_runs": CHECK_RUN_COLUMNS,
    "check_results": CHECK_RESULT_COLUMNS,
    "review_items": REVIEW_ITEM_COLUMNS,
    "human_labels": HUMAN_LABEL_COLUMNS,
    "judge_agreement": AGREEMENT_COLUMNS,
    "trace_reviews": TRACE_REVIEW_COLUMNS,
    "observation_suggestions": SUGGESTION_COLUMNS,
    "space_versions": SPACE_VERSION_COLUMNS,
    "question_intents": INTENT_COLUMNS,
    "eval_runs": EVAL_RUN_COLUMNS,
    "current_results": CURRENT_RESULT_COLUMNS,
    "eval_results": EVAL_RESULT_COLUMNS,
    "artifacts": ARTIFACT_COLUMNS,
    "run_manifests": MANIFEST_COLUMNS,
}


def result_row(result: CheckResult) -> dict[str, Any]:
    row = result.model_dump(mode="json")
    row["evidence"] = json.dumps(result.evidence, default=str)
    return row


def trace_row(trace: Trace) -> dict[str, Any]:
    return trace.model_dump(mode="json")


def step_row(step: Step, space_id: str) -> dict[str, Any]:
    row = step.model_dump(mode="json", exclude={"result_schema", "result_rows"})
    row["space_id"] = space_id
    row["result_schema"] = json.dumps(step.result_schema) if step.result_schema is not None else None
    rows = step.result_rows[:MAX_RESULT_ROWS] if step.result_rows is not None else None
    row["result_rows"] = json.dumps(rows, default=str) if rows is not None else None
    return row


def insert_statement(table: str, columns: list[tuple[str, str]]) -> str:
    """Append a JSON batch (`:rows`). Used for check results: every run has a new run_id."""
    return f"INSERT INTO {table} SELECT inline(from_json(:rows, '{struct_type(columns)}'))"


def in_list(param: str, column: str) -> str:
    """`column` is one of the strings in the list parameter `:param` (passed as a Python list)."""
    return f"array_contains(from_json(:{param}, 'ARRAY<STRING>'), {column})"


def struct_type(columns: list[tuple[str, str]]) -> str:
    """`ARRAY<STRUCT<a: STRING, b: INT>>` for `from_json`."""
    return "ARRAY<STRUCT<" + ", ".join(f"{c}: {t}" for c, t in columns) + ">>"


def merge_statement(
    table: str, columns: list[tuple[str, str]], keys: list[str], keep: Sequence[str] = ()
) -> str:
    """MERGE a JSON batch (`:rows`) into `table` on `keys`: update matches, insert the rest.

    Columns in `keep` are not overwritten with NULL: an update that has no value for them
    keeps the stored one.
    """
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    if keep:
        update = ", ".join(
            f"{c} = coalesce(s.{c}, t.{c})" if c in keep else f"{c} = s.{c}" for c, _ in columns
        )
    else:
        update = "*"
    return (
        f"MERGE INTO {table} t "
        f"USING (SELECT inline(from_json(:rows, '{struct_type(columns)}'))) s "
        f"ON {on} "
        f"WHEN MATCHED THEN UPDATE SET {update} "
        "WHEN NOT MATCHED THEN INSERT *"
    )


# Genie stops serving a query's result after a while; re-reading the conversation then
# returns none, and the rows the checks rely on must survive the re-collect.
STEP_RESULT_COLUMNS = ("result_schema", "result_rows", "row_count")


class DeltaWriter:
    def __init__(self, sql: SqlClient, prefix: str):
        self._sql = sql
        self._prefix = prefix

    async def ensure_tables(self) -> None:
        """Create missing tables, then add any column the code has but an existing table lacks.

        Migrations are additive only (never drop or retype), so an older table keeps working
        with newer code and `MERGE ... UPDATE SET *` finds every column it writes.
        """
        for statement in ddl(self._prefix):
            await self._sql.execute(statement)
        catalog, schema = self._prefix.split(".")
        for table, columns in TABLE_COLUMNS.items():
            existing = await self._sql.execute(
                f"SELECT lower(column_name) AS c FROM {catalog}.information_schema.columns "
                "WHERE table_schema = :schema AND table_name = :table",
                {"schema": schema, "table": table},
            )
            have = {r["c"] for r in existing}
            missing = [(c, t) for c, t in columns if c.lower() not in have]
            if missing:
                cols = ", ".join(f"{c} {t}" for c, t in missing)
                await self._sql.execute(f"ALTER TABLE {self._prefix}.{table} ADD COLUMNS ({cols})")

    async def write_snapshots(self, snapshots: Sequence[Snapshot]) -> None:
        # Keyed on the content hash: re-fetching an unchanged conversation adds nothing.
        await self._merge(
            "raw_snapshots",
            SNAPSHOT_COLUMNS,
            ["conversation_id", "endpoint", "payload_sha256"],
            [s.row() for s in snapshots],
        )

    async def merge_traces(self, traces: Sequence[Trace]) -> MergeCounts:
        return await self._merge("traces", TRACE_COLUMNS, ["trace_id"], [trace_row(t) for t in traces])

    async def merge_steps(self, steps: Sequence[Step], space_id: str) -> MergeCounts:
        return await self._merge(
            "steps",
            STEP_COLUMNS,
            ["trace_id", "step_index"],
            [step_row(s, space_id) for s in steps],
            keep=STEP_RESULT_COLUMNS,
        )

    async def write_check_run(self, run: CheckRun) -> None:
        row = {**run.model_dump(mode="json"), "deferred": json.dumps(run.deferred)}
        await self._merge("check_runs", CHECK_RUN_COLUMNS, ["run_id", "space_id"], [row])

    async def write_check_results(self, results: Sequence[CheckResult]) -> None:
        await self._insert("check_results", CHECK_RESULT_COLUMNS, [result_row(r) for r in results])

    async def replace_current_results(self, space_id: str, run_id: str, trace_ids: Sequence[str]) -> None:
        """Make this run's results (already in `check_results`) the current verdicts of these
        traces; a rule no longer applied to them disappears."""
        ids = list(trace_ids)
        for start in range(0, len(ids), BATCH_ROWS):
            await self._sql.execute(
                f"DELETE FROM {self._prefix}.current_results WHERE space_id = :space_id AND {in_list('ids', 'trace_id')}",
                {"space_id": space_id, "ids": ids[start : start + BATCH_ROWS]},
            )
        columns = ", ".join(f"c.{name}" for name, _ in CHECK_RESULT_COLUMNS)
        await self._sql.execute(
            f"""INSERT INTO {self._prefix}.current_results
                SELECT :space_id, {columns} FROM {self._prefix}.check_results c
                JOIN {self._prefix}.traces t ON t.trace_id = c.trace_id AND t.space_id = :space_id
                WHERE c.run_id = :run_id""",
            {"space_id": space_id, "run_id": run_id},
        )

    async def last_check(self, space_id: str) -> dict[str, Any] | None:
        """The space's last successful check run: when it started, which rule set it used, and
        the traces whose judging it deferred."""
        rows = await self._sql.execute(
            f"""SELECT started_at, criteria_version, deferred FROM {self._prefix}.check_runs
                WHERE space_id = :space_id AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1""",
            {"space_id": space_id},
        )
        if not rows:
            return None
        return {**rows[0], "deferred": json.loads(rows[0]["deferred"] or "[]")}

    async def rollup_counts(self, space_id: str, days: Sequence[date]) -> list[dict[str, Any]]:
        """For the readable traces asked on these days, from their current verdicts: per (day,
        rule) how many fail and how many pass, and per day (`criterion_id` null) how many traces
        there are (`n_traces`) and how many a hard rule fails (`n_fail`). A trace's verdict on a rule
        is fail > error > pass > na across its steps, as in `checker.criterion_verdicts`."""
        if not days:
            return []
        return await self._sql.execute(
            f"""WITH t AS (
                    SELECT trace_id, CAST(coalesce(asked_at, collected_at) AS DATE) AS day
                    FROM {self._prefix}.traces
                    WHERE space_id = :space_id AND access_status = 'ok'
                      AND coalesce(asked_at, collected_at) >= :start AND coalesce(asked_at, collected_at) < :end),
                v AS (
                    SELECT trace_id, criterion_id,
                           CASE WHEN max(verdict = 'fail') THEN 'fail' WHEN max(verdict = 'error') THEN 'error'
                                WHEN max(verdict = 'pass') THEN 'pass' ELSE 'na' END AS verdict,
                           max(verdict = 'fail' AND gate = 'hard') AS hard_fail
                    FROM {self._prefix}.current_results WHERE space_id = :space_id
                    GROUP BY trace_id, criterion_id),
                d AS (SELECT * FROM t WHERE {in_list('days', 'CAST(day AS STRING)')})
                SELECT d.day, v.criterion_id, count_if(v.verdict = 'fail') AS n_fail,
                       count_if(v.verdict = 'pass') AS n_pass, CAST(NULL AS BIGINT) AS n_traces
                FROM d JOIN v USING (trace_id) GROUP BY d.day, v.criterion_id
                UNION ALL
                SELECT d.day, NULL, count_if(h.hard_fail), NULL, count(*)
                FROM d LEFT JOIN (SELECT trace_id, max(hard_fail) AS hard_fail FROM v GROUP BY trace_id) h
                  USING (trace_id)
                GROUP BY d.day""",
            {
                "space_id": space_id,
                "start": datetime.combine(min(days), time(), UTC),
                "end": datetime.combine(max(days) + timedelta(days=1), time(), UTC),
                "days": [d.isoformat() for d in days],
            },
        )

    async def load_traces(
        self,
        space_id: str,
        since: datetime | None = None,
        *,
        collected_since: datetime | None = None,
        trace_ids: Sequence[str] = (),
        conversation_ids: Sequence[str] = (),
    ) -> list[tuple[Trace, list[Step]]]:
        """A space's traces with their steps, in order: all of them, or those asked on or after
        `since`, collected (or re-collected) on or after `collected_since`, listed in `trace_ids`,
        or in `conversation_ids` (the filters are alternatives: a trace matching any is loaded)."""
        params: dict[str, Any] = {"space_id": space_id}
        any_of = []
        if since:
            any_of.append("asked_at >= :since")
            params["since"] = since
        if collected_since:
            any_of.append("collected_at >= :collected_since")
            params["collected_since"] = collected_since
        if trace_ids:
            any_of.append(in_list("trace_ids", "trace_id"))
            params["trace_ids"] = list(trace_ids)
        if conversation_ids:
            any_of.append(in_list("conversation_ids", "conversation_id"))
            params["conversation_ids"] = list(conversation_ids)
        where = "space_id = :space_id" + (f" AND ({' OR '.join(any_of)})" if any_of else "")
        trace_rows, step_rows = await asyncio.gather(
            self._sql.execute(
                f"SELECT * FROM {self._prefix}.traces WHERE {where} ORDER BY asked_at, turn_index", params
            ),
            self._sql.execute(
                f"SELECT s.* FROM {self._prefix}.steps s "
                f"JOIN (SELECT trace_id FROM {self._prefix}.traces WHERE {where}) t USING (trace_id) "
                "ORDER BY s.trace_id, s.step_index",
                params,
            ),
        )
        steps_by_trace: dict[str, list[Step]] = {}
        for row in step_rows:
            row.pop("space_id", None)
            row["result_schema"] = json.loads(row["result_schema"]) if row.get("result_schema") else None
            row["result_rows"] = json.loads(row["result_rows"]) if row.get("result_rows") else None
            steps_by_trace.setdefault(row["trace_id"], []).append(Step.model_validate(row))
        return [(Trace.model_validate(r), steps_by_trace.get(r["trace_id"], [])) for r in trace_rows]

    async def load_current_results(
        self, space_id: str, criterion_ids: list[str], trace_ids: Sequence[str] | None = None
    ) -> list[CheckResult]:
        """The space's current verdicts for these criteria (for reusing LLM verdicts, and review),
        optionally only for these traces."""
        if not criterion_ids:
            return []
        params = {"space_id": space_id, "criteria": ",".join(criterion_ids)}
        only = ""
        if trace_ids is not None:
            only = f" AND {in_list('trace_ids', 'r.trace_id')}"
            params["trace_ids"] = list(trace_ids)
        rows = await self._sql.execute(
            f"""SELECT r.* FROM {self._prefix}.current_results r
                WHERE r.space_id = :space_id AND array_contains(split(:criteria, ','), r.criterion_id){only}""",
            params,
        )
        for row in rows:
            row["evidence"] = json.loads(row["evidence"]) if row.get("evidence") else {}
        return [CheckResult.model_validate(r) for r in rows]

    async def merge_review_items(self, items: Sequence[ReviewItem]) -> MergeCounts:
        rows = [i.model_dump(mode="json") for i in items]
        return await self._merge("review_items", REVIEW_ITEM_COLUMNS, ["space_id", "trace_id"], rows)

    async def load_review_items(self, space_id: str) -> list[ReviewItem]:
        rows = await self._sql.execute(
            f"SELECT * FROM {self._prefix}.review_items WHERE space_id = :space_id", {"space_id": space_id}
        )
        return [ReviewItem.model_validate(r) for r in rows]

    async def merge_human_labels(self, labels: Sequence[HumanLabel]) -> MergeCounts:
        rows = [label.model_dump(mode="json") for label in labels]
        keys = ["trace_id", "criterion_id", "reviewer"]
        return await self._merge("human_labels", HUMAN_LABEL_COLUMNS, keys, rows)

    async def load_human_labels(self, space_id: str) -> list[HumanLabel]:
        rows = await self._sql.execute(
            f"SELECT * FROM {self._prefix}.human_labels WHERE space_id = :space_id", {"space_id": space_id}
        )
        return [HumanLabel.model_validate(r) for r in rows]

    async def merge_trace_reviews(self, reviews: Sequence[TraceReview]) -> MergeCounts:
        rows = [r.model_dump(mode="json") for r in reviews]
        return await self._merge(
            "trace_reviews", TRACE_REVIEW_COLUMNS, ["space_id", "trace_id", "reviewer"], rows
        )

    async def load_suggestions(self, space_id: str) -> dict[str, CategorySuggestion]:
        rows = await self._sql.execute(
            f"SELECT * FROM {self._prefix}.observation_suggestions WHERE space_id = :space_id",
            {"space_id": space_id},
        )
        return {r["trace_id"]: CategorySuggestion.model_validate(r) for r in rows}

    async def merge_suggestions(self, suggestions: Sequence[CategorySuggestion]) -> MergeCounts:
        rows = [s.model_dump(mode="json") for s in suggestions]
        return await self._merge(
            "observation_suggestions", SUGGESTION_COLUMNS, ["space_id", "trace_id"], rows
        )

    async def latest_space_version(self, space_id: str) -> str | None:
        rows = await self._sql.execute(
            f"""SELECT max_by(version, captured_at) AS version FROM {self._prefix}.space_versions
                WHERE space_id = :space_id""",
            {"space_id": space_id},
        )
        return rows[0]["version"] if rows else None

    async def write_space_version(self, row: dict[str, Any]) -> None:
        await self._sql.execute(
            insert_statement(f"{self._prefix}.space_versions", SPACE_VERSION_COLUMNS),
            {"rows": json.dumps([row])},
        )

    async def load_intents(self, space_id: str) -> list[dict[str, Any]]:
        return await self._sql.execute(
            f"SELECT * FROM {self._prefix}.question_intents WHERE space_id = :space_id",
            {"space_id": space_id},
        )

    async def merge_intents(self, rows: list[dict[str, Any]]) -> MergeCounts:
        return await self._merge("question_intents", INTENT_COLUMNS, ["space_id", "kind", "ref_id"], rows)

    async def merge_artifacts(self, rows: list[dict[str, Any]]) -> None:
        """Insert texts not stored yet. Keyed on the content hash, so a known text is left as it
        was, first_seen_at included."""
        if not rows:
            return
        await self._sql.execute(
            f"MERGE INTO {self._prefix}.artifacts t "
            f"USING (SELECT inline(from_json(:rows, '{struct_type(ARTIFACT_COLUMNS)}'))) s "
            "ON t.hash = s.hash WHEN NOT MATCHED THEN INSERT *",
            {"rows": json.dumps(rows, default=str)},
        )

    async def load_artifact_refs(self, kind: str, names: list[str]) -> list[dict[str, Any]]:
        """`{name, version, hash}` of every stored text of these names (no content)."""
        if not names:
            return []
        marks = ", ".join(f":n{i}" for i in range(len(names)))
        return await self._sql.execute(
            f"SELECT name, version, hash FROM {self._prefix}.artifacts WHERE kind = :kind AND name IN ({marks})",
            {"kind": kind, **{f"n{i}": n for i, n in enumerate(names)}},
        )

    async def load_manifests(self, space_id: str, run_kind: str) -> list[dict[str, Any]]:
        return await self._sql.execute(
            f"""SELECT manifest FROM {self._prefix}.run_manifests
                WHERE space_id = :space_id AND run_kind = :kind""",
            {"space_id": space_id, "kind": run_kind},
        )

    async def load_judged_attempts(self, space_id: str) -> list[dict[str, Any]]:
        """Graded regression answers whose expectations a judge decided (for calibrating it)."""
        return await self._sql.execute(
            f"""SELECT run_id, case_id, case_version, trace_id, conversation_id, question, report_text,
                       expectations, steps
                FROM {self._prefix}.eval_results
                WHERE space_id = :space_id AND trace_id IS NOT NULL AND verdict IN ('pass', 'fail')
                  AND (expectations LIKE '%"kind": "fact"%' OR expectations LIKE '%"kind": "behaviour"%')""",
            {"space_id": space_id},
        )

    async def merge_manifest(self, row: dict[str, Any]) -> MergeCounts:
        return await self._merge("run_manifests", MANIFEST_COLUMNS, ["run_id", "space_id"], [row])

    async def merge_eval_run(self, row: dict[str, Any]) -> None:
        """Insert or update one run (written when it starts and again when it ends)."""
        await self._merge("eval_runs", EVAL_RUN_COLUMNS, ["run_id"], [row])

    async def write_eval_results(self, results: Sequence[GradedAttempt]) -> None:
        rows = [
            {
                **r.model_dump(mode="json"),
                "hard_fails": json.dumps(r.hard_fails),
                "expectations": json.dumps([e.model_dump() for e in r.expectations]),
                "failed_by": json.dumps(r.failed_by),
                "steps": json.dumps([s.model_dump(mode="json") for s in r.steps]),
            }
            for r in results
        ]
        await self._insert("eval_results", EVAL_RESULT_COLUMNS, rows)

    async def write_agreement(self, rows: Sequence[Agreement]) -> None:
        if not rows:
            return
        data = [{**r.model_dump(mode="json"), "n_by_reason": json.dumps(r.n_by_reason)} for r in rows]
        await self._sql.execute(
            insert_statement(f"{self._prefix}.judge_agreement", AGREEMENT_COLUMNS), {"rows": json.dumps(data)}
        )

    async def _insert(self, name: str, columns: list[tuple[str, str]], rows: list[dict[str, Any]]) -> None:
        statement = insert_statement(f"{self._prefix}.{name}", columns)
        for start in range(0, len(rows), BATCH_ROWS):
            await self._sql.execute(
                statement, {"rows": json.dumps(rows[start : start + BATCH_ROWS], default=str)}
            )

    async def _merge(
        self,
        name: str,
        columns: list[tuple[str, str]],
        keys: list[str],
        rows: list[dict[str, Any]],
        keep: Sequence[str] = (),
    ) -> MergeCounts:
        # MERGE ignores a source field the table does not have, so a column missing from
        # `columns` would be dropped without a word: refuse it here instead.
        known = {c for c, _ in columns}
        unknown = sorted({k for row in rows for k in row} - known)
        if unknown:
            raise ValueError(f"{name}: no such column(s) {unknown}; add them to its column list")
        counts = MergeCounts()
        statement = merge_statement(f"{self._prefix}.{name}", columns, keys, keep)
        for start in range(0, len(rows), BATCH_ROWS):
            batch = rows[start : start + BATCH_ROWS]
            result = await self._sql.execute(statement, {"rows": json.dumps(batch, default=str)})
            if result:
                counts += MergeCounts(
                    inserted=int(result[0].get("num_inserted_rows") or 0),
                    updated=int(result[0].get("num_updated_rows") or 0),
                )
        return counts
