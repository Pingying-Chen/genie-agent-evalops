"""Version manifests.

A run's numbers depend on more than the agent: the rules, the judges' prompts, the reviewer
guidelines, the cases, the settings and the platform's own code. Each run records all of them.
Texts are stored once, keyed by their content hash, so a manifest stays small and any run can
be traced back to the exact rule, prompt or guideline it used.

A skill is versioned by hand in its front matter. If its text changes and the version does not,
two runs would claim the same `rubric_judge@4` with different prompts, and calibration (which is
per skill version) would silently mix them. `record` refuses that: the job fails.
"""

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from evalops.errors import ConfigError
from evalops.schemas.checks import Criterion, Family
from evalops.schemas.manifest import (
    ArtifactChange,
    ArtifactRef,
    DriftFlag,
    FieldChange,
    Manifest,
    ManifestDiff,
)
from evalops.services.delta_store import DeltaWriter
from evalops.services.llm.skills import Skill
from evalops.services.review.backend import (
    FINDINGS,
    FINDINGS_INSTRUCTION,
    FINDINGS_TITLE,
    TASK_SUCCESS,
    TASK_SUCCESS_INSTRUCTION,
    TASK_SUCCESS_TITLE,
    label_instruction,
)
from evalops.services.review.selection import schema_name

DRIFT_LABEL = "weekly drift check"
Texts = dict[str, tuple[ArtifactRef, str]]  # hash -> (ref, text)


def content_hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=1, default=str)


def _artifact(kind: str, name: str, version: str, text: str) -> tuple[ArtifactRef, str]:
    return ArtifactRef(kind=kind, name=name, version=version, hash=content_hash(text)), text


def criterion_artifact(criterion: Criterion) -> tuple[ArtifactRef, str]:
    return _artifact(
        "criterion", criterion.id, str(criterion.version), _canonical(criterion.model_dump(mode="json"))
    )


def skill_artifact(skill: Skill) -> tuple[ArtifactRef, str]:
    """Everything that shapes what the skill returns: the prompt, the model settings, the schemas."""
    text = _canonical(
        {
            "name": skill.name,
            "version": skill.version,
            "purpose": skill.purpose,
            "model": skill.model_params,
            "input_schema": skill.input_schema.__name__,
            "output_schema": skill.output_schema.__name__,
            "template": skill.template,
        }
    )
    # The version includes a space's override (`5~s2`): its text may differ from the library's.
    return _artifact("skill", skill.name, skill.ref.split("@", 1)[1], text)


def guideline_artifacts(criteria: Iterable[Criterion]) -> list[tuple[ArtifactRef, str]]:
    """What reviewers read next to each label: the turn-level questions and one per rubric rule.
    The label schema name carries the version (MLflow schemas are immutable once used)."""

    def one(name: str, title: str, instruction: str) -> tuple[ArtifactRef, str]:
        version = name.rpartition("__v")[2] or "1"
        return _artifact("guideline", name, version, f"{title}\n\n{instruction}")

    out = [
        one(TASK_SUCCESS, TASK_SUCCESS_TITLE, TASK_SUCCESS_INSTRUCTION),
        one(FINDINGS, FINDINGS_TITLE, FINDINGS_INSTRUCTION),
    ]
    out += [
        one(schema_name(c.id, c.version), c.title or c.id, label_instruction(c))
        for c in criteria
        if c.family == Family.RUBRIC
    ]
    return out


def build(
    run_id: str,
    run_kind: str,
    space_id: str,
    *,
    now: datetime,
    commit: str = "",
    config_versions: dict[str, str] | None = None,
    criteria: list[Criterion] = (),
    criteria_set: str | None = None,
    skills: Iterable[Skill] = (),
    guidelines: bool = False,
    judge_model: str | None = None,
    cases: list[dict[str, Any]] | None = None,
    settings: dict[str, Any] | None = None,
) -> tuple[Manifest, Texts]:
    pairs = [criterion_artifact(c) for c in criteria]
    pairs += [skill_artifact(s) for s in sorted(skills, key=lambda s: s.name)]
    if guidelines:
        pairs += guideline_artifacts(criteria)
    manifest = Manifest(
        run_id=run_id,
        run_kind=run_kind,
        space_id=space_id,
        created_at=now,
        platform_commit=commit or None,
        config_versions=config_versions or {},
        criteria_set=criteria_set,
        artifacts=[ref for ref, _ in pairs],
        judge_model=judge_model,
        cases=cases or [],
        settings=settings or {},
    )
    return manifest, {ref.hash: (ref, text) for ref, text in pairs}


def unbumped_skills(texts: Texts, stored: list[dict]) -> list[str]:
    """Skills whose text differs from a stored text under the same name and version."""
    seen = {(r["name"], r["version"]): r["hash"] for r in stored}
    return sorted(
        f"{ref.name}@{ref.version}"
        for ref, _ in texts.values()
        if ref.kind == "skill" and seen.get((ref.name, ref.version), ref.hash) != ref.hash
    )


