# Source .env.deploy and export it as bundle variables. Used by the scripts below.
set -a
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/../.env.deploy"
set +a
export BUNDLE_VAR_warehouse_id="$WAREHOUSE_ID"
export BUNDLE_VAR_catalog="$CATALOG"
export BUNDLE_VAR_schema="$SCHEMA"
export BUNDLE_VAR_lakebase_branch="$LAKEBASE_BRANCH"
export BUNDLE_VAR_lakebase_endpoint="$LAKEBASE_ENDPOINT"
export BUNDLE_VAR_collector_sp="$COLLECTOR_SP"
export BUNDLE_VAR_tester_sp="$TESTER_SP"
export BUNDLE_VAR_eval_k="${EVAL_K:-3}"
export BUNDLE_VAR_llm_concurrency="${LLM_CONCURRENCY:-2}"
if [ -n "${JUDGE_MODEL:-}" ]; then export BUNDLE_VAR_judge_model="$JUDGE_MODEL"; fi
export BUNDLE_VAR_review_timezone="${REVIEW_TIMEZONE:-UTC}"
export BUNDLE_VAR_review_budget="${REVIEW_BUDGET:-30}"
export BUNDLE_VAR_extra_criteria_dir="${EXTRA_CRITERIA_DIR:-none}"
# The commit every run's manifest records; -dirty when the deployed tree differs from it.
BUNDLE_VAR_platform_commit="$(git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then BUNDLE_VAR_platform_commit="${BUNDLE_VAR_platform_commit}-dirty"; fi
export BUNDLE_VAR_platform_commit

# APP_VIEWERS (comma-separated emails or group:<name>) -> the complex bundle variable app_viewers,
# written to the bundle's variable-overrides file (complex variables cannot come from BUNDLE_VAR_*).
python3 - "$BUNDLE_TARGET" "${APP_VIEWERS:-}" <<'PY'
import json, pathlib, sys
target, raw = sys.argv[1], sys.argv[2]
entries = []
for item in filter(None, (x.strip() for x in raw.split(","))):
    key = "group_name" if item.startswith("group:") else "user_name"
    entries.append({"level": "CAN_USE", key: item.removeprefix("group:")})
path = pathlib.Path(".databricks/bundle") / target / "variable-overrides.json"
path.parent.mkdir(parents=True, exist_ok=True)
overrides = json.loads(path.read_text()) if path.exists() else {}
overrides["app_viewers"] = entries
path.write_text(json.dumps(overrides, indent=1))
PY
