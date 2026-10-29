"""Entrypoint of the `ask` job, run as the TESTER service principal.

    evalops-ask --run-dir /Workspace/.../evalops-runs/<run_id> [--set KEY=VALUE ...]

Reads `request.json` from the run folder the `evaluate` job wrote, checks its own access,
asks every case in scratch copies of the configurations, writes the answers back, and deletes
the scratch spaces. It needs no Delta or Lakebase access: the folder is the whole interface.
"""

import argparse
import logging

import httpx
from databricks.sdk import WorkspaceClient

from evalops.clients.auth import Auth
from evalops.clients.genie import GenieClient
from evalops.clients.sql import SqlClient
from evalops.clients.workspace_folder import WorkspaceFolder
from evalops.config import apply_overrides, get_settings
from evalops.jobs import finish, run_async
from evalops.schemas.evaluation import AskRequest
from evalops.services.regression import asker

logger = logging.getLogger("evalops.ask")


async def run(run_dir: str) -> int:
    settings = get_settings()
    w = WorkspaceClient()
    me = w.current_user.me()
    folder = WorkspaceFolder(w, run_dir)
    request = AskRequest.model_validate_json(await folder.read("request.json"))
    auth = Auth.from_sdk(w)
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as http:
        genie = GenieClient(http, auth)
        sql = SqlClient(
            http, auth, request.arms[0].warehouse_id, max_concurrent=settings.max_concurrent_statements
        )
        done = await asker.run(
            request,
            me.user_name or "",
            f"/Workspace/Users/{me.user_name}",
            genie=genie,
            sql=sql,
            folder=folder,
        )
    logger.info(
        "asked %d attempts; scratch spaces deleted: %s; error: %s", done.attempts, done.deleted, done.error
    )
    return 1 if done.error or not done.deleted else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", action="append", dest="overrides", metavar="KEY=VALUE")
    parser.add_argument("--run-dir", required=True)
    args = parser.parse_args()
    apply_overrides(args.overrides)
    finish(run_async(run(args.run_dir)))


if __name__ == "__main__":
    main()
