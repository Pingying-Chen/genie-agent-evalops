"""Compare judge configurations on labelled examples: agreement with people, accuracy, flips.

    uv run python scripts/judge_experiment.py --profile <p> --model databricks-qwen3-next-80b-a3b-instruct \
        --config v4@1=skills:1.0 --config v5@1=/path/to/candidate/skills:1.0 --k 3 \
        --examples judge_data/rubric.ambiguous_metric.dev.jsonl \
        --examples judge_data/rubric.ambiguous_metric.holdout.jsonl

Each --config is `label=<skills folder>:<temperature>`; each --examples file is scored on its own
(a dev set and a holdout from scripts/export_judge_labels.py, or a skill's examples.jsonl).
Every example is judged k times per config. Reports, per config and file:
- agreement with the labels on the k-sample majority: TPR / TNR with Wilson lower bounds and
  kappa, counted where the label says the rule applies, plus applicability agreement (the same
  numbers as the app's calibration card, from `agreement.score`)
- single-shot and majority accuracy, flip rate (examples whose k samples did not all agree)
- errors and tokens
Change a judge against the dev set; accept it only if the holdout does not get worse.
"""

import argparse
import asyncio
import collections
import json
from pathlib import Path

import httpx
from databricks.sdk import WorkspaceClient

from evalops.clients.auth import Auth
from evalops.schemas.llm import RubricJudgeInput
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import load_skill
from evalops.services.review.agreement import score


async def run_config(
    runner: SkillRunner, folder: Path, temperature: float, examples: list[dict], k: int
) -> dict:
    skill = load_skill("rubric_judge", folder)
    per_example = []
    tokens = errors = 0
    for example in examples:
        data = RubricJudgeInput(**{"context": "", **example["input"]})
        verdicts = []
        for _ in range(k):  # sequential: Free Edition's pay-per-token QPS limit is low
            result = await runner.run(skill, data, temperature=temperature)
            tokens += result.tokens
            if result.output is None:
                errors += 1
                verdicts.append("error")
                continue
            consistent = getattr(result.output, "consistent", lambda: True)()
            verdicts.append(result.output.verdict if consistent else "error")
            errors += not consistent
        majority = collections.Counter(verdicts).most_common(1)[0][0]
        per_example.append(
            {
                "name": example["name"],
                "expected": example["expected"],
                "verdicts": verdicts,
                "majority": majority,
            }
        )
    n = len(per_example)
    single = sum(v == e["expected"] for e in per_example for v in e["verdicts"]) / (n * k)
    return {
        "skill": skill.ref,
        "temperature": temperature,
        "agreement": score([(e["expected"], e["majority"]) for e in per_example]),
        "single_shot_accuracy": round(single, 3),
        "majority_accuracy": round(sum(e["majority"] == e["expected"] for e in per_example) / n, 3),
        "flip_rate": round(sum(len(set(e["verdicts"])) > 1 for e in per_example) / n, 3),
        "errors": errors,
        "tokens": tokens,
        "per_example": per_example,
    }


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--config", action="append", required=True, help="label=<skills folder>:<temperature>"
    )
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--examples", action="append", help="a .jsonl of examples; repeat for dev and holdout"
    )
    parser.add_argument("--out", help="Write the full results as JSON here.")
    args = parser.parse_args()

    files = args.examples or ["skills/rubric_judge/examples.jsonl"]
    auth = Auth.from_sdk(WorkspaceClient(profile=args.profile))
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=180) as http:
        runner = SkillRunner(http, auth, args.model, max_concurrent=1)
        for file in files:
            examples = [json.loads(line) for line in Path(file).read_text().splitlines() if line.strip()]
            for spec in args.config:
                label, _, rest = spec.partition("=")
                folder, _, temperature = rest.rpartition(":")
                r = await run_config(runner, Path(folder), float(temperature), examples, args.k)
                results.setdefault(file, {})[label] = r
                a = r["agreement"]
                print(
                    f"{Path(file).name:40s} {label:8s} {r['skill']:16s} n={a['n']} applies={a['n_applies']} "
                    f"TPR={a['tp']}/{a['tp'] + a['fn']} (LB {pct(a['tpr_lb'])}) "
                    f"TNR={a['tn']}/{a['tn'] + a['fp']} (LB {pct(a['tnr_lb'])}) "
                    f"kappa={'-' if a['kappa'] is None else round(a['kappa'], 2)} "
                    f"applicability={a['applicability_agree']}/{a['n']} "
                    f"majority={r['majority_accuracy']:.0%} flip={r['flip_rate']:.0%} "
                    f"errors={r['errors']} tokens={r['tokens']}",
                    flush=True,
                )
    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
