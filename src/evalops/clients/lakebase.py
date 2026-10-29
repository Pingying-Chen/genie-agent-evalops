"""Operational state store: Lakebase (Postgres via asyncpg) with an in-memory fallback.

Follows the Genie Workbench's Lakebase pattern, with one
improvement:
- host from `w.postgres.get_endpoint(name=...)`; password = short-lived OAuth token from
  `w.postgres.generate_database_credential(endpoint=...)`; user = the identity's client id;
- **the pool gets the password as a callable** (`TokenProvider`). asyncpg calls it for every
  new connection, and the provider mints a fresh token when the cached one is old. The
  Workbench instead recreates the whole pool every 50 minutes; with a callable that is not
  needed (an open connection stays authenticated; only new connections need a valid token);
- `CREATE ... IF NOT EXISTS` at startup, so the identity that runs it owns its tables;
- without Lakebase the app falls back to memory (tests, read-only look); the collector
  must not (`require_durable=True`), because a lost cursor means duplicate or missed work.

The Databricks SDK is synchronous, so its calls run in a worker thread
(`asyncio.to_thread`) to keep the event loop free.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from databricks.sdk import WorkspaceClient

from evalops.errors import ConfigError, Conflict

logger = logging.getLogger(__name__)

TOKEN_MAX_AGE_S = 45 * 60  # credentials live ~60 min; refresh well before that
SCHEMA_VERSION = 8
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class StateStore(Protocol):
    """What the app and jobs need from operational state. Both implementations satisfy it."""

    async def get_cursor(self, space_id: str) -> datetime | None:
        """Newest conversation `created_ts` seen by the last successful run, or None."""
        ...

    async def set_cursor(self, space_id: str, last_seen_created_ts: datetime, run_id: str) -> None:
        """Advance the cursor. Call only after the run's Delta writes committed."""
        ...

    async def get_permission(self, user: str, space_id: str) -> bool | None:
        """Cached CAN_MANAGE check, or None if missing or expired."""
        ...

    async def put_permission(self, user: str, space_id: str, allowed: bool, ttl_seconds: int) -> None: ...

    async def put_daily_rollup(self, rows: list[dict]) -> None:
        """Upsert `{space_id, day, criterion_id, n, n_fail, n_na}` rows (written by the check job)."""
        ...

    async def get_daily_rollup(self, space_id: str, day_from: date, day_to: date) -> list[dict]:
        """Rollup rows for a space and an inclusive day window."""
        ...

    async def put_space_status(self, space_id: str, title: str | None, collector_has_manage: bool) -> None:
        """Whether the collector can read the space (written by the collect job)."""
        ...

    async def get_space_status(self, space_ids: list[str]) -> dict[str, dict]:
        """`{space_id: {title, collector_has_manage, last_checked_at, last_collected_at}}`."""
        ...

    async def sync_reviewer(self, user: str, space_ids: list[str]) -> None:
        """Record that `user` reviews exactly `space_ids` (from their own /Me, written by the app)."""
        ...

    async def get_reviewers(self, space_id: str) -> list[str]:
        """Reviewers of a space who have opened the app, sorted (read by the review job)."""
        ...

    async def put_review_session(self, space_id: str, experiment_id: str, session_id: str, url: str) -> None:
        """Where a space's labeling session lives (written by the review job)."""
        ...

    async def get_review_sessions(self, space_ids: list[str]) -> dict[str, str]:
        """`{space_id: labeling session url}`."""
        ...

    async def get_categories(self, space_id: str) -> list[dict]:
        """The space's failure categories, oldest first (owner edits)."""
        ...

    async def put_category(self, space_id: str, category: dict) -> None:
        """Insert or replace one category, keyed by its `id`."""
        ...

    async def delete_category(self, space_id: str, category_id: str) -> None:
        """Remove a category and every tag that uses it."""
        ...

    async def get_tags(self, space_id: str) -> dict[str, list[str]]:
        """`{trace_id: [category id, ...]}` for the space's tagged observations."""
        ...

    async def set_tags(self, space_id: str, trace_id: str, category_ids: list[str], user: str) -> None:
        """Replace the categories of one observation (an empty list untags it)."""
        ...

    async def get_causes(self, space_id: str) -> dict[tuple[str, str], dict]:
        """`{(trace_id, criterion_id): {cause, note, judge, marked_by}}`: why a judge and a reviewer disagree."""
        ...

    async def put_cause(
        self,
        space_id: str,
        trace_id: str,
        criterion_id: str,
        cause: str | None,
        note: str,
        judge: str,
        user: str,
    ) -> None:
        """Record the owner's diagnosis of one disagreement; `cause=None` removes it."""
        ...

    async def get_eval_cases(self, space_id: str) -> list[dict]:
        """The current (highest) version of every eval case of the space."""
        ...

    async def get_eval_case_history(self, space_id: str, case_id: str) -> list[dict]:
        """Every version of one case, oldest first."""
        ...

    async def get_eval_case_spaces(self) -> list[str]:
        """Every space that has at least one eval case (it may have no traffic yet)."""
        ...

    async def add_eval_case_version(self, space_id: str, case: dict) -> None:
        """Append one version (`case_id`, `version` and the body); raise `Conflict` if that version exists."""
        ...

    async def get_changes(self, space_id: str) -> list[dict]:
        """The space's configuration changes, newest first."""
        ...

    async def put_change(self, space_id: str, change: dict) -> None:
        """Insert or replace one change, keyed by `change_id`."""
        ...

    async def get_setting(self, space_id: str, key: str) -> dict | None:
        """A space's settings object under `key` (e.g. `split`), or None if never saved."""
        ...

    async def put_setting(self, space_id: str, key: str, body: dict, user: str) -> None: ...

    async def get_case_splits(self, space_id: str) -> list[dict]:
        """`{case_id, split, stratum, assigned_by, assigned_at}` for every case with a split."""
        ...

    async def put_case_splits(self, space_id: str, rows: list[dict]) -> None:
        """Insert or replace splits, keyed by `case_id`."""
        ...

    async def delete_case_splits(self, space_id: str, case_ids: list[str]) -> None: ...

    async def get_profile_history(self, space_id: str) -> list[dict]:
        """Every saved version of the space's profile, oldest first: `{version, body, note,
        updated_by, updated_at}`."""
        ...

    async def add_profile_version(self, space_id: str, row: dict) -> None:
        """Append one version; raise `Conflict` if that version exists (someone saved first)."""
        ...


