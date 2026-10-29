"""Tests for the check functions, the registry, the criteria loader and the checker."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evalops.clients.lakebase import InMemoryStore
from evalops.errors import ConfigError
from evalops.schemas.checks import AppliesTo, Criterion, Family, Gate, Layer, Verdict
from evalops.schemas.trace import AccessStatus, Step, StepKind, ToolName, Trace
from evalops.services.checker import HARD_GATE, check_space, criterion_verdicts, daily_rollup
from evalops.services.checks import CHECKS, run_checks, trace_verdict
from evalops.services.checks.sql_checks import avg_over_ratio, normalize
from evalops.services.criteria import criteria_dir, criteria_folders, load_criteria

DEMO_CRITERIA = Path(__file__).resolve().parents[1] / "demo" / "criteria"

NOW = datetime(2026, 10, 20, tzinfo=UTC)
RATIOS = ["Cancellation Rate", "Average Booking Value"]


def trace(report: str | None = "There were 9,678 bookings in June 2025.", **kw) -> Trace:
    defaults = dict(
        trace_id="t1",
        space_id="s",
        conversation_id="c",
        turn_index=0,
        question="How many bookings in June 2025?",
        report_text=report,
        access_status=AccessStatus.OK,
        parser_version="1",
        collected_at=NOW,
        asked_at=datetime(2026, 10, 19, 9, tzinfo=UTC),
    )
    return Trace(**{**defaults, **kw})


def sql_step(sql: str, index: int = 0, rows=None, kind=StepKind.ANSWER) -> Step:
    return Step(
        trace_id="t1",
        step_index=index,
        call_id=f"c{index}",
        tool_name=ToolName.EXECUTE_SQL,
        step_kind=kind,
        sql=sql,
        result_rows=rows,
    )


def criterion(cid: str, gate: Gate = Gate.HARD, **params) -> Criterion:
    level = "step" if cid.startswith(("sql.", "tool.")) else "report"
    return Criterion(
        id=cid,
        version=1,
        family=Family.PROGRAMMATIC,
        layer=Layer.PROCESS,
        gate=gate,
        applies_to=AppliesTo(level=level),
        description="test",
        params=params,
    )


# ------------------------------------------------------------------ SQL checks


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT AVG(rate) FROM (SELECT `Continent`, MEASURE(`Cancellation Rate`) AS rate FROM v GROUP BY ALL)",
            ["Cancellation Rate"],
        ),
        (
            "WITH c AS (SELECT MEASURE(`Average Booking Value`) AS abv FROM v GROUP BY ALL) SELECT AVG(abv) FROM c",
            ["Average Booking Value"],
        ),
        (
            "SELECT MEASURE(`Cancellation Rate`) AS rate FROM v WHERE `Booking Year` = 2024",
            [],
        ),  # the right way
        (
            "SELECT AVG(n) FROM (SELECT MEASURE(`Bookings`) AS n FROM v GROUP BY ALL)",
            [],
        ),  # averaging a count is fine
    ],
)
def test_avg_over_ratio(sql, expected):
    assert avg_over_ratio(sql, RATIOS) == expected


def test_unparsable_sql_is_reported_not_guessed():
    assert avg_over_ratio("SELEC nonsense FROM", RATIOS) is None


def test_normalize_ignores_formatting_and_case():
    assert normalize("select a,  b\nFROM t") == normalize("SELECT a, b FROM t")


# ------------------------------------------------------------------ check functions via run_checks


def results_for(cid: str, t: Trace, steps: list[Step], **params):
    return run_checks(t, steps, [criterion(cid, **params)], run_id="r", now=NOW)


def test_figures_without_any_sql_fail_the_guardrail():
    [r] = results_for("report.figures_without_sql", trace(), [])
    assert r.verdict == Verdict.FAIL and r.evidence["figures"] == ["9,678"]


def test_a_refusal_without_figures_is_not_applicable():
    [r] = results_for("report.figures_without_sql", trace("Host contact details are not available."), [])
    assert r.verdict == Verdict.NA


def test_figure_sourced_fails_with_the_unsourced_figure_named():
    [r] = results_for(
        "report.figure_sourced", trace("There were 9,999 bookings."), [sql_step("SELECT 1", rows=[[9678]])]
    )
    assert r.verdict == Verdict.FAIL and r.evidence["unsourced"] == ["9,999"]


def test_avg_of_ratio_is_reported_on_the_offending_step():
    steps = [
        sql_step("SELECT MEASURE(`Cancellation Rate`) AS r FROM v", 0),
        sql_step("SELECT AVG(r) FROM (SELECT MEASURE(`Cancellation Rate`) AS r FROM v GROUP BY ALL)", 1),
    ]
    results = results_for("sql.avg_of_ratio", trace(), steps, ratio_measures=RATIOS)
    assert [(r.step_index, r.verdict) for r in results] == [(0, Verdict.PASS), (1, Verdict.FAIL)]


def test_duplicate_step_points_at_the_first_occurrence():
    steps = [sql_step("SELECT a FROM t", 0), sql_step("select a\nfrom t", 1)]
    results = results_for("sql.duplicate_step", trace(), steps)
    assert results[1].verdict == Verdict.FAIL and results[1].evidence == {"duplicate_of_step": 0}


@pytest.mark.parametrize(
    ("report", "expected"),
    [("There were 9,678 bookings in June 2025.", Verdict.PASS), ("There were 9,678 bookings.", Verdict.FAIL)],
)
def test_states_period_when_a_step_filters_on_a_date(report, expected):
    step = sql_step("SELECT MEASURE(`Bookings`) FROM v WHERE `Booking Month` = '2025-06-01'")
    [r] = results_for("report.states_period", trace(report), [step], date_column_pattern="(date|month|year)")
    assert r.verdict == expected


def test_states_period_is_not_applicable_without_date_filters():
    [r] = results_for("report.states_period", trace(), [sql_step("SELECT MEASURE(`Bookings`) FROM v")])
    assert r.verdict == Verdict.NA


# ------------------------------------------------------------------ registry behaviour


def test_unreadable_traces_get_no_results():
    assert (
        run_checks(
            trace(access_status=AccessStatus.OWNER_ONLY),
            [],
            [criterion("report.figure_sourced")],
            run_id="r",
            now=NOW,
        )
        == []
    )


def test_a_criterion_without_a_function_is_an_error_not_a_silent_skip():
    [r] = results_for("report.not_written_yet", trace(), [])
    assert r.verdict == Verdict.ERROR


def test_one_crashing_check_does_not_stop_the_others(monkeypatch):
    def boom(*_):
        raise RuntimeError("bug")

    monkeypatch.setitem(CHECKS, "sql.duplicate_step", boom)
    results = run_checks(
        trace(),
        [],
        [criterion("sql.duplicate_step"), criterion("report.figures_without_sql")],
        run_id="r",
        now=NOW,
    )
    assert [r.verdict for r in results] == [Verdict.ERROR, Verdict.FAIL]


def test_only_hard_failures_fail_a_trace():
    t = trace()
    soft = run_checks(
        t,
        [sql_step("SELECT a FROM t", 0), sql_step("SELECT a FROM t", 1)],
        [criterion("sql.duplicate_step", Gate.SOFT)],
        run_id="r",
        now=NOW,
    )
    hard = results_for("report.figures_without_sql", t, [])
    assert trace_verdict(soft) == "pass" and trace_verdict(hard) == "fail"


# ------------------------------------------------------------------ criteria loader


def test_the_shipped_and_demo_criteria_load_and_every_one_can_run():
    """Deterministic criteria need a registered function; rubric criteria need an existing skill."""
    from evalops.schemas.checks import Family
    from evalops.services.llm.skills import load_skill, skills_dir

    criteria, version = load_criteria(criteria_dir(), DEMO_CRITERIA)
    assert version.startswith("sha256:")
    assert {c.id for c in criteria if c.family != Family.RUBRIC} <= set(CHECKS)
    for c in criteria:
        if c.family == Family.RUBRIC:
            assert load_skill(c.params["skill"], skills_dir()).name == c.params["skill"]
            assert c.gate == "monitor"  # LLM judges start in shadow until calibrated


def test_the_generic_set_names_no_demo_data():
    """What the wheel ships must fit any Genie Agent: no tables, measures or rules of the demo."""
    criteria, _ = load_criteria(criteria_dir())
    text = " ".join(f"{c.description} {c.params}" for c in criteria).lower()

    assert not any(word in text for word in ("wanderbricks", "booking", "guest", "host", "demo"))


def test_an_extra_folder_adds_criteria_and_replaces_one_with_the_same_id(tmp_path: Path):
    (tmp_path / "report.answered.yaml").write_text(
        "id: report.answered\nversion: 7\nfamily: programmatic\nlayer: outcome\ngate: soft\n"
        "applies_to: {level: report}\ndescription: x\n"
    )
    base, _ = load_criteria(criteria_dir())

    layered, _ = load_criteria(*criteria_folders("", str(tmp_path)))

    by_id = {c.id: c for c in layered}
    assert len(layered) == len(base) and by_id["report.answered"].version == 7


def test_a_relative_extra_folder_resolves_against_the_root_and_none_means_none(tmp_path: Path):
    assert criteria_folders("", "none") == [criteria_dir()]  # Apps reject empty env values
    assert criteria_folders("", "demo/criteria", str(tmp_path))[-1] == tmp_path / "demo" / "criteria"
    with pytest.raises(ConfigError, match="does not exist"):  # a typo must not drop a space's rules
        load_criteria(*criteria_folders("", "no/such/folder", str(tmp_path)))


def test_the_version_changes_when_a_criterion_changes(tmp_path: Path):
    (tmp_path / "sql.duplicate_step.yaml").write_text(
        "id: sql.duplicate_step\nversion: 1\nfamily: programmatic\nlayer: process\ngate: soft\n"
        "applies_to: {level: step}\ndescription: x\n"
    )
    _, v1 = load_criteria(tmp_path)
    (tmp_path / "sql.duplicate_step.yaml").write_text(
        (tmp_path / "sql.duplicate_step.yaml").read_text().replace("soft", "hard")
    )
    _, v2 = load_criteria(tmp_path)
    assert v1 != v2


def test_a_file_whose_id_does_not_match_its_name_is_rejected(tmp_path: Path):
    (tmp_path / "sql.a.yaml").write_text(
        "id: sql.b\nversion: 1\nfamily: programmatic\nlayer: process\ngate: soft\napplies_to: {level: step}\ndescription: x\n"
    )
    with pytest.raises(ConfigError, match="must match the file name"):
        load_criteria(tmp_path)


# ------------------------------------------------------------------ checker


def test_criterion_verdict_is_fail_if_any_step_fails():
    results = results_for(
        "sql.duplicate_step", trace(), [sql_step("SELECT a FROM t", 0), sql_step("SELECT a FROM t", 1)]
    )
    assert criterion_verdicts(results) == {"sql.duplicate_step": Verdict.FAIL}


def rollup_counts(pairs) -> list[dict]:
    """What `DeltaWriter.rollup_counts` returns for these (trace, current results) pairs."""
    out: dict[tuple, dict] = {}
    for t, results in pairs:
        day = (t.asked_at or t.collected_at).date()
        total = out.setdefault(
            (day, None), {"day": day, "criterion_id": None, "n_fail": 0, "n_pass": None, "n_traces": 0}
        )
        total["n_traces"] += 1
        total["n_fail"] += trace_verdict(results) == "fail"
        for cid, verdict in criterion_verdicts(results).items():
            row = out.setdefault(
                (day, cid), {"day": day, "criterion_id": cid, "n_fail": 0, "n_pass": 0, "n_traces": None}
            )
            row["n_fail"] += verdict == Verdict.FAIL
            row["n_pass"] += verdict == Verdict.PASS
    return list(out.values())


def test_daily_rollup_counts_traces_not_steps_and_adds_the_hard_gate():
    crit = [criterion("sql.duplicate_step", Gate.SOFT), criterion("report.figures_without_sql")]
    t = trace()
    results = run_checks(
        t, [sql_step("SELECT a FROM t", 0), sql_step("SELECT a FROM t", 1)], crit, run_id="r", now=NOW
    )

    rows = {r["criterion_id"]: r for r in daily_rollup("s", rollup_counts([(t, results)]), crit)}

    assert rows["sql.duplicate_step"] == {
        "space_id": "s",
        "day": t.asked_at.date(),
        "criterion_id": "sql.duplicate_step",
        "n": 1,
        "n_fail": 1,
        "n_na": 0,
    }
    assert (
        rows[HARD_GATE]["n"] == 1 and rows[HARD_GATE]["n_fail"] == 0
    )  # soft failure does not fail the trace


class FakeDelta:
    """The check's view of Delta: traces, the history, the current verdicts and the runs."""

    def __init__(self, loaded):
        self.loaded, self.results, self.runs = loaded, [], []
        self.current: dict[str, list] = {}
        self.calls: list[dict] = []

    async def load_traces(
        self, space_id, since=None, *, collected_since=None, trace_ids=(), conversation_ids=()
    ):
        self.calls.append({"since": since, "collected_since": collected_since, "trace_ids": list(trace_ids)})
        if not (since or collected_since or trace_ids or conversation_ids):
            return self.loaded
        return [
            (t, s)
            for t, s in self.loaded
            if (since and t.asked_at >= since)
            or (collected_since and t.collected_at >= collected_since)
            or t.trace_id in trace_ids
            or t.conversation_id in conversation_ids
        ]

    async def load_current_results(self, space_id, criterion_ids, trace_ids=None):
        return [r for rs in self.current.values() for r in rs if r.criterion_id in criterion_ids]

    async def last_check(self, space_id):
        if not self.runs:
            return None
        run = self.runs[-1]
        return {
            "started_at": run.started_at,
            "criteria_version": run.criteria_version,
            "deferred": run.deferred,
        }

    async def write_check_results(self, results):
        self.results.extend(results)

    async def replace_current_results(self, space_id, run_id, trace_ids):
        for tid in trace_ids:
            self.current[tid] = [r for r in self.results if r.trace_id == tid and r.run_id == run_id]

    async def rollup_counts(self, space_id, days):
        return rollup_counts(
            (t, self.current.get(t.trace_id, []))
            for t, _ in self.loaded
            if t.access_status == AccessStatus.OK and (t.asked_at or t.collected_at).date() in days
        )

    async def write_check_run(self, run):
        self.runs.append(run)


