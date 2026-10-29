"""Entrypoint of the `evaluate` job: a regression run of a space's eval set.

    evalops-evaluate --space SPACE_ID [--split dev|test|all] [--change CHANGE_ID] [--label TEXT]
                     [--scope JSON] [--set KEY=VALUE ...]

As the collector: assign a dev/test split to every active case that has none,
read the space's configuration and the cases of the chosen split (on a dev run, optionally only
a slice, the change's targets or chosen cases: `--scope`, a `RunScope`), write them to a
run folder shared with the tester, run the `ask` job (as the tester) and wait, then grade every
answer with the criteria and the cases' expectations, and store the run in Delta. With
`--change`, a second arm asks the same cases on the configuration with that change applied, at
the same time. The scratch spaces live and die inside the `ask` job.
"""

import argparse
import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.iam import AccessControlRequest, PermissionLevel

from evalops.clients.auth import Auth
from evalops.clients.genie import GenieClient
from evalops.clients.lakebase import open_store
from evalops.clients.sql import SqlClient
from evalops.clients.workspace_folder import WorkspaceFolder
from evalops.config import apply_overrides, get_settings
from evalops.errors import ConfigError
from evalops.jobs import finish, run_async
from evalops.schemas.evaluation import RunScope, check_scope
from evalops.services import evalset, evaluators, manifests, profiles, space_changes, splits
from evalops.services.delta_store import DeltaWriter
from evalops.services.llm.runner import SkillRunner
from evalops.services.regression import results as run_results
from evalops.services.regression import runner as regression
from evalops.services.regression.changes import apply as apply_change
from evalops.services.regression.leakage import added_examples, leaks

logger = logging.getLogger("evalops.evaluate")


def share_folder(w: WorkspaceClient, path: str, tester: str) -> None:
    """Create the run folder and let the tester write in it (a workspace ACL, not a data grant)."""
    w.workspace.mkdirs(path)
    folder = w.workspace.get_status(path)
    w.permissions.update(
        "directories",
        str(folder.object_id),
        access_control_list=[
            AccessControlRequest(service_principal_name=tester, permission_level=PermissionLevel.CAN_MANAGE)
        ],
    )


