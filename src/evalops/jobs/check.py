"""Entrypoint of the `check` job: run the criteria over collected traces.

    evalops-check [--space SPACE_ID ...] [--since YYYY-MM-DD] [--set KEY=VALUE ...]

Without `--space` it checks every space that has traces. Each run checks only what is new since
the last one (see `services.checker`); its results are kept as history under a new `run_id` and
become the traces' current verdicts, which the pages read.
"""

import argparse
import logging
import uuid
from datetime import UTC, datetime

import httpx

from evalops.clients.auth import Auth
from evalops.clients.lakebase import open_store
from evalops.clients.sql import SqlClient
from evalops.config import apply_overrides, get_settings
from evalops.jobs import finish, run_async
from evalops.services import manifests, profiles
from evalops.services.checker import check_space
from evalops.services.delta_store import DeltaWriter
from evalops.services.llm.runner import SkillRunner

logger = logging.getLogger("evalops.check")


async def run(space_ids: list[str] | None, since: datetime | None) -> int:
    settings = get_settings()
    library = profiles.load_library(settings)
    logger.info("library rules %s: %s", library.criteria_set, [c.id for c in library.criteria])
    auth = Auth.from_sdk()
    run_id = uuid.uuid4().hex
    store = await open_store(
        settings.lakebase_endpoint,
        settings.lakebase_database,
        settings.lakebase_schema,
        user=settings.lakebase_user,
        require_durable=True,
    )
    failed = 0
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as http:
        sql = SqlClient(http, auth, settings.warehouse_id, max_concurrent=settings.max_concurrent_statements)
        delta = DeltaWriter(sql, settings.table_prefix)
        runner = SkillRunner(http, auth, settings.judge_model, max_concurrent=settings.llm_concurrency)
        await delta.ensure_tables()
        if not space_ids:
            rows = await sql.execute(f"SELECT DISTINCT space_id FROM {settings.table_prefix}.traces")
            space_ids = [r["space_id"] for r in rows]
        for space_id in space_ids:
            try:
                # The space's own rules, judges and models (its profile over the library).
                eff = await profiles.effective(store, library, space_id)
                criteria, version, skills = eff.criteria, eff.criteria_set, eff.rubric_skills()
                live = await delta.latest_space_version(space_id)
                manifest, texts = manifests.build(
                    run_id,
                    "check",
                    space_id,
                    now=datetime.now(UTC),
                    commit=settings.platform_commit,
                    config_versions={"live": live} if live else {},
                    criteria=criteria,
                    criteria_set=version,
                    skills=skills.values(),
                    judge_model=settings.judge_model,
                    settings={"since": since.isoformat() if since else None, **eff.manifest_settings()},
                )
                await manifests.record(delta, manifest, texts)
                stats = await check_space(
                    space_id,
                    delta=delta,
                    store=store,
                    criteria=criteria,
                    criteria_version=version,
                    run_id=run_id,
                    since=since,
                    runner=runner,
                    skills=skills,
                    sample_rate=eff.thresholds["judge_sample_rate"],
                    max_judged=eff.thresholds["max_judged_per_run"],
                    recheck_days=eff.thresholds["recheck_days"],
                )
                logger.info("checked %s", stats)
            except Exception:
                failed += 1
                logger.exception("space %s failed", space_id)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", action="append", dest="overrides", metavar="KEY=VALUE")
    parser.add_argument("--space", action="append", dest="spaces")
    parser.add_argument(
        "--since",
        help="Re-check every trace asked on or after this date (YYYY-MM-DD); default: only new ones.",
    )
    args = parser.parse_args()
    apply_overrides(args.overrides)
    spaces = [space for space in (args.spaces or []) if space] or None  # `--space=` (empty) = all
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None
    finish(run_async(run(spaces, since)))


if __name__ == "__main__":
    main()
