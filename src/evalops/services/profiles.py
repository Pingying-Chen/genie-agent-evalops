"""Space profiles: the library the deployment ships, what one
space changes about it, and the effective configuration every job and page uses for that space.

Saving is version control: each save is a new version (who, when, a note), nothing is
overwritten, a stale edit is refused, and any version can be restored by saving it again. A
profile that changes what a skill or a custom rule returns must also give it a new version,
because judges are calibrated per version: the same version with different behaviour would mix
two judges' agreement with people.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from evalops.clients.lakebase import StateStore
from evalops.config import Settings
from evalops.errors import Conflict, NotFound, ValidationFailed
from evalops.schemas.checks import Criterion, Family
from evalops.schemas.evalset import SplitSettings
from evalops.schemas.profile import (
    EffectiveProfile,
    EffectiveRule,
    EffectiveSkill,
    GateConfig,
    ProfileBody,
    ProfileDiff,
    ProfileSave,
    SpaceProfile,
)
from evalops.services.criteria import criteria_folders, load_criteria
from evalops.services.evaluators import EXPECTATION_JUDGE, EXPECTATIONS_LABEL
from evalops.services.llm.skills import Skill, load_skill, skills_dir
from evalops.services.review.agreement import judge_fingerprint

SKILLS = (
    "rubric_judge",
    "expectation_judge",
    "categorize_observation",
    "intent_tagger",
    "space_change_proposer",
)
THRESHOLDS = (
    "eval_k",
    "calibration_min_lower_bound",
    "review_budget",
    "expectation_review_budget",
    "drift_window_days",
    "drift_min_questions",
    "judge_sample_rate",
    "max_judged_per_run",
    "recheck_days",
)
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass
class Library:
    """What the deployment ships: rule definitions, skills and default settings."""

    criteria: list[Criterion]
    criteria_set: str
    skills_folder: Path
    settings: Settings
    _skills: dict[str, Skill] = field(default_factory=dict)

    def skill(self, name: str) -> Skill:
        if name not in self._skills:
            self._skills[name] = load_skill(name, self.skills_folder)
        return self._skills[name]


def load_library(settings: Settings) -> Library:
    """The library as the jobs and the app load it from the deployment's folders."""
    criteria, version = load_criteria(
        *criteria_folders(settings.criteria_dir, settings.extra_criteria_dir, settings.root_dir)
    )
    return Library(criteria, version, skills_dir(settings.skills_dir), settings)


@dataclass
class Effective:
    """One space's configuration: the library with its profile applied."""

    space_id: str
    profile: SpaceProfile
    criteria: list[Criterion]
    criteria_set: str
    skills: dict[str, Skill]
    thresholds: dict[str, Any]
    gate: GateConfig
    split: SplitSettings | None
    default_model: str

    def skill(self, name: str) -> Skill:
        return self.skills[name]

    def fingerprint(self, skill: Skill) -> str:
        return judge_fingerprint(skill.ref, self.model_of(skill), skill.model_params)

    def judges(self) -> dict[str, str]:
        """The judge behind each LLM-judged rule, and the expectation judge's under its agreement
        label: a calibration counts only if it measured exactly this judge."""
        out = {
            c.id: self.fingerprint(self.skills[name])
            for c in self.criteria
            if c.family == Family.RUBRIC and (name := c.params.get("skill", "rubric_judge")) in self.skills
        }
        if EXPECTATION_JUDGE in self.skills:
            out[EXPECTATIONS_LABEL] = self.fingerprint(self.skills[EXPECTATION_JUDGE])
        return out

    def rubric_skills(self) -> dict[str, Skill]:
        names = {c.params.get("skill", "rubric_judge") for c in self.criteria if c.family == Family.RUBRIC}
        return {n: self.skills[n] for n in names if n in self.skills}

    def model_of(self, skill: Skill) -> str:
        return skill.endpoint or self.default_model

    def manifest_settings(self) -> dict[str, Any]:
        """What a run's manifest records about the space's profile."""
        return {
            "profile_version": self.profile.version,
            "skill_models": {name: self.model_of(s) for name, s in sorted(self.skills.items())},
            "thresholds": self.thresholds,
        }


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode()).hexdigest()[:12]


def effective_skill(base: Skill, setting) -> Skill:
    if setting is None:
        return base
    params = dict(base.model_params)
    for key in ("temperature", "max_tokens", "samples", "reasoning_effort"):
        value = getattr(setting, key)
        if value is not None:
            params[key] = value
    return Skill(
        name=base.name,
        version=base.version,
        purpose=base.purpose,
        template=(setting.prompt.strip() + "\n") if setting.prompt else base.template,
        input_schema=base.input_schema,
        output_schema=base.output_schema,
        model_params=params,
        endpoint=setting.model or None,
        variant=f"s{setting.version}" if setting.changes_output() else "",
    )