async def run(
    space_id: str, label: str, change_id: str = "", split: str = "dev", scope: RunScope | None = None
) -> int:
    settings = get_settings()
    try:
        check_scope(split, scope, has_change=bool(change_id))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if not settings.tester_job_id or not settings.tester_principal:
        raise ConfigError("tester_job_id and tester_principal must be set (the `ask` job and its identity).")
    library = profiles.load_library(settings)
    w = WorkspaceClient()
    me = w.current_user.me().user_name
    auth = Auth.from_sdk(w)
    store = await open_store(
        settings.lakebase_endpoint,
        settings.lakebase_database,
        settings.lakebase_schema,
        user=settings.lakebase_user,
        require_durable=True,
    )
    # The space's own rules, judges, models, k and gate (its profile over the library).
    eff = await profiles.effective(store, library, space_id)
    criteria, criteria_version = eff.criteria, eff.criteria_set
    run_id = uuid.uuid4().hex
    base = (settings.eval_runs_dir or f"/Workspace/Users/{me}/evalops-runs").rstrip("/")
    folder = WorkspaceFolder(w, f"{base}/{run_id}")
    started = datetime.now(UTC)

    async with httpx.AsyncClient(timeout=settings.http_timeout_seconds) as http:
        sql = SqlClient(http, auth, settings.warehouse_id, max_concurrent=settings.max_concurrent_statements)
        delta = DeltaWriter(sql, settings.table_prefix)
        await delta.ensure_tables()
        change = await space_changes.get_change(store, space_id, change_id) if change_id else None
        every = await evalset.cases(store, space_id)
        split_of = await splits.ensure_assigned(
            space_id, every, sql=sql, store=store, prefix=settings.table_prefix, now=started
        )
        values = (
            await splits.values_for(space_id, every, sql=sql, store=store, prefix=settings.table_prefix)
            if scope and scope.dimension
            else None
        )
        targets = set(change.target_case_ids) if change else set()
        cases = splits.select(every, split_of, split, targets, scope, values)
        if not cases:
            logger.info("space %s has no active %s cases in %s: nothing to run", space_id, split, scope)
            return 0
        config = await GenieClient(http, auth).get_space_config(space_id)
        arms = [regression.arm("baseline", config)]
        if change:
            if change.base_version != config.version:
                logger.warning(
                    "change %s was written against %s; the space is now %s",
                    change_id,
                    change.base_version,
                    config.version,
                )
            candidate = config.model_copy(
                update={"serialized_space": apply_change(config.serialized_space, change.operations)}
            )
            arms.append(regression.arm("candidate", candidate))
        request = regression.build_request(
            run_id,
            space_id,
            arms,
            cases,
            k=eff.thresholds["eval_k"],
            concurrency=settings.eval_concurrency,
            forbidden_table=settings.eval_forbidden_table,
        )
        rubric_skills = eff.rubric_skills()
        judge = eff.skill("expectation_judge")
        asked = [
            {
                "case_id": c.case_id,
                "version": c.version,
                "split": split_of[c.case_id].split if c.case_id in split_of else None,
            }
            for c in cases
        ]
        run_label = label or (change.title if change else "")
        registry = await evaluators.evaluators(
            space_id,
            criteria,
            sql=sql,
            store=store,
            prefix=settings.table_prefix,
            judges=eff.judges(),
            min_lower_bound=eff.thresholds["calibration_min_lower_bound"],
            expectation_judge_version=str(judge.version),
        )
        manifest, texts = manifests.build(
            run_id,
            "eval",
            space_id,
            now=started,
            commit=settings.platform_commit,
            config_versions={a.arm: a.config_version for a in request.arms},
            criteria=criteria,
            criteria_set=criteria_version,
            skills=[*rubric_skills.values(), judge],
            judge_model=settings.judge_model,
            cases=asked,
            settings={
                "k": request.k,
                "split": split,
                "scope": scope.model_dump() if scope else None,
                "split_settings": (await splits.get_settings(store, space_id)).model_dump(),
                "change_id": change_id or None,
                # Who may decide this run's gate, as of now: a later calibration does not change it.
                "evaluators": {e.id: e.status for e in registry.evaluators},
                # The gate as configured now: a later profile change does not change this verdict.
                "gate": eff.gate.model_dump(mode="json"),
                **eff.manifest_settings(),
            },
        )
        await manifests.record(delta, manifest, texts, run_label)
        row = {
            "run_id": run_id,
            "space_id": space_id,
            "label": run_label,
            "change_id": change_id or None,
            "k": request.k,
            "arms": json.dumps([{"arm": a.arm, "config_version": a.config_version} for a in request.arms]),
            "split": split,
            "scope": scope.describe() if scope else None,
            "cases": json.dumps(asked),
            "status": "running",
            "tester": settings.tester_principal,
            "started_at": started.isoformat(),
            "criteria_version": criteria_version,
        }
        await delta.merge_eval_run(row)
        await asyncio.to_thread(share_folder, w, folder.path, settings.tester_principal)
        await folder.write("request.json", request.model_dump_json())
        logger.info(
            "run %s: %d %s cases x %d on %s, folder %s",
            run_id,
            len(cases),
            split,
            request.k,
            config.version,
            folder.path,
        )

        error = None
        try:  # the ask job's own failure is reported from done.json; this only waits for it
            await asyncio.to_thread(
                lambda: w.jobs.run_now(
                    settings.tester_job_id, job_parameters={"run_dir": folder.path}
                ).result(timeout=timedelta(minutes=settings.eval_timeout_minutes))
            )
        except Exception as exc:
            error = f"ask job: {exc}"[:1000]
            logger.warning("%s", error)

        preflight, done, attempts = await regression.read_back(folder)
        if done is None:
            error = error or "the ask job ended without writing done.json"
        elif done.error:
            error = done.error
        llm = SkillRunner(http, auth, settings.judge_model, max_concurrent=settings.llm_concurrency)
        graded = await regression.grade(
            request,
            attempts,
            {c.case_id: c for c in cases},
            criteria=criteria,
            runner=llm,
            rubric_skills=rubric_skills,
            judge_skill=judge,
            now=datetime.now(UTC),
        )
        await delta.write_eval_results(graded)
        decision = None
        if change and graded:
            # Decided now, on the eval set and change as they are now: a later edit cannot change it.
            titles = {c.case_id: c.title for c in every}
            expected = {a["case_id"]: a["split"] for a in asked}
            decision = run_results.decide(
                run_results.case_runs(graded, titles, {k: v for k, v in expected.items() if v}),
                targets=targets,
                leaked={leak.case_id for leak in leaks(added_examples(change.operations), every)},
                decides=split == "test",
                undecidable=evaluators.undecidable({e.id: e.status for e in registry.evaluators}),
                gate=eff.gate,
                types={c.case_id: c.type for c in every},
                expected=expected,
                k=request.k,
                titles=titles,
            )
        finished = {
            **row,
            # partial: the ask job died midway; its answers are kept but it never decides a gate.
            "status": "failed" if error and not graded else "partial" if error else "succeeded",
            "preflight": preflight.model_dump_json() if preflight else None,
            "error": error,
            "finished_at": datetime.now(UTC).isoformat(),
            "n_results": len(graded),
            "decision": decision.model_dump_json() if decision else None,
        }
        await delta.merge_eval_run(finished)
        if change and graded and change.status in ("draft", "tested"):
            await space_changes.set_status(store, space_id, change.change_id, "tested")
    if graded:  # the answers are in Delta now; the folder holds copies the tester can still reach
        await asyncio.to_thread(w.workspace.delete, folder.path, recursive=True)
    counts = {v: sum(1 for g in graded if g.verdict == v) for v in ("pass", "fail", "error")}
    logger.info("run %s graded %d answers %s; error: %s", run_id, len(graded), counts, error)
    if done and not done.deleted:
        logger.error("scratch spaces were not all deleted: %s", done.scratch_spaces)
    return 1 if error and not graded else 0


