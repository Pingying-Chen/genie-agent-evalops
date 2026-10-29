"""The tester's half of a regression run, run by the `ask` job as the
tester service principal: check its own access, copy each configuration into a scratch space,
ask every case k times, write what Genie answered to the run folder, delete the scratch spaces.

It holds a consumer's data grants and nothing else, so what it is shown is what a consumer is
shown. It never reads the production space: the collector exported the configuration.
"""

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Protocol

from evalops.clients.genie import GenieClient
from evalops.clients.sql import SqlClient
from evalops.errors import UpstreamError
from evalops.schemas.evaluation import Arm, AskCase, AskDone, AskRequest, Attempt, Preflight
from evalops.schemas.genie import Conversation, ConversationType
from evalops.services.collector import fetch_conversation

logger = logging.getLogger("evalops.ask")

_TABLE = re.compile(r"^[A-Za-z0-9_`.-]+$")


class RunFolder(Protocol):
    """The workspace folder both identities can reach (or a dict in tests)."""

    async def read(self, name: str) -> str: ...

    async def write(self, name: str, text: str) -> None: ...

    async def names(self, prefix: str) -> list[str]: ...


def attempt_name(arm: Arm, case_id: str, n: int) -> str:
    return f"attempts/{arm}__{case_id}__{n}.json"


async def preflight(request: AskRequest, me: str, *, genie: GenieClient, sql: SqlClient) -> Preflight:
    problems = []
    manages = await genie.can_manage(request.space_id)
    if manages:
        problems.append("the tester manages the production space: it sees more than a consumer")
    readable = None
    if request.forbidden_table:
        if not _TABLE.match(request.forbidden_table):
            problems.append(f"forbidden_table {request.forbidden_table!r} is not a table name")
        else:
            try:
                await sql.execute(f"SELECT 1 FROM {request.forbidden_table} LIMIT 1")
                readable = True
                problems.append(f"the tester can read {request.forbidden_table}, which consumers cannot")
            except UpstreamError:
                readable = False
    return Preflight(tester=me, manages_space=manages, forbidden_readable=readable, problems=problems)


async def ask_case(
    space_id: str, arm: Arm, case: AskCase, n: int, *, genie: GenieClient, sql: SqlClient
) -> Attempt:
    """Ask the case's turns in one conversation, then read it back like the collector does."""
    attempt = Attempt(arm=arm, case_id=case.case_id, case_version=case.version, attempt=n)
    started = time.monotonic()
    created = datetime.now(UTC)
    try:
        conversation_id = None
        for turn in case.turns:
            conversation_id = await genie.ask_agent(space_id, turn, conversation_id)
        attempt.conversation_id = conversation_id
        conversation = Conversation(
            conversation_id=conversation_id,
            created_timestamp=int(created.timestamp() * 1000),
            agent_type=ConversationType.AGENT,
        )
        fetched = await fetch_conversation(genie, space_id, conversation, datetime.now(UTC))
        attempt.traces, attempt.steps = fetched.traces, fetched.steps
    except UpstreamError as exc:
        attempt.error = exc.message[:1000]
    attempt.seconds = round(time.monotonic() - started, 1)
    if case.reference_sql:
        try:  # live, as the tester: the consumer's rows and today's data
            rows = await sql.execute(case.reference_sql)
            attempt.reference_columns = list(rows[0]) if rows else []
            attempt.reference_rows = [list(r.values()) for r in rows[:200]]
        except UpstreamError as exc:
            attempt.reference_error = exc.message[:1000]
    return attempt


async def run(
    request: AskRequest, me: str, parent_path: str, *, genie: GenieClient, sql: SqlClient, folder: RunFolder
) -> AskDone:
    check = await preflight(request, me, genie=genie, sql=sql)
    await folder.write("preflight.json", check.model_dump_json())
    if not check.ok:
        done = AskDone(attempts=0, scratch_spaces=[], deleted=True, error="; ".join(check.problems))
        await folder.write("done.json", done.model_dump_json())
        return done

    spaces: dict[Arm, str] = {}
    written = 0
    error = None
    try:
        for arm in request.arms:
            spaces[arm.arm] = await genie.create_space(
                title=f"evalops {request.run_id[:8]} {arm.arm}",
                description=f"Scratch copy for regression run {request.run_id}; deleted when it ends.",
                warehouse_id=arm.warehouse_id,
                serialized_space=arm.serialized_space,
                parent_path=parent_path,
            )
        slots = asyncio.Semaphore(request.concurrency)

        async def one(arm: Arm, case: AskCase, n: int) -> None:
            nonlocal written
            async with slots:
                attempt = await ask_case(spaces[arm], arm, case, n, genie=genie, sql=sql)
            await folder.write(attempt_name(arm, case.case_id, n), attempt.model_dump_json())
            written += 1
            logger.info(
                "%s %s #%d: %s in %.0fs", arm, case.case_id, n, attempt.error or "answered", attempt.seconds
            )

        # Arms interleaved per case and attempt, so both see the same moment and the same data.
        await asyncio.gather(
            *(
                one(arm.arm, case, n)
                for n in range(request.k)
                for case in request.cases
                for arm in request.arms
            )
        )
    except Exception as exc:  # the run folder must still say what happened
        error = f"{type(exc).__name__}: {exc}"[:1000]
        logger.exception("ask run failed")
    finally:
        deleted = True
        for space_id in spaces.values():
            try:
                await genie.delete_space(space_id)
            except UpstreamError:
                deleted = False
                logger.exception("could not delete scratch space %s", space_id)
        done = AskDone(attempts=written, scratch_spaces=list(spaces.values()), deleted=deleted, error=error)
        await folder.write("done.json", done.model_dump_json())
    return done
