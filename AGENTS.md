# Notes for coding agents

Two jobs come up here: deploying the tool into someone's Databricks workspace, and changing its code. [SETUP.md](SETUP.md) has the same deploy steps for people; this file is the runbook in the order an agent runs it, with the places to stop and ask.

## Deploying

### Ground rules

- **The person logs in, not you.** They run `databricks auth login --host <workspace url> --profile <name>` themselves (it opens a browser). Pass `-p <profile>` on every CLI call. Never read, print or copy tokens or `~/.databrickscfg` secrets.
- **No secrets are needed.** Every identity is OAuth (the person, the service principals, the app). `.env.deploy` holds ids only; it is gitignored: never commit it.
- **Show the plan first, then ask before each step that creates or grants something** in their workspace, showing the command. Ask which tables the tester may read: that is a business decision.
- **Ignore CLI tips** such as "Databricks skills are not installed": they are not part of the deploy.
- **Genie questions cost DBU** (about 1 each). Ask first before seeding the demo (18 questions) or starting an eval run (cases × k, × 2 when testing a change).

### Steps

Run every command from the repository root. Collect first: the profile, a SQL warehouse id (`databricks warehouses list -p <p>`), and the catalog and schema for the tool's tables (default `workspace.genie_evalops`). Run SQL through the statement API (`databricks api post /api/2.0/sql/statements -p <p> --json '{"warehouse_id": "<id>", "statement": "...", "wait_timeout": "50s"}'`) or ask the person to run it in the SQL editor.

1. **Lakebase**: `databricks postgres list-projects --show-deleted -p <p>` (a deleted project keeps its id for a week, and plain `list-projects` hides it). If none fits, `databricks postgres create-project <project> -p <p>` with an id that list does not show, e.g. `genie-evalops`, else `genie-evalops-2` (Free Edition allows one live project). Branch `projects/<project>/branches/production`, endpoint `…/endpoints/primary`; use `<project>` in every path below.
2. **Schema**: `CREATE SCHEMA IF NOT EXISTS workspace.genie_evalops`.
3. **Service principals**, `evalops-collector` and `evalops-tester`, created with entitlements (without them jobs fail with "does not have access to Databricks Workspace"):
   ```
   databricks service-principals create -p <p> --json '{"displayName": "evalops-collector", "entitlements": [{"value": "workspace-access"}, {"value": "databricks-sql-access"}]}'
   ```
   Keep each `applicationId`. Then `uv run python scripts/sp_user.py --profile <p> <collector id> <tester id>`, so the jobs can run as them.
4. **Collector grants**:
   - `databricks permissions update warehouses <id> -p <p> --json '{"access_control_list": [{"service_principal_name": "<collector id>", "permission_level": "CAN_USE"}]}'`;
   - SQL: ``GRANT USE CATALOG ON CATALOG workspace TO `<collector id>` `` and ``GRANT USE SCHEMA, CREATE TABLE, SELECT, MODIFY ON SCHEMA workspace.genie_evalops TO `<collector id>` `` (BROWSE on the spaces' catalogs comes when a space is registered, below);
   - Lakebase role: `databricks postgres create-role projects/<project>/branches/production --role-id collector -p <p> --json '{"spec": {"identity_type": "SERVICE_PRINCIPAL", "postgres_role": "<collector id>", "auth_method": "LAKEBASE_OAUTH_V1"}}'`.
5. **Tester grants**: CAN_USE on the warehouse (same command as above), and exactly the consumers' data access: `USE CATALOG`, `USE SCHEMA` and `SELECT` on the space's tables or metric views. Nothing more: it checks this about itself before every run.
6. **Settings**: copy `.env.deploy.example` to `.env.deploy`; fill in the profile, warehouse, Lakebase paths and the two SP ids. Leave `APP_SP` empty. On Free Edition set `LLM_CONCURRENCY=1`.
7. **Deploy**: `scripts/deploy.sh` (needs Node 22 on `PATH`). On a first deploy it grants the collector, deploys the jobs, reports the app's "Table … does not exist" as expected, runs collect, then deploys again. It takes several minutes. The `free` target deploys in development mode: jobs are named `[dev <user>] evalops-*`, and the bundle's warnings about permissions sections are expected.
8. **App grants**: `databricks apps get genie-agent-evalops -p <p>` → `service_principal_client_id` into `.env.deploy` as `APP_SP`, then `scripts/grant.sh`.
9. **Verify**: with `T=$(databricks auth token -p <p> -o json | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")`, `curl -H "Authorization: Bearer $T" <app url>/api/health` must say `"state_store": "PostgresStore"`, and `<app url>/api/spaces` must answer 200 (`url` from `databricks apps get genie-agent-evalops -p <p>`). Give the person the app URL. A space shared with all workspace users, like Free Edition's sample space, is visible to the collector and shows as monitored without a grant.

Then, as they choose: register a space (collector CAN_MANAGE on it, plus `BROWSE` on its catalog), add reviewers ([SETUP.md](SETUP.md), "Add reviewers"), schedule the jobs, or build [the demo](demo/README.md).

### When something fails

| Symptom | Cause and fix |
|---|---|
| Job: "does not have access to Databricks Workspace" | The SP has no entitlements: add `workspace-access` and `databricks-sql-access` (`databricks service-principals patch`) |
| Deploy: "servicePrincipal.user" / cannot run as | `scripts/sp_user.py` for that SP |
| `create-role`: role_id must match pattern | Role ids start with a letter: `--role-id collector` |
| Collect: "permission denied for database databricks_postgres" | `scripts/grant.sh` (it works with `APP_SP` empty) |
| Deploy: "Number of app resources must be ≤ 20" | An app takes at most 20 resources: grant fewer tables |
| App page 500 after an upgrade | A new Lakebase table: run `scripts/grant.sh` |
| Reviewer: "Unable to view page" | The user lacks workspace access, or is not in `APP_VIEWERS` (then deploy again) |
| Review traces say "metadata not readable" | The collector lacks `BROWSE` on the catalog |
| Genie: "An internal error occurred" | The asker lacks `SELECT` on the space's tables |

## Changing the code

- Backend: `uv sync --extra dev`, `uv run ruff check .`, `uv run ruff format .`, `uv run pytest` (HTTP is mocked; no test touches a workspace).
- Frontend (`frontend/`): `npm ci`, `npm run build`. After changing an API schema: `uv run python scripts/export_openapi.py && (cd frontend && npm run gen:types)`; CI fails when the committed types drift.
- A new Delta table the app reads must exist before the app's grant on it deploys; a new Lakebase table needs `scripts/grant.sh` after the job that creates it.
- Editing a judge's prompt (`skills/`) requires raising its `version`: deploys refuse a changed prompt under the same version.
