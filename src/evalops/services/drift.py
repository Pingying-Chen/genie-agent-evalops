"""Keep the eval set in step with what users ask.

Every production question and every active eval case gets a short intent label (a skill, run
by the review job, cached per question). Comparing the two gives suggestions: intents users
ask that no case covers (add), cases whose intent nobody asked lately (retire), plus hygiene
(cases that are flaky on the current configuration, duplicate cases). Suggestions only: the
owner approves every change to the eval set.
"""

import hashlib
import logging
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from evalops.clients.lakebase import StateStore
from evalops.clients.sql import SqlClient
from evalops.schemas.evalset import (
    AddSuggestion,
    AskedQuestion,
    DuplicateSuggestion,
    EvalCase,
    EvalSetSuggestions,
    FlakySuggestion,
    RetireSuggestion,
)
from evalops.schemas.llm import IntentInput, IntentOutput
from evalops.services import evalset, splits
from evalops.services.delta_store import DeltaWriter
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill
from evalops.services.regression.leakage import config_examples, leaks
from evalops.services.splits import case_intents

logger = logging.getLogger("evalops.drift")

BATCH = 25


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def case_question(case: EvalCase) -> str:
    """What the case's graded turn asks, with the turn it follows (as production follow-ups are shown)."""
    turns = case.turns
    index = case.expect.turn if case.expect.turn is not None else len(turns) - 1
    return f"{turns[index]} (follow-up to: {turns[index - 1]})" if index > 0 else turns[index]


async def production_questions(space_id: str, *, sql: SqlClient, prefix: str, since: datetime) -> list[dict]:
    return await sql.execute(
        f"""SELECT t.trace_id, coalesce(t.asked_at, t.collected_at) AS asked_at,
                   CASE WHEN p.question IS NOT NULL THEN concat(t.question, ' (follow-up to: ', p.question, ')')
                        ELSE t.question END AS question
            FROM {prefix}.traces t
            LEFT JOIN {prefix}.traces p ON p.conversation_id = t.conversation_id AND p.space_id = t.space_id
                 AND p.turn_index = t.turn_index - 1
            WHERE t.space_id = :space_id AND t.access_status = 'ok' AND t.question IS NOT NULL
              AND coalesce(t.asked_at, t.collected_at) >= :since""",
        {"space_id": space_id, "since": since},
    )


async def tag_intents(
    space_id: str,
    *,
    sql: SqlClient,
    store: StateStore,
    delta: DeltaWriter,
    prefix: str,
    runner: SkillRunner,
    skill: Skill,
    window_days: int,
    now: datetime,
) -> int:
    """Tag every production question in the window and every active case not tagged yet (or whose
    question changed). Batches share the growing list of intents, so labels stay consistent."""
    asked = await production_questions(
        space_id, sql=sql, prefix=prefix, since=now - timedelta(days=window_days)
    )
    cases = [c for c in await evalset.cases(store, space_id) if c.status == "active"]
    wanted = [("trace", r["trace_id"], r["question"]) for r in asked]
    wanted += [("case", f"{c.case_id}@{c.version}", case_question(c)) for c in cases]
    existing = {(r["kind"], r["ref_id"]): r for r in await delta.load_intents(space_id)}
    todo = [w for w in wanted if (existing.get(w[:2]) or {}).get("question_hash") != _hash(w[2])]
    intents = sorted({r["intent"] for r in existing.values() if r.get("intent")})
    rows = []
    for start in range(0, len(todo), BATCH):  # sequential: each batch sees the intents the last one added
        batch = todo[start : start + BATCH]
        data = IntentInput(
            intents="\n".join(intents) or "(none yet)",
            questions="\n".join(f"{n}. {q}" for n, (_, _, q) in enumerate(batch, start=1)),
        )
        result = await runner.run(skill, data)
        if not isinstance(result.output, IntentOutput):
            logger.warning("intent tagging batch failed: %s", result.error)
            continue
        by_number = {t.number: t.intent.strip().lower() for t in result.output.tags if t.intent.strip()}
        for n, (kind, ref_id, question) in enumerate(batch, start=1):
            if n not in by_number:
                continue
            intent = by_number[n][:120]
            if intent not in intents:
                intents.append(intent)
            rows.append(
                {
                    "space_id": space_id,
                    "kind": kind,
                    "ref_id": ref_id,
                    "question": question[:2000],
                    "question_hash": _hash(question),
                    "intent": intent,
                    "skill": skill.ref,
                    "model": runner.model,
                    "tagged_at": now.isoformat(),
                }
            )
    if rows:
        await delta.merge_intents(rows)
    return len(rows)


