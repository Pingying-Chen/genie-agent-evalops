#!/usr/bin/env bash
# Build the frontend, deploy the bundle, then (re)start the app.
# Settings come from .env.deploy (see .env.deploy.example). Extra args go to `bundle deploy`.
# On the first deploy, the app's table grants fail until the first collect has created the tables:
# so it grants the collector, deploys the jobs, runs collect once, then deploys again.
set -euo pipefail
cd "$(dirname "$0")/.."
source scripts/env.sh
(cd frontend && npm ci --no-audit --no-fund && npm run build)
deploy() { databricks bundle deploy -t "$BUNDLE_TARGET" -p "$DATABRICKS_PROFILE" "$@"; }
exists=$(databricks tables exists "$CATALOG.$SCHEMA.traces" -p "$DATABRICKS_PROFILE" -o json |
  python3 -c "import json, sys; print(json.load(sys.stdin)['table_exists'])")
if [ "$exists" != "True" ]; then
  echo "First deploy: the tables do not exist yet."
  scripts/grant.sh
  deploy "$@" || echo "Expected: the app cannot be created before its tables exist. Running collect."
  databricks bundle run collect -t "$BUNDLE_TARGET" -p "$DATABRICKS_PROFILE"
fi
deploy "$@"
databricks bundle run evalops -t "$BUNDLE_TARGET" -p "$DATABRICKS_PROFILE"
