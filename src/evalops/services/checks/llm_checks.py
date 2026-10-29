"""LLM-judged criteria (family `rubric`), run asynchronously next to the deterministic checks.

Each rubric criterion names a skill and carries its rule text in `params`. Results are reused
when nothing that could change the verdict changed: same trace, criterion version, skill
version, model and sampling settings (temperature, number of samples). That keeps reruns of the check job from paying for the same judgement.
"""

import asyncio
import hashlib
from collections import Counter, defaultdict
from datetime import datetime

from evalops.errors import UpstreamError
from evalops.schemas.checks import CheckResult, Criterion, Family, Verdict
from evalops.schemas.llm import RubricJudgeInput
from evalops.schemas.trace import AccessStatus, Step, ToolName, Trace
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill

# (trace_id, criterion_id) -> previous result, for reuse
Previous = dict[tuple[str, str], CheckResult]


def rubric_criteria(criteria: list[Criterion]) -> list[Criterion]:
    return [c for c in criteria if c.family == Family.RUBRIC]


def render_steps(steps: list[Step]) -> str:
    return "\n\n".join(
        f"{s.title or 'Query'}\n{s.sql}" for s in steps if s.tool_name == ToolName.EXECUTE_SQL and s.sql
    )


def conversation_context(
    trace: Trace, loaded: list[tuple[Trace, list[Step]]], *, max_chars: int = 4000
) -> str:
    """Earlier turns of the same conversation, oldest first: what a follow-up inherits."""
    earlier = sorted(
        (
            t
            for t, _ in loaded
            if t.conversation_id == trace.conversation_id and t.turn_index < trace.turn_index
        ),
        key=lambda t: t.turn_index,
    )
    text = "\n\n".join(
        f"Turn {t.turn_index + 1} question: {t.question}\nReport: {(t.report_text or '')[:1500]}"
        for t in earlier
    )
    return text[-max_chars:]


def judge_input(
    criterion: Criterion,
    trace: Trace,
    steps: list[Step],
    loaded: list[tuple[Trace, list[Step]]],
    context: str | None = None,
) -> RubricJudgeInput:
    """What the judge reads for one turn; also what labelled examples are exported as.
    `context` = `conversation_context(trace, loaded)`, when the caller already has it."""
    return RubricJudgeInput(
        rule=criterion.params["rule"],
        failure=criterion.params.get("failure", ""),
        question=trace.question or "",
        context=conversation_context(trace, loaded) if context is None else context,
        steps=render_steps(steps),
        report=trace.report_text or "",
    )


def _reusable(previous: CheckResult | None, criterion: Criterion, skill: Skill, model: str) -> bool:
    return (
        previous is not None
        and previous.criterion_version == criterion.version
        and previous.evidence.get("skill") == skill.ref
        and previous.evidence.get("model") == model
        and previous.evidence.get("model_params") == skill.model_params
        and previous.verdict != Verdict.ERROR
    )


def sampled(trace_id: str, rate: float) -> bool:
    """Whether a trace is in the judged sample: stable across runs, uniform over traces."""
    if rate >= 1:
        return True
    return int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16) / 0x100000000 < rate