# --------------------------------------------------------------------------- in-memory


class InMemoryStore:
    """Process-local fallback. Not durable: everything is lost on restart.

    `clock` is injectable so tests can move time forward instead of sleeping.
    """

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._cursors: dict[str, datetime] = {}
        self._permissions: dict[tuple[str, str], tuple[bool, float]] = {}
        self.rollup: dict[tuple[str, object, str], dict] = {}
        self._space_status: dict[str, dict] = {}
        self._last_collected: dict[str, datetime] = {}
        self.reviewers: dict[str, set[str]] = {}
        self._review_sessions: dict[str, str] = {}
        self.categories: dict[str, dict[str, dict]] = {}  # space -> id -> category
        self.tags: dict[str, dict[str, list[str]]] = {}  # space -> trace -> category ids
        self.eval_cases: dict[str, dict[tuple[str, int], dict]] = {}  # space -> (case, version) -> case
        self.changes: dict[str, dict[str, dict]] = {}  # space -> change id -> change
        self.causes: dict[str, dict[tuple[str, str], dict]] = {}  # space -> (trace, criterion) -> cause
        self.settings: dict[tuple[str, str], dict] = {}  # (space, key) -> body
        self.splits: dict[str, dict[str, dict]] = {}  # space -> case id -> split row
        self.profiles: dict[str, dict[int, dict]] = {}  # space -> version -> row

    async def get_cursor(self, space_id: str) -> datetime | None:
        return self._cursors.get(space_id)

    async def set_cursor(self, space_id: str, last_seen_created_ts: datetime, run_id: str) -> None:
        self._cursors[space_id] = last_seen_created_ts
        self._last_collected[space_id] = datetime.fromtimestamp(self._clock(), tz=UTC)

    async def get_permission(self, user: str, space_id: str) -> bool | None:
        entry = self._permissions.get((user, space_id))
        if entry is None:
            return None
        allowed, expires_at = entry
        if self._clock() >= expires_at:
            del self._permissions[(user, space_id)]
            return None
        return allowed

    async def put_permission(self, user: str, space_id: str, allowed: bool, ttl_seconds: int) -> None:
        self._permissions[(user, space_id)] = (allowed, self._clock() + ttl_seconds)

    async def put_daily_rollup(self, rows: list[dict]) -> None:
        for row in rows:
            self.rollup[(row["space_id"], row["day"], row["criterion_id"])] = row

    async def get_daily_rollup(self, space_id: str, day_from: date, day_to: date) -> list[dict]:
        return sorted(
            (r for (s, d, _), r in self.rollup.items() if s == space_id and day_from <= d <= day_to),
            key=lambda r: (r["day"], r["criterion_id"]),
        )

    async def put_space_status(self, space_id: str, title: str | None, collector_has_manage: bool) -> None:
        previous = self._space_status.get(space_id, {})
        self._space_status[space_id] = {
            "title": title if title is not None else previous.get("title"),
            "collector_has_manage": collector_has_manage,
            "last_checked_at": datetime.fromtimestamp(self._clock(), tz=UTC),
        }

    async def get_space_status(self, space_ids: list[str]) -> dict[str, dict]:
        return {
            s: {**self._space_status[s], "last_collected_at": self._last_collected.get(s)}
            for s in space_ids
            if s in self._space_status
        }

    async def sync_reviewer(self, user: str, space_ids: list[str]) -> None:
        for space_id, users in self.reviewers.items():
            if space_id not in space_ids:
                users.discard(user)
        for space_id in space_ids:
            self.reviewers.setdefault(space_id, set()).add(user)

    async def get_reviewers(self, space_id: str) -> list[str]:
        return sorted(self.reviewers.get(space_id, set()))

    async def put_review_session(self, space_id: str, experiment_id: str, session_id: str, url: str) -> None:
        self._review_sessions[space_id] = url

    async def get_review_sessions(self, space_ids: list[str]) -> dict[str, str]:
        return {s: self._review_sessions[s] for s in space_ids if s in self._review_sessions}

    async def get_categories(self, space_id: str) -> list[dict]:
        return sorted(self.categories.get(space_id, {}).values(), key=lambda c: c["created_at"])

    async def put_category(self, space_id: str, category: dict) -> None:
        self.categories.setdefault(space_id, {})[category["id"]] = dict(category)

    async def delete_category(self, space_id: str, category_id: str) -> None:
        self.categories.get(space_id, {}).pop(category_id, None)
        for trace_id, ids in list(self.tags.get(space_id, {}).items()):
            self.tags[space_id][trace_id] = [i for i in ids if i != category_id]

    async def get_tags(self, space_id: str) -> dict[str, list[str]]:
        return {t: list(ids) for t, ids in self.tags.get(space_id, {}).items() if ids}

    async def set_tags(self, space_id: str, trace_id: str, category_ids: list[str], user: str) -> None:
        self.tags.setdefault(space_id, {})[trace_id] = list(dict.fromkeys(category_ids))

    async def get_causes(self, space_id: str) -> dict[tuple[str, str], dict]:
        return dict(self.causes.get(space_id, {}))

    async def put_cause(
        self,
        space_id: str,
        trace_id: str,
        criterion_id: str,
        cause: str | None,
        note: str,
        judge: str,
        user: str,
    ) -> None:
        causes = self.causes.setdefault(space_id, {})
        if cause is None:
            causes.pop((trace_id, criterion_id), None)
        else:
            causes[(trace_id, criterion_id)] = {
                "cause": cause,
                "note": note,
                "judge": judge,
                "marked_by": user,
            }

    async def get_eval_cases(self, space_id: str) -> list[dict]:
        latest: dict[str, dict] = {}
        for (case_id, version), case in self.eval_cases.get(space_id, {}).items():
            if case_id not in latest or version > latest[case_id]["version"]:
                latest[case_id] = case
        return sorted((dict(c) for c in latest.values()), key=lambda c: c["case_id"])

    async def get_eval_case_history(self, space_id: str, case_id: str) -> list[dict]:
        versions = [c for (cid, _), c in self.eval_cases.get(space_id, {}).items() if cid == case_id]
        return sorted((dict(c) for c in versions), key=lambda c: c["version"])

    async def get_eval_case_spaces(self) -> list[str]:
        return sorted(s for s, cases in self.eval_cases.items() if cases)

    async def add_eval_case_version(self, space_id: str, case: dict) -> None:
        cases = self.eval_cases.setdefault(space_id, {})
        key = (case["case_id"], case["version"])
        if key in cases:
            raise Conflict("Someone else saved this case first; reload it and try again.")
        cases[key] = dict(case)

    async def get_changes(self, space_id: str) -> list[dict]:
        return sorted(
            (dict(c) for c in self.changes.get(space_id, {}).values()),
            key=lambda c: c["created_at"],
            reverse=True,
        )

    async def put_change(self, space_id: str, change: dict) -> None:
        self.changes.setdefault(space_id, {})[change["change_id"]] = dict(change)

    async def get_setting(self, space_id: str, key: str) -> dict | None:
        body = self.settings.get((space_id, key))
        return dict(body) if body is not None else None

    async def put_setting(self, space_id: str, key: str, body: dict, user: str) -> None:
        self.settings[(space_id, key)] = dict(body)

    async def get_case_splits(self, space_id: str) -> list[dict]:
        return sorted((dict(r) for r in self.splits.get(space_id, {}).values()), key=lambda r: r["case_id"])

    async def put_case_splits(self, space_id: str, rows: list[dict]) -> None:
        mine = self.splits.setdefault(space_id, {})
        for row in rows:
            mine[row["case_id"]] = dict(row)

    async def delete_case_splits(self, space_id: str, case_ids: list[str]) -> None:
        mine = self.splits.get(space_id, {})
        for case_id in case_ids:
            mine.pop(case_id, None)

    async def get_profile_history(self, space_id: str) -> list[dict]:
        return [dict(r) for _, r in sorted(self.profiles.get(space_id, {}).items())]

    async def add_profile_version(self, space_id: str, row: dict) -> None:
        mine = self.profiles.setdefault(space_id, {})
        if row["version"] in mine:
            raise Conflict("Someone else saved this profile first; reload it and try again.")
        mine[row["version"]] = dict(row)