async def record(delta: DeltaWriter, manifest: Manifest, texts: Texts, label: str = "") -> None:
    """Store the texts (once each) and the manifest. Call at the start of a run: a skill edited
    without a version bump fails the job before it produces numbers under a wrong name."""
    skill_names = sorted({ref.name for ref, _ in texts.values() if ref.kind == "skill"})
    if skill_names:
        bad = unbumped_skills(texts, await delta.load_artifact_refs("skill", skill_names))
        if bad:
            raise ConfigError(
                f"Skill text changed without a version bump: {', '.join(bad)}. "
                "Raise `version` in its SKILL.md front matter."
            )
    await delta.merge_artifacts(
        [
            {
                "hash": ref.hash,
                "kind": ref.kind,
                "name": ref.name,
                "version": ref.version,
                "content": text,
                "first_seen_at": manifest.created_at.isoformat(),
            }
            for ref, text in texts.values()
        ]
    )
    await delta.merge_manifest(
        {
            "run_id": manifest.run_id,
            "space_id": manifest.space_id,
            "run_kind": manifest.run_kind,
            "created_at": manifest.created_at.isoformat(),
            "label": label or None,
            "manifest": manifest.model_dump_json(),
        }
    )


def _case_key(c: dict) -> tuple:
    return c.get("version"), c.get("split")


def diff(a: Manifest, b: Manifest, texts: dict[str, str]) -> ManifestDiff:
    """Pure: what differs from run `a` to run `b`. `texts` maps hashes to stored text."""
    fields = [
        FieldChange(field=name, before=before, after=after)
        for name, before, after in (
            ("platform_commit", a.platform_commit, b.platform_commit),
            ("criteria_set", a.criteria_set, b.criteria_set),
            ("judge_model", a.judge_model, b.judge_model),
        )
        if before != after
    ]
    for arm in sorted(set(a.config_versions) | set(b.config_versions)):
        before, after = a.config_versions.get(arm), b.config_versions.get(arm)
        if before != after:
            fields.append(FieldChange(field=f"config_versions.{arm}", before=before, after=after))
    for key in sorted(set(a.settings) | set(b.settings)):
        before, after = a.settings.get(key), b.settings.get(key)
        if before != after:
            fields.append(FieldChange(field=f"settings.{key}", before=before, after=after))

    before_refs = {(r.kind, r.name): r for r in a.artifacts}
    after_refs = {(r.kind, r.name): r for r in b.artifacts}
    artifacts = []
    for key in sorted(set(before_refs) | set(after_refs)):
        old, new = before_refs.get(key), after_refs.get(key)
        if old and new and old.hash == new.hash:
            continue
        artifacts.append(
            ArtifactChange(
                kind=key[0],
                name=key[1],
                change="added" if old is None else "removed" if new is None else "changed",
                before_version=old.version if old else None,
                after_version=new.version if new else None,
                before=texts.get(old.hash) if old else None,
                after=texts.get(new.hash) if new else None,
            )
        )

    cases_a = {c["case_id"]: c for c in a.cases}
    cases_b = {c["case_id"]: c for c in b.cases}
    return ManifestDiff(
        a=a,
        b=b,
        fields=fields,
        artifacts=artifacts,
        cases_added=sorted(set(cases_b) - set(cases_a)),
        cases_removed=sorted(set(cases_a) - set(cases_b)),
        cases_changed=sorted(
            cid for cid in set(cases_a) & set(cases_b) if _case_key(cases_a[cid]) != _case_key(cases_b[cid])
        ),
    )


def same_inputs(a: Manifest, b: Manifest) -> bool:
    """Everything we control is the same (the commit aside, which is reported separately)."""
    return (
        a.config_versions == b.config_versions
        and a.criteria_set == b.criteria_set
        and {(r.kind, r.name, r.hash) for r in a.artifacts} == {(r.kind, r.name, r.hash) for r in b.artifacts}
        and a.judge_model == b.judge_model
        and sorted(map(_case_key_full, a.cases)) == sorted(map(_case_key_full, b.cases))
        and a.settings == b.settings
    )


def _case_key_full(c: dict) -> tuple:
    return c["case_id"], c.get("version"), c.get("split")


def drift_flags(checks: list[tuple[Manifest, dict[str, str]]]) -> list[DriftFlag]:
    """Pure: scheduled drift-check runs of one space, oldest first, each with its baseline outcome
    per case (`pass` = k of k passed, `fail` = k of k failed, anything else ignored). Consecutive
    runs on the same inputs whose decided outcomes flip are flagged: Genie itself changed."""
    flags = []
    for (a, out_a), (b, out_b) in zip(checks, checks[1:], strict=False):
        if not same_inputs(a, b):
            continue
        flipped = sorted(
            cid for cid in set(out_a) & set(out_b) if {out_a[cid], out_b[cid]} == {"pass", "fail"}
        )
        if flipped:
            note = (
                f"platform code also changed ({a.platform_commit} → {b.platform_commit})"
                if a.platform_commit != b.platform_commit
                else ""
            )
            flags.append(DriftFlag(earlier_run=a.run_id, later_run=b.run_id, flipped=flipped, note=note))
    return flags
