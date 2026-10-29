"""Export a space's human labels for one rubric criterion as a time-split judge dataset.

    uv run python scripts/export_judge_labels.py --profile <p> --warehouse <id> --space <space_id> \
        --criterion rubric.ambiguous_metric [--extra-criteria demo/criteria] [--holdout 0.3] [--out judge_data]

Writes <out>/<criterion>.dev.jsonl and <out>/<criterion>.holdout.jsonl in the shape
scripts/judge_experiment.py reads. The files hold real questions and answers: `judge_data/` is
gitignored; keep them out of the repo.
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from databricks.sdk import WorkspaceClient

from evalops.clients.auth import Auth
from evalops.clients.sql import SqlClient
from evalops.services.criteria import criteria_folders, load_criteria
from evalops.services.delta_store import DeltaWriter
from evalops.services.judge_data import examples_from_labels, time_split


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--warehouse", required=True)
    parser.add_argument("--space", required=True)
    parser.add_argument("--criterion", required=True)
    parser.add_argument("--extra-criteria", default="", help="the deployment's own criteria folder")
    parser.add_argument("--prefix", default="workspace.genie_evalops")
    parser.add_argument("--holdout", type=float, default=0.3)
    parser.add_argument("--out", default="judge_data")
    args = parser.parse_args()

    criteria, _ = load_criteria(*criteria_folders("", args.extra_criteria))
    criterion = next((c for c in criteria if c.id == args.criterion), None)
    if criterion is None:
        raise SystemExit(f"No criterion {args.criterion!r}; pass --extra-criteria for a deployment's own.")
    auth = Auth.from_sdk(WorkspaceClient(profile=args.profile))
    async with httpx.AsyncClient(timeout=120) as http:
        delta = DeltaWriter(SqlClient(http, auth, args.warehouse), args.prefix)
        labels = await delta.load_human_labels(args.space)
        loaded = await delta.load_traces(args.space)
    examples = examples_from_labels(labels, loaded, criterion)
    dev, holdout = time_split(examples, args.holdout)
    out = Path(args.out)
    out.mkdir(exist_ok=True)
    for name, rows in (("dev", dev), ("holdout", holdout)):
        path = out / f"{criterion.id}.{name}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        counts = {k: sum(r["expected"] == k for r in rows) for k in ("fail", "pass", "na")}
        print(f"{path}: {len(rows)} examples {counts}")


if __name__ == "__main__":
    asyncio.run(main())
