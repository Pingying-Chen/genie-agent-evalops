"""Runtime configuration, read from environment variables prefixed `EVALOPS_`.

The bundle (databricks.yml) and app.yaml set these per deployment target, so nothing
workspace-specific is hard-coded.
"""

import os
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EVALOPS_", env_file=".env", extra="ignore")

    # Where Delta tables live
    catalog: str = Field(default="workspace", description="Unity Catalog catalog for evalops tables.")
    db_schema: str = Field(
        default="genie_evalops", alias="EVALOPS_SCHEMA", description="Schema for evalops tables."
    )
    warehouse_id: str = Field(default="", description="SQL warehouse used by the app and jobs.")

    # Lakebase (operational state)
    lakebase_endpoint: str = Field(
        default="", description="Lakebase Autoscaling endpoint name; empty = in-memory fallback (app only)."
    )
    lakebase_database: str = Field(default="databricks_postgres")
    lakebase_user: str = Field(
        default="",
        description=(
            "Postgres role to connect as. Empty = auto: the service principal's client id, "
            "else the signed-in user's name. Set it when the deploying identity differs."
        ),
    )
    lakebase_schema: str = Field(default="evalops")

    # Collector / jobs
    collect_job_id: int | None = Field(default=None, description="Job triggered by POST /collect.")
    evaluate_job_id: int | None = Field(default=None, description="Job triggered by POST /evaluations.")
    collector_sp_application_id: str = Field(
        default="", description="Shown in 'grant CAN_MANAGE to ...' hints."
    )
    exclude_spaces: list[str] = Field(default_factory=list)
    lookback_days: int = Field(default=7, ge=0)

    # Checks
    criteria_dir: str = Field(
        default="", description="Folder of criteria YAML; empty = the packaged defaults."
    )
    extra_criteria_dir: str = Field(
        default="",
        description="Folder of this deployment's own criteria, layered on the base set (same id "
        "replaces); empty or 'none' = none. Relative paths resolve against root_dir.",
    )
    root_dir: str = Field(
        default="", description="Base for relative paths (jobs: the bundle's files folder); empty = cwd."
    )

    # LLM skills (v2)
    judge_model: str = Field(
        default="databricks-qwen3-next-80b-a3b-instruct",
        description="Serving endpoint the LLM skills call (per workspace).",
    )
    skills_dir: str = Field(default="", description="Folder of skills; empty = the packaged defaults.")
    llm_concurrency: int = Field(
        default=2, ge=1, description="Concurrent LLM calls (serving endpoints rate-limit)."
    )

    # Human review (v2)
    reviewer_group_pattern: str = Field(
        default="genie-reviewers-{space_id}",
        description="Name of the Databricks group whose members review a space; {space_id} is filled in.",
    )
    review_experiment_root: str = Field(
        default="",
        description="Workspace folder for per-space review experiments; empty = the job identity's home.",
    )
    review_timezone: str = Field(
        default="UTC", description="Time zone the reviewer reads 'asked at' in (UTC is always shown too)."
    )
    review_budget: int = Field(default=30, ge=1, description="Most traces queued per space per review run.")
    expectation_review_budget: int = Field(
        default=10,
        ge=0,
        description="Regression answers queued per space per review run to calibrate expectation_judge; 0 = none.",
    )
    judge_sample_rate: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="Share of new answers the LLM judges read (a stable random sample by trace id).",
    )
    max_judged_per_run: int = Field(
        default=500,
        ge=1,
        description="Most fresh LLM judgements per space per check run; the rest wait for the next run.",
    )
    recheck_days: int = Field(
        default=30,
        ge=1,
        description="When the rules change, how many days of earlier answers the next check run re-checks.",
    )
    calibration_min_lower_bound: float = Field(
        default=0.8,
        gt=0,
        le=1,
        description="Wilson lower bound both TPR and TNR must reach to count as calibrated.",
    )

    # Concurrency and caching
    # Regression runs (v3)
    tester_job_id: int | None = Field(default=None, description="The `ask` job, run as the tester.")
    tester_principal: str = Field(
        default="", description="The tester service principal's application id (shares the run folder)."
    )
    eval_runs_dir: str = Field(
        default="",
        description="Workspace folder for run hand-off files; empty = <job identity home>/evalops-runs.",
    )
    eval_k: int = Field(default=3, ge=1, le=10, description="Times each case is asked per configuration.")
    eval_concurrency: int = Field(default=3, ge=1, le=8, description="Questions in flight at once.")
    eval_forbidden_table: str = Field(
        default="", description="A table consumers cannot read; the preflight fails if the tester can."
    )
    eval_timeout_minutes: int = Field(default=180, ge=5)
    drift_window_days: int = Field(
        default=30, ge=1, description="Production traffic eval set suggestions look at."
    )
    drift_min_questions: int = Field(
        default=2, ge=1, description="Questions with one intent before a missing case is suggested."
    )

    platform_commit: str = Field(
        default="", description="Git commit of the deployed platform (the bundle sets it), for manifests."
    )

    genie_concurrency_per_space: int = Field(default=4, ge=1)
    max_concurrent_statements: int = Field(default=4, ge=1)
    permission_cache_ttl_seconds: int = Field(default=300, ge=0)
    http_timeout_seconds: float = Field(default=60.0, gt=0)

    @property
    def table_prefix(self) -> str:
        """Fully-qualified `<catalog>.<schema>` for Delta tables."""
        return f"{self.catalog}.{self.db_schema}"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings (cached); override in tests with `app.dependency_overrides`."""
    return Settings()


def apply_overrides(pairs: list[str] | None) -> None:
    """Apply `KEY=VALUE` pairs as `EVALOPS_KEY` env vars before settings load.

    Jobs pass configuration as task parameters (`--set warehouse_id=...`), because serverless
    job environments have no per-task environment variables.
    """
    for pair in pairs or []:
        key, _, value = pair.partition("=")
        os.environ[f"EVALOPS_{key.strip().upper()}"] = value
    get_settings.cache_clear()
