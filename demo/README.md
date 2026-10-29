# Try it on a demo agent

A small Genie agent you can build in any workspace (Free Edition included) from the `samples.wanderbricks` dataset in the `samples` catalog. It gives the tool real conversations to check, rules to calibrate judges on, and a sandbox for testing changes.

## Steps

1. **Metric view**: run `demo/01_metric_view.sql` on a SQL warehouse. It creates `workspace.genie_demo.booking_metrics` over bookings, properties, destinations and countries ([metric views](https://docs.databricks.com/aws/en/metric-views/)).
2. **The agent**: `uv run python demo/02_create_space.py --profile <profile> --warehouse <id>` creates "Wanderbricks Bookings Assistant": five short rules (R1–R5: which revenue, no averaged ratios, state the period, no personal data, clarify ambiguous metrics) and one SQL example. Re-running it updates the space in place.
3. **Register it**: give the collector service principal CAN_MANAGE on the space and `BROWSE` on the `workspace` catalog, and give your consumers (and the tester) `USE CATALOG` on `workspace`, `USE SCHEMA` on `workspace.genie_demo` and `SELECT` on the metric view. Genie runs SQL as the person asking, so without it Genie answers "An internal error occurred".
4. **Traffic**: `uv run python demo/03_seed_questions.py --profile <profile> --space <space id>` asks the 18 questions in `demo/seed_questions.yaml` in agent mode, each in a new conversation (`--multi-turn` for the follow-up scripts). Then **Collect now** in the app.
5. **Your own rules**: set `EXTRA_CRITERIA_DIR=demo/criteria` in `.env.deploy` and deploy, so the demo's data rules and its two LLM judges (`demo/criteria`) run too.

Optional copies on the same view:

- **Control** (`--control`): the agent without R4 and R5, so the judges of those rules see real failures to be calibrated on.
- **Sandbox** (`--sandbox`): the same agent for trying the eval set loop. `uv run python demo/04_seed_sandbox.py --profile <profile> --space <sandbox id> --collector <collector id> --app <app url>` registers it and imports 23 synthetic eval cases (`demo/sandbox_cases.yaml`, marked `origin: synthetic`). Use it to try dev and gate runs, and to apply and roll back changes, without touching an agent people use. After a dev run, `uv run python demo/05_synthetic_expectation_labels.py --profile <profile> --space <sandbox id> --warehouse <id>` writes made-up review labels so you can see judge calibration work without labelling (it needs `SELECT` on `eval_results` and `MODIFY` on `human_labels`, which the collector created: grant them to yourself as the schema owner); it refuses any space whose title does not end with "(sandbox)", and the judge's agreement says the labels were synthetic.

Testing a change asks Genie cases × k × 2 questions (about 1 DBU each): keep sandbox runs small.

