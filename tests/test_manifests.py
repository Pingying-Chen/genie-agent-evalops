"""Version manifests: every run records exactly which rules,
prompts, guidelines, cases and settings produced its numbers, so two numbers can be explained by
a diff instead of a guess."""

import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_api import VIEWER, FakeSql, api, genie, sql  # noqa: F401  (fixtures)

from evalops.errors import ConfigError
from evalops.services import manifests
from evalops.services.criteria import criteria_dir, load_criteria
from evalops.services.llm.skills import load_skill, skills_dir

NOW = datetime(2026, 10, 25, tzinfo=UTC)
DEMO = Path(__file__).resolve().parents[1] / "demo" / "criteria"
CRITERIA, CRITERIA_SET = load_criteria(criteria_dir(), DEMO)
JUDGE = load_skill("expectation_judge", skills_dir(""))


def build(run_id="r1", **kw):
    args = dict(
        now=NOW,
        commit="abc123",
        config_versions={"baseline": "cfg1"},
        criteria=CRITERIA,
        criteria_set=CRITERIA_SET,
        skills=[JUDGE],
        judge_model="m",
        cases=[{"case_id": "c1", "version": 1, "split": "test"}],
        settings={"k": 3},
    )
    return manifests.build(run_id, "eval", "s", **{**args, **kw})


def test_the_same_inputs_give_the_same_hashes_and_an_edited_prompt_a_new_one():
    first, texts = build()
    again, _ = build("r2")
    assert [a.hash for a in first.artifacts] == [a.hash for a in again.artifacts]
    assert all(h in texts for h in (a.hash for a in first.artifacts))  # every text is kept

    edited = dataclasses.replace(JUDGE, template=JUDGE.template + "\nBe strict.")
    changed, _ = build(skills=[edited])
    skill = lambda m: next(a for a in m.artifacts if a.kind == "skill")  # noqa: E731
    assert skill(changed).hash != skill(first).hash and skill(changed).version == skill(first).version


def test_reviewer_guidelines_are_versioned_by_their_label_schema_name():
    review, texts = manifests.build("r", "review", "s", now=NOW, criteria=CRITERIA, guidelines=True)
    names = {a.name: a for a in review.artifacts if a.kind == "guideline"}
    assert names["task_success__v3"].version == "3"
    assert "rubric__ambiguous_metric__v2" in names  # one per rubric rule, carrying its version
    assert "Rule:" in texts[names["rubric__ambiguous_metric__v2"].hash][1]


class Delta:
    def __init__(self, stored=()):
        self.stored = list(stored)
        self.artifacts, self.manifests = [], []

    async def load_artifact_refs(self, kind, names):
        return [r for r in self.stored if r["name"] in names]

    async def merge_artifacts(self, rows):
        self.artifacts += rows

    async def merge_manifest(self, row):
        self.manifests.append(row)


async def test_a_skill_edited_without_a_version_bump_fails_the_run_before_it_starts():
    manifest, texts = build()
    ok = Delta()
    await manifests.record(ok, manifest, texts, "label")
    assert len(ok.manifests) == 1 and ok.manifests[0]["label"] == "label"
    assert len(ok.artifacts) == len(manifest.artifacts)

    old = {"name": JUDGE.name, "version": str(JUDGE.version), "hash": "sha256:something-else"}
    with pytest.raises(ConfigError, match=f"{JUDGE.name}@{JUDGE.version}"):
        await manifests.record(Delta([old]), manifest, texts)


def test_a_diff_names_every_difference_with_the_texts_before_and_after():
    a, texts_a = build()
    rule = next(c for c in CRITERIA if c.id == "rubric.ambiguous_metric")
    reworded = rule.model_copy(update={"params": {**rule.params, "rule": "Ask which measure."}})
    b, texts_b = build(
        "r2",
        commit="def456",
        config_versions={"baseline": "cfg1", "candidate": "cfg2"},
        criteria=[reworded if c.id == rule.id else c for c in CRITERIA],
        cases=[
            {"case_id": "c1", "version": 2, "split": "test"},
            {"case_id": "c2", "version": 1, "split": "dev"},
        ],
        settings={"k": 5},
    )
    texts = {h: t for h, (_, t) in {**texts_a, **texts_b}.items()}

    found = manifests.diff(a, b, texts)

    assert {f.field: (f.before, f.after) for f in found.fields} == {
        "platform_commit": ("abc123", "def456"),
        "config_versions.candidate": (None, "cfg2"),
        "settings.k": (3, 5),
    }
    [changed] = found.artifacts
    assert (changed.name, changed.change) == ("rubric.ambiguous_metric", "changed")
    assert "Ask which measure." in changed.after and "Ask which measure." not in changed.before
    assert (found.cases_added, found.cases_removed, found.cases_changed) == (["c2"], [], ["c1"])


def test_genie_drift_is_flagged_only_when_everything_we_control_is_the_same():
    week1, _ = build("w1")
    week2, _ = build("w2", now=NOW + timedelta(days=7))
    week3, _ = build("w3", now=NOW + timedelta(days=14), settings={"k": 5})  # we changed k

    flags = manifests.drift_flags([(week1, {"c1": "pass"}), (week2, {"c1": "fail"}), (week3, {"c1": "pass"})])

    assert [(f.earlier_run, f.later_run, f.flipped) for f in flags] == [("w1", "w2", ["c1"])]
    new_code, _ = build("w4", commit="zzz")
    [flag] = manifests.drift_flags([(week1, {"c1": "pass"}), (new_code, {"c1": "fail"})])
    assert "platform code also changed" in flag.note


def test_the_versions_page_is_for_owners_and_a_diff_needs_both_runs_in_the_space(api):  # noqa: F811
    assert api.get("/api/spaces/space-ok/versions", headers=VIEWER).status_code == 200
    assert api.get("/api/spaces/space-no/versions", headers=VIEWER).status_code in (403, 404)
    missing = api.get("/api/spaces/space-ok/versions/diff?a=x&b=y", headers=VIEWER)
    assert missing.status_code == 404