def suggest(
    asked: list[dict],
    intents: dict[tuple[str, str], str],
    cases: list[EvalCase],
    baseline_runs: dict[str, list[bool]],
    *,
    space_id: str,
    window_days: int,
    since: datetime,
    min_questions: int,
) -> EvalSetSuggestions:
    """Pure: `asked` production questions in the window, `intents` by (kind, ref_id), the active
    cases, and per case whether it was flaky on the baseline in each of its recent runs."""
    active = [c for c in cases if c.status == "active"]
    case_intent = case_intents(active, intents)
    covered = {i for i in case_intent.values() if i}
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in asked:
        intent = intents.get(("trace", r["trace_id"]))
        if intent:
            groups[intent].append(r)
    add = [
        AddSuggestion(
            intent=intent,
            count=len(rows),
            examples=[
                AskedQuestion(trace_id=r["trace_id"], question=r["question"], asked_at=r.get("asked_at"))
                for r in sorted(rows, key=lambda r: r.get("asked_at") or since, reverse=True)[:3]
            ],
        )
        for intent, rows in groups.items()
        if intent not in covered and len(rows) >= min_questions
    ]
    add.sort(key=lambda a: (-a.count, a.intent))
    asked_intents = set(groups)
    retire = [
        RetireSuggestion(case_id=c.case_id, title=c.title, intent=case_intent[c.case_id])
        for c in active
        if case_intent[c.case_id] and case_intent[c.case_id] not in asked_intents and c.updated_at < since
    ]
    flaky = [
        FlakySuggestion(case_id=c.case_id, title=c.title, flaky_runs=sum(runs), runs=len(runs))
        for c in active
        if len(runs := baseline_runs.get(c.case_id, [])) >= 2 and sum(runs) >= 2
    ]
    by_intent = Counter(i for i in case_intent.values() if i)
    duplicates = [
        DuplicateSuggestion(
            intent=intent,
            case_ids=[c.case_id for c in active if case_intent[c.case_id] == intent],
            titles=[c.title for c in active if case_intent[c.case_id] == intent],
        )
        for intent, n in sorted(by_intent.items())
        if n > 1
    ]
    tagged = sum(1 for r in asked if ("trace", r["trace_id"]) in intents)
    return EvalSetSuggestions(
        space_id=space_id,
        window_days=window_days,
        tagged_questions=tagged,
        untagged_questions=len(asked) - tagged,
        add=add,
        retire=retire,
        flaky=flaky,
        duplicates=duplicates,
    )


async def suggestions(
    space_id: str, *, sql: SqlClient, store: StateStore, prefix: str, window_days: int, min_questions: int
) -> EvalSetSuggestions:
    since = datetime.now(UTC) - timedelta(days=window_days)
    asked = await production_questions(space_id, sql=sql, prefix=prefix, since=since)
    rows = await sql.execute(
        f"SELECT kind, ref_id, intent FROM {prefix}.question_intents WHERE space_id = :space_id",
        {"space_id": space_id},
    )
    # Flaky on the baseline: per case, its last 3 runs.
    runs = await sql.execute(
        f"""SELECT r.case_id, r.run_id, max(u.started_at) AS started_at,
                   count_if(r.verdict = 'pass') > 0 AND count_if(r.verdict = 'fail') > 0 AS flaky
            FROM {prefix}.eval_results r JOIN {prefix}.eval_runs u USING (run_id)
            WHERE r.space_id = :space_id AND r.arm = 'baseline'
            GROUP BY r.case_id, r.run_id""",
        {"space_id": space_id},
    )
    baseline: dict[str, list[tuple]] = defaultdict(list)
    for r in runs:
        baseline[r["case_id"]].append((r["started_at"], bool(r["flaky"])))
    recent = {
        cid: [f for _, f in sorted(v, key=lambda x: x[0], reverse=True)[:3]] for cid, v in baseline.items()
    }
    snapshot = await sql.execute(
        f"""SELECT max_by(serialized_space, captured_at) AS serialized_space FROM {prefix}.space_versions
            WHERE space_id = :space_id""",
        {"space_id": space_id},
    )
    cases = await evalset.cases(store, space_id)
    examples = (
        config_examples(snapshot[0]["serialized_space"])
        if snapshot and snapshot[0].get("serialized_space")
        else []
    )
    result = suggest(
        asked,
        {(r["kind"], r["ref_id"]): r["intent"] for r in rows},
        cases,
        recent,
        space_id=space_id,
        window_days=window_days,
        since=since,
        min_questions=min_questions,
    )
    split_of = {cid: s.split for cid, s in (await splits.stored(store, space_id)).items()}
    return result.model_copy(update={"leaks": leaks(examples, cases, split_of)})
