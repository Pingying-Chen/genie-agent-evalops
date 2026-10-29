#!/usr/bin/env bash
# Grant the app's and the collector's service principals their Lakebase privileges.
# deploy.sh runs it before the first collect (APP_SP still empty); run it again once APP_SP is set,
# and after every upgrade.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
uv run python scripts/grant_lakebase.py --profile "$DATABRICKS_PROFILE" --endpoint "$LAKEBASE_ENDPOINT" \
  --app-client-id "$APP_SP" --collector-client-id "$COLLECTOR_SP"
