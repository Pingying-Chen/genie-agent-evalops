"""Grant Lakebase privileges to the app's and/or the collector's service principal.

Run once, as the current owner of the `evalops` schema:

    uv run python scripts/grant_lakebase.py --profile <p> --endpoint <projects/.../endpoints/...> \
        [--app-client-id <app SP>] [--collector-client-id <jobs SP>] [--schema evalops]

- app: read everything, write only the permission cache (the bundle's `postgres` app resource
  creates its role and lets it connect).
- collector: bootstrap the schema and write all state. Create its role first:
  `databricks postgres create-role <branch> --role-id collector --json '{"spec": {"identity_type":
  "SERVICE_PRINCIPAL", "postgres_role": "<id>", "auth_method": "LAKEBASE_OAUTH_V1"}}'`
"""

import argparse
import asyncio

from databricks.sdk import WorkspaceClient

from evalops.clients.lakebase import PostgresStore, app_grants, collector_grants


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--app-client-id")
    parser.add_argument("--collector-client-id")
    parser.add_argument("--schema", default="evalops")
    parser.add_argument("--database", default="databricks_postgres")
    args = parser.parse_args()
    store = await PostgresStore.connect(
        args.endpoint, args.database, args.schema, w=WorkspaceClient(profile=args.profile)
    )
    async with store._pool.acquire() as conn:
        statements = []
        if args.app_client_id:
            statements += app_grants(args.schema, args.app_client_id)
        if args.collector_client_id:
            statements += collector_grants(args.schema, args.database, args.collector_client_id)
        for statement in statements:
            await conn.execute(statement)
            print(statement)
    await store.close()


if __name__ == "__main__":
    asyncio.run(main())