def resolve(library: Library, profile: SpaceProfile) -> Effective:
    """Pure: the library with the profile applied."""
    criteria = []
    for c in library.criteria:
        setting = profile.rules.get(c.id)
        if setting is None:
            criteria.append(c)
            continue
        if not setting.enabled:
            continue
        update: dict[str, Any] = {}
        if setting.gate is not None:
            update["gate"] = setting.gate
        if setting.params and c.family != Family.RUBRIC:
            update["params"] = {**c.params, **setting.params}
        criteria.append(c.model_copy(update=update) if update else c)
    criteria += profile.custom_rules
    criteria.sort(key=lambda c: c.id)
    defaults = library.settings
    thresholds = {
        name: getattr(profile.thresholds, name)
        if getattr(profile.thresholds, name) is not None
        else getattr(defaults, name)
        for name in THRESHOLDS
    }
    return Effective(
        space_id=profile.space_id,
        profile=profile,
        criteria=criteria,
        # Unchanged library rules keep the library's hash, so a space without a profile compares
        # equal with runs from before profiles existed.
        criteria_set=library.criteria_set
        if not profile.rules and not profile.custom_rules
        else _hash([c.model_dump(mode="json") for c in criteria]),
        skills={name: effective_skill(library.skill(name), profile.skills.get(name)) for name in SKILLS},
        thresholds=thresholds,
        gate=profile.gate,
        split=profile.split,
        default_model=defaults.judge_model,
    )


async def current(store: StateStore, space_id: str) -> SpaceProfile:
    history = await store.get_profile_history(space_id)
    if not history:
        return SpaceProfile(space_id=space_id, version=0)
    latest = history[-1]
    return SpaceProfile(
        **latest["body"],
        space_id=space_id,
        version=latest["version"],
        note=latest["note"],
        updated_by=latest["updated_by"],
        updated_at=latest["updated_at"],
    )


async def effective(store: StateStore, library: Library, space_id: str) -> Effective:
    return resolve(library, await current(store, space_id))


def validate(library: Library, body: ProfileBody, previous: SpaceProfile) -> None:
    """Refuse what would silently change a judge without a new version, or name things the
    library does not have."""
    library_ids = {c.id: c for c in library.criteria}
    unknown = sorted(set(body.rules) - set(library_ids))
    if unknown:
        raise ValidationFailed(
            f"Unknown rules: {', '.join(unknown)}. Rule settings apply to the library's rules."
        )
    for rule_id, setting in body.rules.items():
        if setting.params and library_ids[rule_id].family == Family.RUBRIC:
            raise ValidationFailed(
                f"{rule_id} is an LLM-judged rule: change its wording by adding a custom rule with its own version."
            )
    clashes = sorted({c.id for c in body.custom_rules} & set(library_ids))
    if clashes:
        raise ValidationFailed(f"Custom rule ids already used by the library: {', '.join(clashes)}.")
    for c in body.custom_rules:
        if not str(c.params.get("rule", "")).strip():
            raise ValidationFailed(f"Custom rule {c.id} needs params.rule: what it checks.")
        if c.params.get("skill", "rubric_judge") not in SKILLS:
            raise ValidationFailed(f"Custom rule {c.id} names an unknown skill.")
    before_rules = {c.id: c for c in previous.custom_rules}
    for c in body.custom_rules:
        old = before_rules.get(c.id)
        if old and old.version == c.version and old.model_dump(mode="json") != c.model_dump(mode="json"):
            raise ValidationFailed(
                f"Custom rule {c.id} changed but kept version {c.version}: raise its version "
                "(reviewers' labels and the judge's calibration are per version)."
            )
    unknown_skills = sorted(set(body.skills) - set(SKILLS))
    if unknown_skills:
        raise ValidationFailed(f"Unknown skills: {', '.join(unknown_skills)}.")
    for name, setting in body.skills.items():
        if setting.changes_output() and setting.version is None:
            raise ValidationFailed(
                f"{name}: a prompt or sampling change needs a version (calibration is per version)."
            )
        if setting.prompt:
            needed = set(_PLACEHOLDER.findall(library.skill(name).template))
            missing = sorted(needed - set(_PLACEHOLDER.findall(setting.prompt)))
            if missing:
                raise ValidationFailed(
                    f"{name}: the prompt must keep {', '.join('{' + m + '}' for m in missing)}."
                )
        old = previous.skills.get(name)
        output_fields = ("prompt", "temperature", "max_tokens", "samples", "reasoning_effort")
        if (
            old
            and setting.version is not None
            and old.version == setting.version
            and any(getattr(old, f) != getattr(setting, f) for f in output_fields)
        ):
            raise ValidationFailed(
                f"{name}: the override changed but kept version {setting.version}: raise it."
            )