async def run_rubric_checks(
    loaded: list[tuple[Trace, list[Step]]],
    criteria: list[Criterion],
    *,
    runner: SkillRunner,
    skills: dict[str, Skill],
    previous: Previous,
    run_id: str,
    now: datetime,
    only: set[str] | None = None,
    sample_rate: float = 1.0,
    max_judged: int | None = None,
) -> tuple[list[CheckResult], int, set[str]]:
    """Results for the gradable traces and rubric criteria, how many LLM calls were made, and the
    traces whose judging was deferred.

    `only` limits judging to those trace ids; the rest of `loaded` is still the conversation
    context (a regression run grades one turn of a scripted conversation).

    At scale: `sample_rate` judges a stable share of traces (by a hash of the trace id); the rest
    get no rubric result at all, so a rule's rate counts judged answers only and stays unbiased.
    `max_judged` caps fresh judgements per run; the rest get no result either and are returned as
    deferred, for the next run. A reusable earlier verdict is always kept."""
    calls = 0
    judged = 0
    deferred: set[str] = set()
    turns: dict[str, list[tuple[Trace, list[Step]]]] = defaultdict(list)
    for pair in loaded:
        turns[pair[0].conversation_id].append(pair)
    contexts: dict[str, str] = {}

    def context_of(trace: Trace) -> str:
        if trace.trace_id not in contexts:
            contexts[trace.trace_id] = conversation_context(trace, turns[trace.conversation_id])
        return contexts[trace.trace_id]

    async def judge(trace: Trace, steps: list[Step], criterion: Criterion) -> CheckResult | None:
        nonlocal calls, judged
        skill = skills[criterion.params["skill"]]
        base = dict(
            run_id=run_id,
            trace_id=trace.trace_id,
            step_index=None,
            criterion_id=criterion.id,
            criterion_version=criterion.version,
            family=criterion.family,
            layer=criterion.layer,
            gate=criterion.gate,
            checked_at=now,
        )
        prior = previous.get((trace.trace_id, criterion.id))
        if _reusable(prior, criterion, skill, skill.endpoint or runner.model):
            return CheckResult(
                **base, verdict=prior.verdict, evidence={**prior.evidence, "reused_from": prior.run_id}
            )
        if not sampled(trace.trace_id, sample_rate):
            return None
        if max_judged is not None and judged >= max_judged:
            deferred.add(trace.trace_id)
            return None
        if not (trace.report_text or "").strip():
            return CheckResult(**base, verdict=Verdict.NA, evidence={"reason": "no report to judge"})
        judged += 1
        data = judge_input(criterion, trace, steps, loaded, context_of(trace))
        samples = int(skill.model_params.get("samples", 1))
        votes: list[str] = []
        outputs: list[tuple[object, str]] = []  # (consistent output, its reasoning summary)
        evidence: dict = {
            "skill": skill.ref,
            "model": skill.endpoint or runner.model,
            "model_params": skill.model_params,
            "tokens": 0,
            "samples": samples,
        }
        for _ in range(samples):  # sequential per trace; traces still run concurrently
            calls += 1
            try:
                result = await runner.run(skill, data)
            except UpstreamError as exc:  # e.g. rate limited after retries: this verdict only, not the run
                return CheckResult(
                    **base, verdict=Verdict.ERROR, evidence={**evidence, "error": exc.message[:500]}
                )
            evidence["tokens"] += result.tokens
            if result.served_model:
                evidence["served_model"] = result.served_model
            if result.output is None:
                votes.append("error")
                evidence["error"] = result.error
                continue
            out = result.output
            votes.append(out.verdict if out.consistent() else "inconsistent")
            if out.consistent():
                outputs.append((out, result.reasoning))
        tally = Counter(votes)
        verdict, top = tally.most_common(1)[0]
        evidence |= {"votes": votes, "agreement": round(top / samples, 2)}
        majority = [v for v, n in tally.items() if n == top]
        if len(majority) > 1 or verdict in ("error", "inconsistent"):
            return CheckResult(
                **base, verdict=Verdict.ERROR, evidence={**evidence, "error": f"no usable majority: {votes}"}
            )
        # Show the reasoning of one sample that voted with the majority.
        chosen, reasoning = next((o, r) for o, r in outputs if o.verdict == verdict)
        evidence |= {
            "applies": chosen.applies,
            "applies_reason": chosen.applies_reason,
            "quote": chosen.quote,
            "reason": chosen.reasoning,
            "judge_reasoning": reasoning[:2000],
        }
        return CheckResult(**base, verdict=Verdict(verdict), evidence=evidence)

    jobs = [
        judge(trace, steps, criterion)
        for trace, steps in loaded
        if trace.access_status == AccessStatus.OK and (only is None or trace.trace_id in only)
        for criterion in rubric_criteria(criteria)
    ]
    results = [r for r in await asyncio.gather(*jobs) if r is not None]
    return results, calls, deferred
