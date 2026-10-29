"""Read side of version manifests: runs over time with what
produced them, the agent's configuration history, a diff of any two runs, and drift flags from
the scheduled gate runs."""

from evalops.clients.sql import SqlClient
from evalops.errors import NotFound
from evalops.schemas.manifest import Manifest, ManifestDiff, ManifestRow, SpaceVersionRow, Versions
from evalops.services import manifests
from evalops.services.regression.results import graded_from_row, outcome


async def _drift(space_id: str, *, sql: SqlClient, prefix: str) -> list:
    rows = await sql.execute(
        f"""SELECT manifest FROM {prefix}.run_manifests
            WHERE space_id = :space_id AND run_kind = 'eval' AND label = :label ORDER BY created_at""",
        {"space_id": space_id, "label": manifests.DRIFT_LABEL},
    )
    checks = [Manifest.model_validate_json(r["manifest"]) for r in rows]
    if len(checks) < 2:
        return []
    results = await sql.execute(
        f"""SELECT r.* FROM {prefix}.eval_results r
            JOIN {prefix}.eval_runs u USING (run_id)
            WHERE r.space_id = :space_id AND r.arm = 'baseline' AND u.label = :label""",
        {"space_id": space_id, "label": manifests.DRIFT_LABEL},
    )
    by_run: dict[str, dict[str, list]] = {}
    for r in results:
        by_run.setdefault(r["run_id"], {}).setdefault(r["case_id"], []).append(graded_from_row(r))
    decided = []
    for m in checks:
        per_case = {}
        for case_id, attempts in by_run.get(m.run_id, {}).items():
            o = outcome(attempts)
            if not o.errors and not o.flaky:
                per_case[case_id] = "pass" if o.pass_all else "fail"
        decided.append((m, per_case))
    return manifests.drift_flags(decided)


async def versions(space_id: str, *, sql: SqlClient, prefix: str, limit: int = 50) -> Versions:
    rows = await sql.execute(
        f"""SELECT label, manifest FROM {prefix}.run_manifests WHERE space_id = :space_id
            ORDER BY created_at DESC LIMIT {int(limit)}""",
        {"space_id": space_id},
    )
    runs = []
    for r in rows:
        m = Manifest.model_validate_json(r["manifest"])
        runs.append(
            ManifestRow(
                run_id=m.run_id,
                run_kind=m.run_kind,
                created_at=m.created_at,
                label=r.get("label"),
                platform_commit=m.platform_commit,
                config_versions=m.config_versions,
                criteria_set=m.criteria_set,
                judge_model=m.judge_model,
                artifacts=len(m.artifacts),
                cases=len(m.cases),
            )
        )
    history = await sql.execute(
        f"""SELECT version, captured_at, previous_version FROM {prefix}.space_versions
            WHERE space_id = :space_id ORDER BY captured_at DESC LIMIT {int(limit)}""",
        {"space_id": space_id},
    )
    return Versions(
        space_id=space_id,
        runs=runs,
        config_history=[SpaceVersionRow.model_validate(h) for h in history],
        drift=await _drift(space_id, sql=sql, prefix=prefix),
    )


async def _manifest(space_id: str, run_id: str, *, sql: SqlClient, prefix: str) -> Manifest:
    rows = await sql.execute(
        f"SELECT manifest FROM {prefix}.run_manifests WHERE space_id = :space_id AND run_id = :run_id",
        {"space_id": space_id, "run_id": run_id},
    )
    if not rows:
        raise NotFound("No manifest for that run in this space.")
    return Manifest.model_validate_json(rows[0]["manifest"])


async def diff(space_id: str, a: str, b: str, *, sql: SqlClient, prefix: str) -> ManifestDiff:
    """Both runs must belong to the space; only texts those two manifests name are returned."""
    first = await _manifest(space_id, a, sql=sql, prefix=prefix)
    second = await _manifest(space_id, b, sql=sql, prefix=prefix)
    hashes = sorted({r.hash for r in first.artifacts} ^ {r.hash for r in second.artifacts})
    texts = {}
    if hashes:
        marks = ", ".join(f":h{i}" for i in range(len(hashes)))
        rows = await sql.execute(
            f"SELECT hash, content FROM {prefix}.artifacts WHERE hash IN ({marks})",
            {f"h{i}": h for i, h in enumerate(hashes)},
        )
        texts = {r["hash"]: r["content"] for r in rows}
    return manifests.diff(first, second, texts)