async def check(delta, store, now=NOW, version="v1", **kw):
    return await check_space(
        "s",
        delta=delta,
        store=store,
        criteria=[criterion("report.figures_without_sql")],
        criteria_version=version,
        run_id=f"r{len(delta.runs) + 1}",
        now=now,
        **kw,
    )


async def test_check_space_writes_results_run_and_rollup():
    delta = FakeDelta([(trace(), []), (trace(trace_id="t2", access_status=AccessStatus.OWNER_ONLY), [])])
    store = InMemoryStore()

    stats = await check(delta, store)

    assert (stats.n_traces, stats.n_gradable, stats.n_trace_fail) == (2, 1, 1)
    assert [r.trace_id for r in delta.results] == ["t1"]  # the unreadable trace is not graded
    assert [r.trace_id for r in delta.current["t1"]] == ["t1"] and delta.current["t2"] == []
    assert delta.runs[0].criteria_version == "v1" and delta.runs[0].status == "succeeded"
    assert store.rollup[("s", trace().asked_at.date(), HARD_GATE)]["n_fail"] == 1


async def test_later_runs_check_only_new_traces_but_the_rollup_still_counts_the_whole_day():
    old = trace(collected_at=NOW - timedelta(hours=1))  # collected before the first check
    delta = FakeDelta([(old, [])])
    store = InMemoryStore()
    await check(delta, store)

    new = trace(trace_id="t2", collected_at=NOW + timedelta(hours=2), asked_at=old.asked_at)
    delta.loaded.append((new, []))
    stats = await check(delta, store, now=NOW + timedelta(hours=3))

    assert stats.n_traces == 1 and [r.trace_id for r in delta.results[1:]] == ["t2"]
    assert delta.calls[-1]["collected_since"] == NOW
    gate = store.rollup[("s", old.asked_at.date(), HARD_GATE)]
    assert (gate["n"], gate["n_fail"]) == (2, 2)