async def run_all(label: str, split: str) -> int:
    """`--space all`: every space that has eval cases, one after another, without a change (the
    weekly drift check). One space failing does not stop the others."""
    settings = get_settings()
    store = await open_store(
        settings.lakebase_endpoint,
        settings.lakebase_database,
        settings.lakebase_schema,
        user=settings.lakebase_user,
        require_durable=True,
    )
    failed = 0
    for space_id in await store.get_eval_case_spaces():
        try:
            failed += await run(space_id, label, "", split)
        except Exception:
            failed += 1
            logger.exception("space %s failed", space_id)
    return 1 if failed else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", action="append", dest="overrides", metavar="KEY=VALUE")
    parser.add_argument("--space", required=True, help="A space id, or `all`: every space with eval cases.")
    parser.add_argument("--label", default="")
    parser.add_argument(
        "--split",
        default="dev",
        choices=["dev", "test", "all"],
        help="dev = iterate; test = gate run (test cases plus the change's targets); all = every active case.",
    )
    parser.add_argument(
        "--change", default="", help="A space change to test against the current configuration."
    )
    parser.add_argument(
        "--scope",
        default="",
        help='Dev runs only, part of the eval set as JSON: {"dimension": "type", "value": "guardrail"} '
        'or {"targets_only": true}. Empty = the whole split.',
    )
    args = parser.parse_args()
    apply_overrides(args.overrides)
    if args.space == "all":
        if args.change:
            parser.error("--change needs one --space")
        finish(run_async(run_all(args.label, args.split or "test")))
    else:
        scope = RunScope.model_validate_json(args.scope) if args.scope.strip() else None
        finish(run_async(run(args.space, args.label, args.change, args.split or "dev", scope)))


if __name__ == "__main__":
    main()
