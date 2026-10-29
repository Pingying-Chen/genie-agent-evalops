"""Entrypoint of the `collect` job (runs as the collector service principal).

    evalops-collect [--space SPACE_ID ...]

Without `--space` it collects every monitored space (CAN_MANAGE held by this identity).
"""

import argparse
import logging
import uuid
from datetime import UTC, datetime

import httpx

from evalops.clients.auth import Auth
from evalops.clients.genie import GenieClient
from evalops.clients.lakebase import open_store
from evalops.clients.sql import SqlClient
from evalops.config import apply_overrides, get_settings
from evalops.jobs import finish, run_async
from evalops.services import collector
from evalops.services.delta_store import DeltaWriter

logger = logging.getLogger("evalops.collect")


async def run(space_ids: list[str] | None) -> int:
    """Collect the given (or all monitored) spaces; return a process exit code."""
    settings = get_settings()
    auth = Auth.from_sdk()
    run_id = uuid.uuid4().hex
    store = await open_store(
        settings.lakebase_endpoint,
        settings.lakebase_database,
        settings.lakebase_schema,
        user=settings.lakebase_user,
        require_durable=True,
    )
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as http:
        genie = GenieClient(http, auth)
        sql = SqlClient(http, auth, settings.warehouse_id, max_concurrent=settings.max_concurrent_statements)
        writer = DeltaWriter(sql, settings.table_prefix)
        await writer.ensure_tables()
        # Always resolve what this identity may read (and record each space's status), so a
        # space whose CAN_MANAGE grant was revoked shows "not monitored" instead of failing.
        monitored = await collector.monitored_spaces(genie, settings, store)
        spaces = [s for s in space_ids if s in monitored] if space_ids else monitored
        for skipped in sorted(set(space_ids or []) - set(spaces)):
            # A revoked grant usually hides the space from this identity entirely, so
            # monitored_spaces never sees it: record the loss explicitly.
            await store.put_space_status(skipped, None, False)
            logger.warning("skipping %s: the collector does not hold CAN_MANAGE on it", skipped)
        failed = 0
        for space_id in spaces:
            try:
                stats = await collector.collect_space(
                    space_id, genie=genie, writer=writer, store=store, settings=settings, run_id=run_id
                )
                logger.info("collected %s", stats)
            except Exception:
                failed += 1
                logger.exception("space %s failed", space_id)
                continue
            try:  # a missing version must not fail the collection it follows
                version = await collector.record_space_version(
                    space_id, genie=genie, writer=writer, now=datetime.now(UTC)
                )
                if version:
                    logger.info("space %s: new configuration version %s", space_id, version)
            except Exception:
                logger.exception("space %s: could not record its configuration version", space_id)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--set",
        action="append",
        dest="overrides",
        metavar="KEY=VALUE",
        help="Setting override, e.g. warehouse_id=abc.",
    )
    parser.add_argument(
        "--space", action="append", dest="spaces", help="Collect only this space (repeatable)."
    )
    args = parser.parse_args()
    apply_overrides(args.overrides)
    spaces = [space for space in (args.spaces or []) if space] or None  # `--space=` (empty) = all
    finish(run_async(run(spaces)))


if __name__ == "__main__":
    main()