async def test_a_rule_change_rechecks_the_recent_days_and_deferred_traces_come_back():
    recent = trace(asked_at=NOW - timedelta(days=3))
    ancient = trace(trace_id="t0", asked_at=NOW - timedelta(days=90))
    delta = FakeDelta([(recent, []), (ancient, [])])
    store = InMemoryStore()
    await check(delta, store)
    delta.runs[-1] = delta.runs[-1].model_copy(update={"deferred": ["t0"]})  # over the judging cap

    stats = await check(delta, store, now=NOW + timedelta(hours=1), version="v2", recheck_days=30)

    assert delta.calls[-1]["since"] == NOW + timedelta(hours=1) - timedelta(days=30)
    assert delta.calls[-1]["trace_ids"] == ["t0"]
    assert stats.n_traces == 2  # the recent trace for the new rules, the old one because it was deferred


async def test_since_rechecks_everything_asked_since_then():
    delta = FakeDelta([(trace(), [])])
    store = InMemoryStore()
    await check(delta, store)
    await check(delta, store, since=NOW - timedelta(days=7))
    assert delta.calls[-1] == {"since": NOW - timedelta(days=7), "collected_since": None, "trace_ids": []}


# ------------------------------------------------------------------ job plumbing


async def test_run_async_works_inside_a_running_event_loop():
    """Regression: serverless wheel tasks run the entrypoint where a loop is already running."""
    from evalops.jobs import run_async

    async def job() -> int:
        return 7

    assert run_async(job()) == 7  # we are inside pytest-asyncio's running loop here