async def save(
    store: StateStore, library: Library, space_id: str, body: ProfileSave, user: str
) -> SpaceProfile:
    previous = await current(store, space_id)
    if body.base_version != previous.version:
        raise Conflict(
            f"The profile is at version {previous.version}; you edited version {body.base_version}. Reload it."
        )
    content = ProfileBody.model_validate(body.model_dump(exclude={"base_version", "note"}))
    validate(library, content, previous)
    row = {
        "version": previous.version + 1,
        "body": content.model_dump(mode="json"),
        "note": body.note.strip() or "",
        "updated_by": user,
        "updated_at": datetime.now(UTC),
    }
    await store.add_profile_version(space_id, row)
    return await current(store, space_id)


async def history(store: StateStore, space_id: str) -> list[SpaceProfile]:
    return [
        SpaceProfile(
            **r["body"],
            space_id=space_id,
            version=r["version"],
            note=r["note"],
            updated_by=r["updated_by"],
            updated_at=r["updated_at"],
        )
        for r in reversed(await store.get_profile_history(space_id))
    ]


async def restore(
    store: StateStore, library: Library, space_id: str, version: int, user: str
) -> SpaceProfile:
    old = next((p for p in await history(store, space_id) if p.version == version), None)
    if old is None:
        raise NotFound("No such profile version.")
    latest = await current(store, space_id)
    body = ProfileSave(
        **ProfileBody.model_validate(old.model_dump()).model_dump(),
        base_version=latest.version,
        note=f"Restored version {version}",
    )
    return await save(store, library, space_id, body, user)


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            out |= _flatten(v, f"{prefix}.{k}" if prefix else str(k))
        return out
    if isinstance(value, list) and value and all(isinstance(v, dict) and "id" in v for v in value):
        return {f"{prefix}[{v['id']}]": _canonical(v) for v in value}
    return {prefix: value}


def diff(a: SpaceProfile, b: SpaceProfile) -> ProfileDiff:
    """Every setting that differs from version a to version b."""
    fa = _flatten(ProfileBody.model_validate(a.model_dump()).model_dump(mode="json"))
    fb = _flatten(ProfileBody.model_validate(b.model_dump()).model_dump(mode="json"))
    changes = [
        {"path": path, "before": fa.get(path), "after": fb.get(path)}
        for path in sorted(set(fa) | set(fb))
        if fa.get(path) != fb.get(path)
    ]
    return ProfileDiff(a=a.version, b=b.version, changes=changes)


def export_yaml(profile: SpaceProfile) -> str:
    body = ProfileBody.model_validate(profile.model_dump()).model_dump(mode="json", exclude_defaults=True)
    header = f"# Profile of space {profile.space_id}, version {profile.version}\n"
    return header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100)


def parse_yaml(text: str) -> ProfileBody:
    try:
        return ProfileBody.model_validate(yaml.safe_load(text) or {})
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValidationFailed(f"Not a valid profile file: {exc}") from exc


def effective_view(eff: Effective, library: Library) -> EffectiveProfile:
    library_ids = {c.id for c in library.criteria}
    active = {c.id: c for c in eff.criteria}
    rules = []
    for c in library.criteria:
        setting = eff.profile.rules.get(c.id)
        shown = active.get(c.id, c)
        rules.append(
            EffectiveRule(
                id=c.id,
                title=c.title,
                family=c.family.value,
                gate=shown.gate.value,
                source="override" if setting else "library",
                enabled=c.id in active,
            )
        )
    rules += [
        EffectiveRule(
            id=c.id, title=c.title, family=c.family.value, gate=c.gate.value, source="custom", enabled=True
        )
        for c in eff.criteria
        if c.id not in library_ids
    ]
    return EffectiveProfile(
        space_id=eff.space_id,
        profile_version=eff.profile.version,
        criteria_set=eff.criteria_set,
        rules=rules,
        skills=[
            EffectiveSkill(
                name=name,
                ref=s.ref,
                model=eff.model_of(s),
                model_params=s.model_params,
                overridden=name in eff.profile.skills,
            )
            for name, s in eff.skills.items()
        ],
        thresholds=eff.thresholds,
        gate=eff.gate,
    )
