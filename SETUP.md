# Set up by hand

The steps a coding agent follows from [AGENTS.md](AGENTS.md), to run yourself. Pass `-p <profile>` to every `databricks` command if your workspace is not the default profile.

You need a workspace with Genie, [Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/), a SQL warehouse, [Lakebase](https://docs.databricks.com/aws/en/oltp/), and compute for the jobs. [Free Edition](https://docs.databricks.com/aws/en/getting-started/free-edition) works. On your machine: the [Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/), Python 3.12 with [uv](https://docs.astral.sh/uv/), and Node 22. Log in with `databricks auth login --host <workspace url> --profile <name>`.

## Steps

1. **Lakebase project**: `databricks postgres create-project genie-evalops` (any id; a deleted project keeps its id for a week). Its branch is `projects/genie-evalops/branches/production` and its endpoint `…/endpoints/primary`.
2. **A schema** for the tool's tables: `CREATE SCHEMA workspace.genie_evalops` (or the `CATALOG` / `SCHEMA` you set below).
3. **Two [service principals](https://docs.databricks.com/aws/en/admin/users-groups/service-principals)**, `evalops-collector` and `evalops-tester`, each with the entitlements a job identity needs:
   ```
   databricks service-principals create --json '{"displayName": "evalops-collector", "entitlements": [{"value": "workspace-access"}, {"value": "databricks-sql-access"}]}'
   ```
   and the same for `evalops-tester`. Then give yourself the `servicePrincipal.user` role on both, so the jobs can run as them (a workspace admin does not have it by default): `uv run python scripts/sp_user.py --profile <profile> <collector id> <tester id>`. Ids are the application ids.
4. **The collector** reads your spaces' conversations and runs the checks. Give it:
   - CAN_USE on the warehouse;
   - `USE CATALOG`, `USE SCHEMA`, `CREATE TABLE`, `SELECT` and `MODIFY` on the schema ([privileges](https://docs.databricks.com/aws/en/data-governance/unity-catalog/manage-privileges/));
   - a Lakebase role: `databricks postgres create-role projects/genie-evalops/branches/production --role-id collector --json '{"spec": {"identity_type": "SERVICE_PRINCIPAL", "postgres_role": "<collector id>", "auth_method": "LAKEBASE_OAUTH_V1"}}'`.
5. **The tester** (only needed for testing changes) asks your eval set as a consumer would. Give it CAN_USE on the warehouse and exactly your consumers' data access (`USE CATALOG`, `USE SCHEMA` and `SELECT` on the space's tables or metric views), nothing else. It checks this about itself before every run.
6. **Settings**: copy `.env.deploy.example` to `.env.deploy` and fill it in (see [Settings](#settings)), leaving `APP_SP` empty for now.
7. **Deploy**: `scripts/deploy.sh`. The app reads tables that the first collect creates, so the first time it also grants the collector its Lakebase privileges, deploys the jobs (the app fails then, which it reports as expected), runs `evalops-collect` once and deploys again.
8. **Let the app write its state**: put the app's service principal id (`databricks apps get genie-agent-evalops`) in `.env.deploy` as `APP_SP` and run `scripts/grant.sh`. After an upgrade, run it again; repeating it is safe.

## After the first deploy

**No Genie space yet?** Optionally, test it on [the demo](demo/README.md): it builds a sample Genie space from `samples.wanderbricks` with seed questions, rules and a sandbox. Asking Genie costs about 1 DBU a question: the seed questions are 18.

**Add a space**: give the collector CAN_MANAGE on it ([sharing a space](https://docs.databricks.com/aws/en/genie-agents/set-up)) and `BROWSE` on the catalog of its tables, so reviewers see their descriptions and the data boundary (metadata only, not the data). The next collect picks it up.

**Add reviewers**: create a group `genie-reviewers-<space id>` with the people who should label that space's answers (they need workspace access, which users invited in the UI have), add `group:genie-reviewers-<space id>` to `APP_VIEWERS` and deploy. Each reviewer then opens the app once to register.

**Schedule the jobs** you want to run on their own: `evalops-collect` (new conversations, then checks) and `evalops-review` (labels, judge measurement, the next review sample), e.g. daily. Add a schedule to the job in the Jobs UI. A weekly drift check runs on Sundays already (see [USAGE.md](USAGE.md#7-compare-runs-and-catch-genie-changes)).

## Settings

In `.env.deploy`:

| Setting | Default | Meaning |
|---|---|---|
| `DATABRICKS_PROFILE` | — | CLI profile of the workspace |
| `WAREHOUSE_ID` | — | SQL warehouse the tool uses |
| `CATALOG` / `SCHEMA` | `workspace` / `genie_evalops` | Where the tool keeps its tables |
| `LAKEBASE_BRANCH` / `LAKEBASE_ENDPOINT` | — | `projects/<p>/branches/<b>` and `…/endpoints/<e>` |
| `COLLECTOR_SP` / `TESTER_SP` / `APP_SP` | — | Application ids of the service principals |
| `APP_VIEWERS` | you | Who may open the app (emails or `group:<name>`); they still see only spaces they manage |
| `EXTRA_CRITERIA_DIR` | `none` | Your own rules and judges |
| `EVAL_K` | `3` | Times each case is asked per configuration when testing a change |
| `REVIEW_BUDGET` | `30` | Answers queued for review per space per review run |
| `REVIEW_TIMEZONE` | `UTC` | Time zone reviewers read times in |
| `JUDGE_MODEL` | `databricks-qwen3-next-80b-a3b-instruct` | Serving endpoint every LLM skill calls |
| `LLM_CONCURRENCY` | `2` | Parallel judge calls (1 on Free Edition) |

To stop the weekly drift check, deploy with `drift_check_pause=PAUSED`.