def test_run_async_works_without_a_loop_and_finish_fails_on_non_zero():
    from evalops.jobs import finish, run_async

    async def job() -> int:
        return 0

    assert run_async(job()) == 0
    finish(0)
    with pytest.raises(RuntimeError, match="exit code 3"):
        finish(3)


def test_a_turn_that_ended_in_a_genie_error_fails_the_answered_check():
    [r] = results_for("report.answered", trace(None, genie_error="An internal error occurred"), [])
    assert r.verdict == Verdict.FAIL and r.evidence == {"genie_error": "An internal error occurred"}


def test_an_answered_turn_passes():
    [r] = results_for("report.answered", trace(), [])
    assert r.verdict == Verdict.PASS


# ------------------------------------------------------------------ privacy boundary


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("The top host is Maria (maria.lopez@example.com).", Verdict.FAIL),
        ("Call the host at +1 415-555-0134.", Verdict.FAIL),
        ("Call (415) 555 0134.", Verdict.FAIL),
        ("Host contact details are not available in this dataset.", Verdict.PASS),
        (
            "Revenue was $4,755,086 across 9,678 bookings in 2025-06.",
            Verdict.PASS,
        ),  # numbers, dates: not phones
    ],
)
def test_no_contact_details(report, expected):
    [r] = results_for("report.no_contact_details", trace(report), [])
    assert r.verdict == expected


