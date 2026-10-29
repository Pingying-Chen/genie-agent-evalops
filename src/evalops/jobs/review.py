"""Entrypoint of the `review` job: human review of the rubric judges (mechanism A).

    evalops-review [--space SPACE_ID ...] [--set KEY=VALUE ...]

Per space: pull labels from the space's MLflow labeling session, reveal the judge's verdict
on what a human labelled, record judge-human agreement, then queue new traces for the
space's registered reviewers. Last, suggest a failure category for each new or changed
observation. Needs the `review` extra (MLflow).
"""

import argparse
import asyncio
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
from evalops.services import drift, evalset, manifests, profiles, splits
from evalops.services.checks.llm_checks import rubric_criteria
from evalops.services.delta_store import DeltaWriter
from evalops.services.failures import suggest_categories
from evalops.services.llm.runner import SkillRunner
from evalops.services.review.backend import VIEW_VERSION

logger = logging.getLogger("evalops.review")


async def run(space_ids: list[str] | None) -> int:
    from evalops.services.review.context import describe_tables, space_context
    from evalops.services.review.expectations import review_expectations
    from evalops.services.review.mlflow_backend import MlflowBackend  # heavy: the review extra only
    from evalops.services.review.service import review_space

    settings = get_settings()
    library = profiles.load_library(settings)
    store = await open_store(
        settings.lakebase_endpoint,
        settings.lakebase_database,
        settings.lakebase_schema,
        user=settings.lakebase_user,
        require_durable=True,
    )
    backend = MlflowBackend(settings.review_experiment_root)
    failed = 0
    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as http:
        auth = Auth.from_sdk()
        sql = SqlClient(http, auth, settings.warehouse_id, max_concurrent=settings.max_concurrent_statements)
        delta = DeltaWriter(sql, settings.table_prefix)
        genie = GenieClient(http, auth)
        runner = SkillRunner(http, auth, settings.judge_model, max_concurrent=settings.llm_concurrency)

        async def load_context(space_id: str) -> str:
            definition = await genie.get_space_definition(space_id)
            return space_context(definition, await describe_tables(sql, http, auth, definition.tables))

        await delta.ensure_tables()
        if not space_ids:
            rows = await sql.execute(f"SELECT DISTINCT space_id FROM {settings.table_prefix}.traces")
            # A space with eval cases but no traffic yet still needs its cases tagged and split.
            space_ids = sorted({r["space_id"] for r in rows} | set(await store.get_eval_case_spaces()))
        run_id = uuid.uuid4().hex
        for space_id in space_ids:
            try:
                # The space's own rules, judges, models and budgets (its profile over the library).
                eff = await profiles.effective(store, library, space_id)
                rubrics, criteria_set = rubric_criteria(eff.criteria), eff.criteria_set
                categorize, intents = eff.skill("categorize_observation"), eff.skill("intent_tagger")
                expectation_judge = eff.skill("expectation_judge")
                t = eff.thresholds
                live = await delta.latest_space_version(space_id)
                manifest, texts = manifests.build(
                    run_id,
                    "review",
                    space_id,
                    now=datetime.now(UTC),
                    commit=settings.platform_commit,
                    config_versions={"live": live} if live else {},
                    criteria=rubrics,  # what reviewers label, and what their labels calibrate
                    criteria_set=criteria_set,
                    skills=[categorize, intents, expectation_judge],
                    guidelines=True,
                    judge_model=settings.judge_model,
                    settings={
                        "review_view": VIEW_VERSION,
                        **eff.manifest_settings(),
                    },
                )
                await manifests.record(delta, manifest, texts)
                stats = await review_space(
                    space_id,
                    delta=delta,
                    store=store,
                    backend=backend,
                    criteria=rubrics,
                    budget=t["review_budget"],
                    min_lower_bound=t["calibration_min_lower_bound"],
                    now=datetime.now(UTC),
                    load_context=load_context,
                    timezone=settings.review_timezone,
                )
                logger.info(
                    "space %s: %d reviewers, %d labels, %d turn reviews, %d judge verdicts revealed, "
                    "%d queued, session %s",
                    space_id,
                    stats.reviewers,
                    stats.labels,
                    stats.trace_reviews,
                    stats.judge_attached,
                    stats.queued,
                    stats.session_url or "-",
                )
                suggested = await suggest_categories(
                    space_id,
                    sql=sql,
                    store=store,
                    delta=delta,
                    prefix=settings.table_prefix,
                    runner=runner,
                    skill=categorize,
                    now=datetime.now(UTC),
                )
                logger.info("space %s: %d failure category suggestions", space_id, suggested)
                tagged = await drift.tag_intents(
                    space_id,
                    sql=sql,
                    store=store,
                    delta=delta,
                    prefix=settings.table_prefix,
                    runner=runner,
                    skill=intents,
                    window_days=t["drift_window_days"],
                    now=datetime.now(UTC),
                )
                logger.info("space %s: %d questions and cases tagged with an intent", space_id, tagged)
                # Now that new cases have an intent, they can be stratified.
                assigned = await splits.ensure_assigned(
                    space_id,
                    await evalset.cases(store, space_id),
                    sql=sql,
                    store=store,
                    prefix=settings.table_prefix,
                    now=datetime.now(UTC),
                )
                logger.info("space %s: %d eval cases have a dev/test split", space_id, len(assigned))
                experiment_id = await asyncio.to_thread(backend.ensure_experiment, space_id)
                measured, queued = await review_expectations(
                    space_id,
                    delta=delta,
                    store=store,
                    backend=backend,
                    experiment_id=experiment_id,
                    assessments=await asyncio.to_thread(backend.assessments, experiment_id),
                    reviewers=await store.get_reviewers(space_id),
                    judge=expectation_judge,
                    model=eff.model_of(expectation_judge),
                    budget=t["expectation_review_budget"],
                    min_lower_bound=t["calibration_min_lower_bound"],
                    now=datetime.now(UTC),
                )
                logger.info(
                    "space %s: expectation_judge n=%s TPR LB %s TNR LB %s; %d answers queued",
                    space_id,
                    measured.n if measured else 0,
                    measured.tpr_lb if measured else None,
                    measured.tnr_lb if measured else None,
                    queued,
                )
                for a in stats.agreement or []:
                    logger.info(
                        "  %s v%d [%s]: n=%d TPR=%s (LB %s) TNR=%s (LB %s) kappa=%s calibrated=%s",
                        a.criterion_id,
                        a.criterion_version,
                        a.judge,
                        a.n,
                        a.tpr,
                        a.tpr_lb,
                        a.tnr,
                        a.tnr_lb,
                        a.kappa,
                        a.calibrated,
                    )
            except Exception:
                failed += 1
                logger.exception("space %s failed", space_id)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", action="append", dest="overrides", metavar="KEY=VALUE")
    parser.add_argument("--space", action="append", dest="spaces")
    args = parser.parse_args()
    apply_overrides(args.overrides)
    finish(run_async(run([s for s in (args.spaces or []) if s] or None)))


if __name__ == "__main__":
    main()
