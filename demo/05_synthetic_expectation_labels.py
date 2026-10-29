"""Write synthetic human labels for the sandbox's judged regression answers, to exercise the
expectation_judge calibration end to end without a person labelling.

Usage:
    python demo/05_synthetic_expectation_labels.py --profile personal --space <sandbox space id> \
        --warehouse <id> [--catalog workspace] [--schema genie_evalops] [--disagree-every 4]

Refuses any space whose title does not end with "(sandbox)". Each label copies the judge's own
verdict, except every Nth answer (by trace id), which gets the opposite one, so the measured
agreement is not trivially perfect. The labels' reviewer is `synthetic`; the review job names
them on the judge's agreement row, so they can never pass for real ones.
"""

import argparse
import json
from datetime import UTC, datetime

from databricks.sdk import WorkspaceClient

REVIEWER = "synthetic"


def query(w: WorkspaceClient, warehouse: str, sql: str, params: dict | None = None) -> list[dict]:
    from databricks.sdk.service.sql import StatementParameterListItem

    result = w.statement_execution.execute_statement(
        warehouse_id=warehouse,
        statement=sql,
        parameters=[StatementParameterListItem(name=k, value=str(v)) for k, v in (params or {}).items()],
        wait_timeout="50s",
    )
    if result.status.error:
        raise RuntimeError(result.status.error.message)
    names = [c.name for c in result.manifest.schema.columns] if result.manifest else []
    return (
        [dict(zip(names, row, strict=True)) for row in (result.result.data_array or [])]
        if result.result
        else []
    )


def verdict(expectations: str) -> str | None:
    judged = [e for e in json.loads(expectations or "[]") if e.get("kind") in ("fact", "behaviour")]
    if not judged:
        return None
    if any(e.get("passed") is False for e in judged):
        return "fail"
    return "pass" if all(e.get("passed") is True for e in judged) else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--schema", default="genie_evalops")
    parser.add_argument("--disagree-every", type=int, default=4)
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile)
    title = w.api_client.do("GET", f"/api/2.0/genie/spaces/{args.space}").get("title", "")
    if not title.endswith("(sandbox)"):
        raise SystemExit(
            f"Refusing: {title!r} is not a sandbox space. Synthetic labels never go on a real agent."
        )

    prefix = f"{args.catalog}.{args.schema}"
    # Every judged regression answer of the space, not only queued ones: the sandbox has no
    # reviewers, so nothing is queued there; agreement pairs labels with answers by trace id.
    rows = query(
        w,
        args.warehouse,
        f"""SELECT trace_id, max_by(expectations, graded_at) AS expectations
            FROM {prefix}.eval_results
            WHERE space_id = :space AND trace_id IS NOT NULL AND verdict IN ('pass', 'fail')
            GROUP BY trace_id ORDER BY trace_id""",
        {"space": args.space},
    )
    now = datetime.now(UTC).isoformat()
    labels = []
    for n, r in enumerate(rows):
        judged = verdict(r["expectations"])
        if judged is None:
            continue
        flip = args.disagree_every and n % args.disagree_every == 0
        labels.append(
            {
                "space_id": args.space,
                "trace_id": r["trace_id"],
                "mlflow_trace_id": "",  # never in MLflow: no person saw it
                "criterion_id": "expectations",
                "criterion_version": 1,
                "reviewer": REVIEWER,
                "label": ({"pass": "fail", "fail": "pass"}[judged]) if flip else judged,
                "rationale": "synthetic label (sandbox): "
                + ("disagrees with the judge on purpose" if flip else "copies the judge"),
                "assessment_id": f"synthetic-{r['trace_id']}",
                "labeled_at": now,
            }
        )
    if not labels:
        raise SystemExit("No queued expectation reviews with a judge verdict yet: run the review job first.")
    query(
        w,
        args.warehouse,
        f"""MERGE INTO {prefix}.human_labels t
            USING (SELECT inline(from_json(:rows, 'array<struct<space_id:string,trace_id:string,mlflow_trace_id:string,
                   criterion_id:string,criterion_version:int,reviewer:string,label:string,rationale:string,
                   assessment_id:string,labeled_at:timestamp>>'))) s
            ON t.trace_id = s.trace_id AND t.criterion_id = s.criterion_id AND t.reviewer = s.reviewer
            WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *""",
        {"rows": json.dumps(labels)},
    )
    flipped = sum("on purpose" in label["rationale"] for label in labels)
    print(f"{len(labels)} synthetic labels written ({flipped} disagree with the judge) on {title}")


if __name__ == "__main__":
    main()
