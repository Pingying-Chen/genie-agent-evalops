"""Async client for the two Jobs API calls the app makes: trigger a run, read its state."""

import httpx

from evalops.clients.auth import Auth
from evalops.clients.http import request_json

SERVICE = "jobs"

# Jobs API life-cycle/result states -> the API's RunStatus.state
_STATES = {
    "QUEUED": "pending",
    "PENDING": "pending",
    "BLOCKED": "pending",
    "WAITING_FOR_RETRY": "pending",
    "RUNNING": "running",
    "TERMINATING": "running",
}
_RESULTS = {"SUCCESS": "succeeded", "CANCELED": "canceled", "CANCELLED": "canceled"}


class JobsClient:
    def __init__(self, http: httpx.AsyncClient, auth: Auth):
        self._http = http
        self._auth = auth

    async def run_now(self, job_id: int, job_parameters: dict[str, str]) -> int:
        """Start a run of `job_id` with job-level parameters; return the run id.

        Job parameters, not `python_params`: those would REPLACE the task's configured
        arguments (the `--set ...` settings) instead of adding to them.
        """
        body = await request_json(
            self._http,
            self._auth,
            "POST",
            "/api/2.1/jobs/run-now",
            service=SERVICE,
            json={"job_id": job_id, "job_parameters": job_parameters},
        )
        return int(body["run_id"])

    async def get_run(self, run_id: int) -> dict:
        return await request_json(
            self._http,
            self._auth,
            "GET",
            "/api/2.1/jobs/runs/get",
            service=SERVICE,
            params={"run_id": run_id},
        )


def run_state(run: dict) -> str:
    """Map a Jobs API run to pending / running / succeeded / failed / canceled."""
    state = run.get("state", {})
    life = state.get("life_cycle_state", "")
    if life in _STATES:
        return _STATES[life]
    return _RESULTS.get(state.get("result_state", ""), "failed")