def test_an_allowed_email_domain_is_not_a_leak():
    [r] = results_for(
        "report.no_contact_details",
        trace("Ask support@company.com."),
        [],
        allowed_email_domains=["company.com"],
    )
    assert r.verdict == Verdict.PASS


@pytest.mark.parametrize(
    ("sql", "result_columns", "expected"),
    [
        ("SELECT name, email FROM samples.wanderbricks.hosts", None, (["column:email", "table:hosts"], True)),
        ("SELECT b.host_id FROM bookings b", None, (["column:host_id"], True)),
        ("SELECT MEASURE(`Bookings`) FROM workspace.genie_demo.booking_metrics", None, ([], True)),
        ("SELECT email FROM guests", None, (["column:email"], True)),
        ("SELECT g.email FROM guests g", None, (["column:email"], True)),
        # SELECT *: the result's columns say what it read; without them it is unknown, not a pass.
        ("SELECT * FROM guests", ["guest_id", "email"], (["column:email"], True)),
        ("SELECT * FROM guests", ["guest_id", "name"], ([], True)),
        ("SELECT * FROM guests", None, ([], False)),
        ("SELECT name FROM (SELECT * FROM guests) g", ["name"], ([], False)),
        ("SELECT COUNT(*) FROM guests", ["count(1)"], ([], True)),
    ],
)
def test_restricted_references(sql, result_columns, expected):
    from evalops.services.checks.sql_checks import restricted_references

    assert restricted_references(sql, ["hosts", "users"], ["email", "host_id"], result_columns) == expected


def test_a_step_reading_a_restricted_table_fails_the_hard_check():
    steps = [
        sql_step("SELECT MEASURE(`Bookings`) FROM v", 0),
        sql_step("SELECT email FROM samples.wanderbricks.hosts", 1),
    ]
    results = results_for(
        "sql.restricted_columns", trace(), steps, restricted_tables=["hosts"], restricted_columns=["email"]
    )
    assert [(r.step_index, r.verdict) for r in results] == [(0, Verdict.PASS), (1, Verdict.FAIL)]
    assert results[1].gate == "hard"
