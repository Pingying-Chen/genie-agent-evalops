"""The collector's half of a regression run: hand the tester what to
ask, then grade what came back. The `evaluate` job wires these to Delta and the Jobs API.
"""

import asyncio
import logging
from datetime import datetime

from evalops.schemas.checks import Criterion
from evalops.schemas.evalset import EvalCase
from evalops.schemas.evaluation import (
    ArmConfig,
    AskCase,
    AskDone,
    AskRequest,
    Attempt,
    GradedAttempt,
    Preflight,
)
from evalops.schemas.genie import SpaceConfig
from evalops.services.llm.runner import SkillRunner
from evalops.services.llm.skills import Skill
from evalops.services.regression.asker import RunFolder
from evalops.services.regression.grading import grade_attempt

logger = logging.getLogger("evalops.evaluate")


def arm(name: str, config: SpaceConfig) -> ArmConfig:
    return ArmConfig(
        arm=name,
        config_version=config.version,
        title=config.title or "",
        warehouse_id=config.warehouse_id or "",
        serialized_space=config.serialized_space,
    )


def build_request(
    run_id: str,
    space_id: str,
    arms: list[ArmConfig],
    cases: list[EvalCase],
    *,
    k: int,
    concurrency: int,
    forbidden_table: str = "",
) -> AskRequest:
    return AskRequest(
        run_id=run_id,
        space_id=space_id,
        arms=arms,
        cases=[
            AskCase(case_id=c.case_id, version=c.version, turns=c.turns, reference_sql=c.expect.reference_sql)
            for c in cases
        ],
        k=k,
        concurrency=concurrency,
        forbidden_table=forbidden_table or None,
    )


async def read_back(folder: RunFolder) -> tuple[Preflight | None, AskDone | None, list[Attempt]]:
    """What the tester left in the folder. A missing `done.json` means the ask job died midway;
    the attempts it did write are still graded."""
    names = set(await folder.names(""))
    preflight = (
        Preflight.model_validate_json(await folder.read("preflight.json"))
        if "preflight.json" in names
        else None
    )
    done = AskDone.model_validate_json(await folder.read("done.json")) if "done.json" in names else None
    attempts = [Attempt.model_validate_json(await folder.read(n)) for n in await folder.names("attempts")]
    return preflight, done, attempts


async def grade(
    request: AskRequest,
    attempts: list[Attempt],
    cases: dict[str, EvalCase],
    *,
    criteria: list[Criterion],
    runner: SkillRunner,
    rubric_skills: dict[str, Skill],
    judge_skill: Skill,
    now: datetime,
) -> list[GradedAttempt]:
    async def one(attempt: Attempt) -> GradedAttempt:
        return await grade_attempt(
            attempt,
            cases[attempt.case_id],
            criteria=criteria,
            runner=runner,
            rubric_skills=rubric_skills,
            judge_skill=judge_skill,
            run_id=request.run_id,
            space_id=request.space_id,
            now=now,
        )

    graded = await asyncio.gather(*(one(a) for a in attempts if a.case_id in cases))
    return sorted(graded, key=lambda g: (g.case_id, g.arm, g.attempt))