# --------------------------------------------------------------------------- Lakebase


class TokenProvider:
    """Async callable that returns a valid Lakebase OAuth token, refreshing it when stale.

    Passed to asyncpg as `password=`. A lock makes concurrent new connections share one
    refresh instead of each minting a token.
    """

    def __init__(
        self,
        w: WorkspaceClient,
        endpoint: str,
        *,
        max_age_s: float = TOKEN_MAX_AGE_S,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._w = w
        self._endpoint = endpoint
        self._max_age_s = max_age_s
        self._clock = clock
        self._token: str | None = None
        self._minted_at = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() - self._minted_at >= self._max_age_s:
                credential = await asyncio.to_thread(
                    self._w.postgres.generate_database_credential, endpoint=self._endpoint
                )
                if not credential.token:
                    raise ConfigError(f"Lakebase returned no credential for {self._endpoint}.")
                self._token = credential.token
                self._minted_at = self._clock()
                logger.info("minted Lakebase credential for %s", self._endpoint)
            return self._token


async def resolve_user(w: WorkspaceClient, configured: str = "") -> str:
    """Postgres role to connect as: the configured name, else the SP client id, else the user.

    Lakebase maps Databricks identities to Postgres roles by name: a service principal's
    role is its application (client) id, a user's role is their email.
    """
    if configured:
        return configured
    if w.config.client_id:
        return w.config.client_id
    me = await asyncio.to_thread(w.current_user.me)
    if not me.user_name:
        raise ConfigError("Cannot determine the Lakebase user; set EVALOPS_LAKEBASE_USER.")
    return me.user_name


async def resolve_host(w: WorkspaceClient, endpoint: str) -> str:
    """DNS host of a Lakebase Autoscaling endpoint (`projects/<p>/branches/<b>/endpoints/<e>`)."""
    info = await asyncio.to_thread(w.postgres.get_endpoint, name=endpoint)
    hosts = info.status.hosts if info.status else None
    if not hosts or not hosts.host:
        raise ConfigError(f"Lakebase endpoint {endpoint} has no host (stopped, or still provisioning?).")
    return hosts.host


def app_grants(schema: str, role: str) -> list[str]:
    """What the app's service principal needs on tables the jobs own (run once as the owner)."""
    r = '"' + role.replace('"', '""') + '"'
    return [
        f"GRANT USAGE ON SCHEMA {schema} TO {r}",
        f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {r}",
        f"GRANT INSERT, UPDATE ON {schema}.permission_cache TO {r}",
        f"GRANT INSERT, UPDATE, DELETE ON {schema}.reviewers TO {r}",
        f"GRANT INSERT, UPDATE, DELETE ON {schema}.failure_categories TO {r}",
        f"GRANT INSERT, UPDATE, DELETE ON {schema}.observation_tags TO {r}",
        f"GRANT INSERT, UPDATE, DELETE ON {schema}.disagreement_causes TO {r}",
        f"GRANT INSERT ON {schema}.eval_cases TO {r}",
        f"GRANT INSERT, UPDATE ON {schema}.space_changes TO {r}",
        f"GRANT INSERT, UPDATE ON {schema}.space_settings TO {r}",
        f"GRANT INSERT, UPDATE, DELETE ON {schema}.eval_case_splits TO {r}",
        f"GRANT INSERT ON {schema}.space_profiles TO {r}",
    ]


def collector_grants(schema: str, database: str, role: str) -> list[str]:
    """What the jobs' service principal needs to bootstrap and write state (run once as the owner).

    CREATE on the database and the schema because Postgres checks them even for
    `CREATE ... IF NOT EXISTS`; full DML on the existing tables, which another identity created.
    """
    r = '"' + role.replace('"', '""') + '"'
    return [
        f"GRANT CREATE ON DATABASE {database} TO {r}",
        f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {r}",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {r}",
    ]


def ddl(schema: str) -> list[str]:
    """Idempotent DDL for every operational table."""
    s = schema
    return [
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"""CREATE TABLE IF NOT EXISTS {s}.schema_version (
                version INT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        f"""CREATE TABLE IF NOT EXISTS {s}.collector_cursor (
                space_id TEXT PRIMARY KEY,
                last_seen_created_ts TIMESTAMPTZ NOT NULL,
                last_run_id TEXT NOT NULL,
                last_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                status TEXT, error TEXT)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.collector_runs (
                run_id TEXT, space_id TEXT,
                started_at TIMESTAMPTZ NOT NULL, finished_at TIMESTAMPTZ,
                n_conversations INT, n_traces_new INT, n_traces_updated INT,
                status TEXT NOT NULL, error TEXT,
                PRIMARY KEY (run_id, space_id))""",
        f"""CREATE TABLE IF NOT EXISTS {s}.space_status (
                space_id TEXT PRIMARY KEY, title TEXT,
                collector_has_manage BOOLEAN NOT NULL, last_checked_at TIMESTAMPTZ NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.permission_cache (
                user_email TEXT, space_id TEXT, allowed BOOLEAN NOT NULL,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (user_email, space_id))""",
        f"""CREATE TABLE IF NOT EXISTS {s}.daily_rollup (
                space_id TEXT, day DATE, criterion_id TEXT,
                n INT NOT NULL, n_fail INT NOT NULL, n_na INT NOT NULL,
                PRIMARY KEY (space_id, day, criterion_id))""",
        # Reviewers register themselves: the app writes the viewer's verified reviewer groups.
        f"""CREATE TABLE IF NOT EXISTS {s}.reviewers (
                space_id TEXT, user_email TEXT, last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_id, user_email))""",
        f"""CREATE TABLE IF NOT EXISTS {s}.review_sessions (
                space_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, session_id TEXT NOT NULL,
                url TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        # Failure analysis: owners name kinds of failure and tag observations.
        f"""CREATE TABLE IF NOT EXISTS {s}.failure_categories (
                space_id TEXT, category_id TEXT, name TEXT NOT NULL, description TEXT NOT NULL,
                impact SMALLINT NOT NULL, criterion_id TEXT,
                created_at TIMESTAMPTZ NOT NULL, updated_by TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (space_id, category_id))""",
        # The first version capped impact at 3 in the table; the API now validates 1-5. Only when
        # the constraint is still there: ALTER needs the table's owner, and grant.sh runs this as
        # the workspace owner, not as the jobs' service principal that owns the table.
        f"""DO $$ BEGIN
                IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'failure_categories_impact_check'
                           AND conrelid = '{s}.failure_categories'::regclass) THEN
                    ALTER TABLE {s}.failure_categories DROP CONSTRAINT failure_categories_impact_check;
                END IF;
            END $$""",
        f"""CREATE TABLE IF NOT EXISTS {s}.observation_tags (
                space_id TEXT, trace_id TEXT, category_id TEXT, tagged_by TEXT NOT NULL,
                tagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_id, trace_id, category_id))""",
        # Judge improvement: the owner's diagnosis of each judge-reviewer disagreement.
        f"""CREATE TABLE IF NOT EXISTS {s}.disagreement_causes (
                space_id TEXT, trace_id TEXT, criterion_id TEXT, cause TEXT NOT NULL, note TEXT NOT NULL,
                judge TEXT NOT NULL, marked_by TEXT NOT NULL, marked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_id, trace_id, criterion_id))""",
        # Eval set: every version of every case is kept; the body is the case as JSON.
        f"""CREATE TABLE IF NOT EXISTS {s}.eval_cases (
                space_id TEXT, case_id TEXT, version INTEGER, body TEXT NOT NULL,
                updated_by TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (space_id, case_id, version))""",
        # Space changes: written by owners, tested by regression runs, applied by them.
        f"""CREATE TABLE IF NOT EXISTS {s}.space_changes (
                space_id TEXT, change_id TEXT, body TEXT NOT NULL, status TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (space_id, change_id))""",
        # Per-space settings: one JSON object per (space, key), e.g. the split settings.
        f"""CREATE TABLE IF NOT EXISTS {s}.space_settings (
                space_id TEXT, key TEXT, body TEXT NOT NULL,
                updated_by TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (space_id, key))""",
        # Dev/test split of each eval case, apart from the case so edits never move it.
        f"""CREATE TABLE IF NOT EXISTS {s}.eval_case_splits (
                space_id TEXT, case_id TEXT, split TEXT NOT NULL, stratum TEXT NOT NULL,
                assigned_by TEXT NOT NULL, assigned_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (space_id, case_id))""",
        # Space profiles: every save a version; the body is the profile as JSON.
        f"""CREATE TABLE IF NOT EXISTS {s}.space_profiles (
                space_id TEXT, version INTEGER, body TEXT NOT NULL, note TEXT NOT NULL,
                updated_by TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (space_id, version))""",
        f"INSERT INTO {s}.schema_version (version) VALUES ({SCHEMA_VERSION}) ON CONFLICT DO NOTHING",
    ]


class PostgresStore:
    """Lakebase-backed store. Create with `await PostgresStore.connect(...)`.

    Every value goes in as a `$n` parameter; only the schema name is interpolated, and it is
    validated against `_IDENTIFIER` first.
    """

    def __init__(self, pool, schema: str):
        self._pool = pool
        self._s = schema

    @classmethod
    async def connect(
        cls,
        endpoint: str,
        database: str,
        schema: str,
        *,
        user: str = "",
        bootstrap: bool = True,
        w: WorkspaceClient | None = None,
    ) -> "PostgresStore":
        """Resolve the endpoint, create the pool with a refreshing password, and (if `bootstrap`)
        create the tables.

        Least privilege: the identity that owns the schema (the jobs) bootstraps; the app
        connects with `bootstrap=False` and only needs USAGE, SELECT and INSERT/UPDATE on
        `permission_cache` (Postgres checks CREATE on the schema even for `IF NOT EXISTS`).
        """
        import asyncpg  # imported here so the in-memory path needs no Postgres driver at import time

        if not _IDENTIFIER.match(schema):
            raise ConfigError(f"Invalid Lakebase schema name: {schema!r}")
        w = w or WorkspaceClient()
        host = await resolve_host(w, endpoint)
        user = await resolve_user(w, user)
        pool = await asyncpg.create_pool(
            host=host,
            port=5432,
            database=database,
            user=user,
            password=TokenProvider(w, endpoint),
            ssl="require",
            min_size=1,
            max_size=10,
            command_timeout=30,
            max_inactive_connection_lifetime=30 * 60,
        )
        store = cls(pool, schema)
        if bootstrap:
            async with pool.acquire() as conn, conn.transaction():
                for statement in ddl(schema):
                    await conn.execute(statement)
        logger.info("Lakebase store ready: host=%s user=%s schema=%s", host, user, schema)
        return store

    async def close(self) -> None:
        await self._pool.close()

    async def get_cursor(self, space_id: str) -> datetime | None:
        return await self._pool.fetchval(
            f"SELECT last_seen_created_ts FROM {self._s}.collector_cursor WHERE space_id = $1", space_id
        )

    async def set_cursor(self, space_id: str, last_seen_created_ts: datetime, run_id: str) -> None:
        await self._pool.execute(
            f"""INSERT INTO {self._s}.collector_cursor (space_id, last_seen_created_ts, last_run_id, status)
                VALUES ($1, $2, $3, 'ok')
                ON CONFLICT (space_id) DO UPDATE SET
                    last_seen_created_ts = EXCLUDED.last_seen_created_ts,
                    last_run_id = EXCLUDED.last_run_id, last_run_at = now(), status = 'ok', error = NULL""",
            space_id,
            last_seen_created_ts,
            run_id,
        )

    async def get_permission(self, user: str, space_id: str) -> bool | None:
        return await self._pool.fetchval(
            f"""SELECT allowed FROM {self._s}.permission_cache
                WHERE user_email = $1 AND space_id = $2 AND expires_at > now()""",
            user,
            space_id,
        )

    async def put_permission(self, user: str, space_id: str, allowed: bool, ttl_seconds: int) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        await self._pool.execute(
            f"""INSERT INTO {self._s}.permission_cache (user_email, space_id, allowed, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_email, space_id) DO UPDATE SET
                    allowed = EXCLUDED.allowed, checked_at = now(), expires_at = EXCLUDED.expires_at""",
            user,
            space_id,
            allowed,
            expires_at,
        )

    async def put_daily_rollup(self, rows: list[dict]) -> None:
        await self._pool.executemany(
            f"""INSERT INTO {self._s}.daily_rollup (space_id, day, criterion_id, n, n_fail, n_na)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (space_id, day, criterion_id) DO UPDATE SET
                    n = EXCLUDED.n, n_fail = EXCLUDED.n_fail, n_na = EXCLUDED.n_na""",
            [(r["space_id"], r["day"], r["criterion_id"], r["n"], r["n_fail"], r["n_na"]) for r in rows],
        )

    async def get_daily_rollup(self, space_id: str, day_from: date, day_to: date) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT space_id, day, criterion_id, n, n_fail, n_na FROM {self._s}.daily_rollup
                WHERE space_id = $1 AND day BETWEEN $2 AND $3 ORDER BY day, criterion_id""",
            space_id,
            day_from,
            day_to,
        )
        return [dict(r) for r in rows]

    async def put_space_status(self, space_id: str, title: str | None, collector_has_manage: bool) -> None:
        await self._pool.execute(
            f"""INSERT INTO {self._s}.space_status (space_id, title, collector_has_manage, last_checked_at)
                VALUES ($1, $2, $3, now())
                ON CONFLICT (space_id) DO UPDATE SET
                    title = COALESCE(EXCLUDED.title, {self._s}.space_status.title),
                    collector_has_manage = EXCLUDED.collector_has_manage, last_checked_at = now()""",
            space_id,
            title,
            collector_has_manage,
        )

    async def get_space_status(self, space_ids: list[str]) -> dict[str, dict]:
        rows = await self._pool.fetch(
            f"""SELECT s.space_id, s.title, s.collector_has_manage, s.last_checked_at,
                       c.last_run_at AS last_collected_at
                FROM {self._s}.space_status s
                LEFT JOIN {self._s}.collector_cursor c USING (space_id)
                WHERE s.space_id = ANY($1::text[])""",
            space_ids,
        )
        return {r["space_id"]: {k: v for k, v in dict(r).items() if k != "space_id"} for r in rows}

    async def sync_reviewer(self, user: str, space_ids: list[str]) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"DELETE FROM {self._s}.reviewers WHERE user_email = $1 AND NOT (space_id = ANY($2::text[]))",
                user,
                space_ids,
            )
            await conn.executemany(
                f"""INSERT INTO {self._s}.reviewers (space_id, user_email) VALUES ($1, $2)
                    ON CONFLICT (space_id, user_email) DO UPDATE SET last_seen_at = now()""",
                [(space_id, user) for space_id in space_ids],
            )

    async def get_reviewers(self, space_id: str) -> list[str]:
        rows = await self._pool.fetch(
            f"SELECT user_email FROM {self._s}.reviewers WHERE space_id = $1 ORDER BY user_email", space_id
        )
        return [r["user_email"] for r in rows]

    async def put_review_session(self, space_id: str, experiment_id: str, session_id: str, url: str) -> None:
        await self._pool.execute(
            f"""INSERT INTO {self._s}.review_sessions (space_id, experiment_id, session_id, url)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (space_id) DO UPDATE SET experiment_id = EXCLUDED.experiment_id,
                    session_id = EXCLUDED.session_id, url = EXCLUDED.url, updated_at = now()""",
            space_id,
            experiment_id,
            session_id,
            url,
        )

    async def get_review_sessions(self, space_ids: list[str]) -> dict[str, str]:
        rows = await self._pool.fetch(
            f"SELECT space_id, url FROM {self._s}.review_sessions WHERE space_id = ANY($1::text[])", space_ids
        )
        return {r["space_id"]: r["url"] for r in rows}

    async def get_categories(self, space_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT category_id AS id, name, description, impact, criterion_id, created_at,
                       updated_by, updated_at
                FROM {self._s}.failure_categories WHERE space_id = $1 ORDER BY created_at""",
            space_id,
        )
        return [dict(r) for r in rows]

    async def put_category(self, space_id: str, category: dict) -> None:
        c = category
        await self._pool.execute(
            f"""INSERT INTO {self._s}.failure_categories (space_id, category_id, name, description,
                    impact, criterion_id, created_at, updated_by, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (space_id, category_id) DO UPDATE SET name = EXCLUDED.name,
                    description = EXCLUDED.description, impact = EXCLUDED.impact,
                    criterion_id = EXCLUDED.criterion_id, updated_by = EXCLUDED.updated_by,
                    updated_at = EXCLUDED.updated_at""",
            space_id,
            c["id"],
            c["name"],
            c["description"],
            c["impact"],
            c["criterion_id"],
            c["created_at"],
            c["updated_by"],
            c["updated_at"],
        )

    async def delete_category(self, space_id: str, category_id: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"DELETE FROM {self._s}.observation_tags WHERE space_id = $1 AND category_id = $2",
                space_id,
                category_id,
            )
            await conn.execute(
                f"DELETE FROM {self._s}.failure_categories WHERE space_id = $1 AND category_id = $2",
                space_id,
                category_id,
            )

    async def get_tags(self, space_id: str) -> dict[str, list[str]]:
        rows = await self._pool.fetch(
            f"""SELECT trace_id, category_id FROM {self._s}.observation_tags
                WHERE space_id = $1 ORDER BY tagged_at""",
            space_id,
        )
        tags: dict[str, list[str]] = {}
        for r in rows:
            tags.setdefault(r["trace_id"], []).append(r["category_id"])
        return tags

    async def set_tags(self, space_id: str, trace_id: str, category_ids: list[str], user: str) -> None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"""DELETE FROM {self._s}.observation_tags
                    WHERE space_id = $1 AND trace_id = $2 AND NOT (category_id = ANY($3::text[]))""",
                space_id,
                trace_id,
                category_ids,
            )
            await conn.executemany(
                f"""INSERT INTO {self._s}.observation_tags (space_id, trace_id, category_id, tagged_by)
                    VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING""",
                [(space_id, trace_id, c, user) for c in dict.fromkeys(category_ids)],
            )

    async def get_causes(self, space_id: str) -> dict[tuple[str, str], dict]:
        rows = await self._pool.fetch(
            f"""SELECT trace_id, criterion_id, cause, note, judge, marked_by
                FROM {self._s}.disagreement_causes WHERE space_id = $1""",
            space_id,
        )
        return {(r["trace_id"], r["criterion_id"]): dict(r) for r in rows}

    async def put_cause(
        self,
        space_id: str,
        trace_id: str,
        criterion_id: str,
        cause: str | None,
        note: str,
        judge: str,
        user: str,
    ) -> None:
        if cause is None:
            await self._pool.execute(
                f"""DELETE FROM {self._s}.disagreement_causes
                    WHERE space_id = $1 AND trace_id = $2 AND criterion_id = $3""",
                space_id,
                trace_id,
                criterion_id,
            )
            return
        await self._pool.execute(
            f"""INSERT INTO {self._s}.disagreement_causes
                    (space_id, trace_id, criterion_id, cause, note, judge, marked_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (space_id, trace_id, criterion_id) DO UPDATE SET cause = EXCLUDED.cause,
                    note = EXCLUDED.note, judge = EXCLUDED.judge, marked_by = EXCLUDED.marked_by,
                    marked_at = now()""",
            space_id,
            trace_id,
            criterion_id,
            cause,
            note,
            judge,
            user,
        )

    async def get_eval_cases(self, space_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT DISTINCT ON (case_id) case_id, version, body, updated_by, updated_at
                FROM {self._s}.eval_cases WHERE space_id = $1 ORDER BY case_id, version DESC""",
            space_id,
        )
        return [_case(space_id, r) for r in rows]

    async def get_eval_case_history(self, space_id: str, case_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT case_id, version, body, updated_by, updated_at FROM {self._s}.eval_cases
                WHERE space_id = $1 AND case_id = $2 ORDER BY version""",
            space_id,
            case_id,
        )
        return [_case(space_id, r) for r in rows]

    async def get_profile_history(self, space_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT version, body, note, updated_by, updated_at FROM {self._s}.space_profiles
                WHERE space_id = $1 ORDER BY version""",
            space_id,
        )
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    async def add_profile_version(self, space_id: str, row: dict) -> None:
        import asyncpg  # as in connect(): the in-memory path needs no Postgres driver

        try:
            await self._pool.execute(
                f"""INSERT INTO {self._s}.space_profiles (space_id, version, body, note, updated_by, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6)""",
                space_id,
                row["version"],
                json.dumps(row["body"], default=str),
                row["note"],
                row["updated_by"],
                row["updated_at"],
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("Someone else saved this profile first; reload it and try again.") from exc

    async def get_eval_case_spaces(self) -> list[str]:
        rows = await self._pool.fetch(f"SELECT DISTINCT space_id FROM {self._s}.eval_cases ORDER BY space_id")
        return [r["space_id"] for r in rows]

    async def add_eval_case_version(self, space_id: str, case: dict) -> None:
        import asyncpg  # as in connect(): the in-memory path needs no Postgres driver

        body = {k: v for k, v in case.items() if k not in _CASE_COLUMNS}
        try:
            await self._pool.execute(
                f"""INSERT INTO {self._s}.eval_cases (space_id, case_id, version, body, updated_by, updated_at)
                    VALUES ($1, $2, $3, $4, $5, $6)""",
                space_id,
                case["case_id"],
                case["version"],
                json.dumps(body, default=str),
                case["updated_by"],
                case["updated_at"],
            )
        except asyncpg.UniqueViolationError as exc:
            raise Conflict("Someone else saved this case first; reload it and try again.") from exc

    async def get_changes(self, space_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT change_id, body, status, created_by, created_at, updated_at FROM {self._s}.space_changes
                WHERE space_id = $1 ORDER BY created_at DESC""",
            space_id,
        )
        return [
            {
                **json.loads(r["body"]),
                **{k: r[k] for k in ("change_id", "status", "created_by", "created_at", "updated_at")},
                "space_id": space_id,
            }
            for r in rows
        ]

    async def put_change(self, space_id: str, change: dict) -> None:
        keys = ("space_id", "change_id", "status", "created_by", "created_at", "updated_at")
        body = {k: v for k, v in change.items() if k not in keys}
        await self._pool.execute(
            f"""INSERT INTO {self._s}.space_changes (space_id, change_id, body, status, created_by, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (space_id, change_id) DO UPDATE SET body = EXCLUDED.body, status = EXCLUDED.status,
                    updated_at = EXCLUDED.updated_at""",
            space_id,
            change["change_id"],
            json.dumps(body, default=str),
            change["status"],
            change["created_by"],
            change["created_at"],
            change["updated_at"],
        )

    async def get_setting(self, space_id: str, key: str) -> dict | None:
        row = await self._pool.fetchrow(
            f"SELECT body FROM {self._s}.space_settings WHERE space_id = $1 AND key = $2", space_id, key
        )
        return json.loads(row["body"]) if row else None

    async def put_setting(self, space_id: str, key: str, body: dict, user: str) -> None:
        await self._pool.execute(
            f"""INSERT INTO {self._s}.space_settings (space_id, key, body, updated_by, updated_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (space_id, key) DO UPDATE SET body = EXCLUDED.body,
                    updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at""",
            space_id,
            key,
            json.dumps(body, default=str),
            user,
        )

    async def get_case_splits(self, space_id: str) -> list[dict]:
        rows = await self._pool.fetch(
            f"""SELECT case_id, split, stratum, assigned_by, assigned_at FROM {self._s}.eval_case_splits
                WHERE space_id = $1 ORDER BY case_id""",
            space_id,
        )
        return [dict(r) for r in rows]

    async def put_case_splits(self, space_id: str, rows: list[dict]) -> None:
        await self._pool.executemany(
            f"""INSERT INTO {self._s}.eval_case_splits (space_id, case_id, split, stratum, assigned_by, assigned_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (space_id, case_id) DO UPDATE SET split = EXCLUDED.split, stratum = EXCLUDED.stratum,
                    assigned_by = EXCLUDED.assigned_by, assigned_at = EXCLUDED.assigned_at""",
            [
                (space_id, r["case_id"], r["split"], r["stratum"], r["assigned_by"], r["assigned_at"])
                for r in rows
            ],
        )

    async def delete_case_splits(self, space_id: str, case_ids: list[str]) -> None:
        await self._pool.execute(
            f"DELETE FROM {self._s}.eval_case_splits WHERE space_id = $1 AND case_id = ANY($2::text[])",
            space_id,
            case_ids,
        )


async def open_store(
    endpoint: str,
    database: str,
    schema: str,
    *,
    user: str = "",
    require_durable: bool,
    bootstrap: bool = True,
) -> StateStore:
    """Return a Postgres store when configured, else memory; fail fast if durability is required."""
    if endpoint:
        return await PostgresStore.connect(endpoint, database, schema, user=user, bootstrap=bootstrap)
    if require_durable:
        raise ConfigError("Lakebase is required here (EVALOPS_LAKEBASE_ENDPOINT is empty).")
    return InMemoryStore()


_CASE_COLUMNS = ("space_id", "case_id", "version", "updated_by", "updated_at")


def _case(space_id: str, row) -> dict:
    return {
        **json.loads(row["body"]),
        "space_id": space_id,
        "case_id": row["case_id"],
        "version": row["version"],
        "updated_by": row["updated_by"],
        "updated_at": row["updated_at"],
    }
