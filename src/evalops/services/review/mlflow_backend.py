"""`ReviewBackend` on MLflow 3 labeling sessions (Databricks Review App).

Imported only by the review job: MLflow and `databricks-agents` are the `review` extra,
so the app and the other jobs do not install them.

Learned on Free Edition (2026-10-20):
- label schema names allow only letters, digits and `_` (see `selection.schema_name`), and a
  schema referenced by a session can be neither overwritten nor deleted;
- label schemas belong to an experiment, so each space's experiment gets its own;
- a session is visible only to its assigned users, and assigning grants them WRITE on the
  experiment; a group grant alone lets members read traces but not find the session;
- `LabelingSession.add_traces` needs Trace objects (a str is parsed as trace JSON);
- a session's label schemas are fixed when it is created, so the session name carries a
  hash of its schema names and a new criterion starts a new session.
"""

import os

from evalops.schemas.checks import Criterion
from evalops.services.review.backend import (
    FINDINGS,
    FINDINGS_INSTRUCTION,
    FINDINGS_TITLE,
    TASK_SUCCESS,
    TASK_SUCCESS_INSTRUCTION,
    TASK_SUCCESS_TITLE,
    VIEW_VERSION,
    Assessment,
    TurnView,
    label_instruction,
)
from evalops.services.review.selection import schema_name

os.environ.setdefault("MLFLOW_ENABLE_ASYNC_TRACE_LOGGING", "false")  # traces exist once logged

import mlflow  # noqa: E402
from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.ml import ExperimentAccessControlRequest, ExperimentPermissionLevel  # noqa: E402
from mlflow.entities import AssessmentSource  # noqa: E402
from mlflow.exceptions import MlflowException  # noqa: E402
from mlflow.genai import label_schemas  # noqa: E402
from mlflow.genai.labeling import create_labeling_session, get_labeling_sessions  # noqa: E402


class MlflowBackend:
    def __init__(self, root: str = "", w: WorkspaceClient | None = None):
        mlflow.set_tracking_uri("databricks")
        self._w = w or WorkspaceClient()
        self._me = self._w.current_user.me().user_name
        # Default: the job identity's home, which no one else can read (unlike /Shared).
        self._root = root.rstrip("/") or f"/Users/{self._me}/genie-evalops-review"

    def ensure_experiment(self, space_id: str) -> str:
        self._w.workspace.mkdirs(self._root)
        return mlflow.set_experiment(f"{self._root}/{space_id}").experiment_id

    def set_reviewers(self, experiment_id: str, reviewers: list[str]) -> None:
        acl = [
            ExperimentAccessControlRequest(user_name=u, permission_level=ExperimentPermissionLevel.CAN_EDIT)
            for u in reviewers
        ]
        self._w.experiments.set_permissions(experiment_id, access_control_list=acl)

    def ensure_label_schemas(self, experiment_id: str, criteria: list[Criterion]) -> list[str]:
        """The turn-level questions first, then one schema per rule (display order)."""
        mlflow.set_experiment(experiment_id=experiment_id)
        wanted = [
            (
                TASK_SUCCESS,
                TASK_SUCCESS_TITLE,
                label_schemas.InputCategorical(options=["pass", "fail"]),
                TASK_SUCCESS_INSTRUCTION,
                True,
            ),
            (FINDINGS, FINDINGS_TITLE, label_schemas.InputText(max_length=2000), FINDINGS_INSTRUCTION, False),
        ] + [
            (
                schema_name(c.id, c.version),
                c.title[:200],  # the rule itself is in the instruction; ids mean nothing to reviewers
                label_schemas.InputCategorical(options=["pass", "fail", "na"]),
                label_instruction(c),
                True,
            )
            for c in criteria
        ]
        for name, title, field, instruction, comment in wanted:
            try:
                label_schemas.get_label_schema(name)
                continue  # immutable once a session uses it; a change is a new version
            except MlflowException:  # not found
                pass
            label_schemas.create_label_schema(
                name=name,
                type="feedback",
                title=title,
                input=field,
                instruction=instruction,
                enable_comment=comment,
            )
        return [name for name, *_ in wanted]

    def ensure_schema(self, experiment_id: str, name: str, title: str, instruction: str) -> None:
        mlflow.set_experiment(experiment_id=experiment_id)
        try:
            label_schemas.get_label_schema(name)
            return
        except MlflowException:  # not found
            pass
        label_schemas.create_label_schema(
            name=name,
            type="feedback",
            title=title,
            input=label_schemas.InputCategorical(options=["pass", "fail"]),
            instruction=instruction,
            enable_comment=True,
        )

    def ensure_session(
        self, experiment_id: str, name: str, reviewers: list[str], schemas: list[str]
    ) -> tuple[str, str]:
        mlflow.set_experiment(experiment_id=experiment_id)
        session = next((s for s in get_labeling_sessions() if s.name == name), None)
        if session is None:
            session = create_labeling_session(name=name, assigned_users=reviewers, label_schemas=schemas)
        elif sorted(session.assigned_users or []) != sorted(reviewers):
            session = session.set_assigned_users(reviewers)
        return session.labeling_session_id, session.url

    def find_traces(self, experiment_id: str) -> dict[str, str]:
        found = mlflow.search_traces(locations=[experiment_id], return_type="list", include_spans=False)
        return {
            t.info.tags["evalops.trace_id"]: t.info.trace_id
            for t in found
            if "evalops.trace_id" in t.info.tags and t.info.tags.get("evalops.view", "1") == VIEW_VERSION
        }

    def log_turn(self, experiment_id: str, turn: TurnView) -> str:
        mlflow.set_experiment(experiment_id=experiment_id)
        with mlflow.start_span(name="genie_agent_turn", span_type="AGENT") as root:
            root.set_inputs({"messages": turn.messages})
            _log_spans(turn.steps)
            root.set_outputs(turn.report)
            mlflow.update_current_trace(tags=turn.tags, request_preview=turn.title or None)
        return root.trace_id

    def add_to_session(self, experiment_id: str, session_id: str, mlflow_trace_ids: list[str]) -> None:
        mlflow.set_experiment(experiment_id=experiment_id)
        session = next(s for s in get_labeling_sessions() if s.labeling_session_id == session_id)
        # Trace objects, not ids: despite its type hint, add_traces parses a str as trace JSON.
        session.add_traces([mlflow.get_trace(i) for i in mlflow_trace_ids])

    def assessments(self, experiment_id: str) -> list[Assessment]:
        found = mlflow.search_traces(locations=[experiment_id], return_type="list", include_spans=False)
        return [
            Assessment(
                mlflow_trace_id=t.info.trace_id,
                name=a.name,
                source_type=str(a.source.source_type),
                source_id=a.source.source_id,
                value=getattr(a, "value", None),
                rationale=a.rationale,
                assessment_id=a.assessment_id,
                created_ms=a.create_time_ms,
            )
            for t in found
            for a in t.info.assessments or []
            if a.valid is not False  # superseded (overridden) assessments are invalid
        ]

    def log_judge(self, mlflow_trace_id: str, name: str, value: str, rationale: str, source_id: str) -> None:
        mlflow.log_feedback(
            trace_id=mlflow_trace_id,
            name=name,
            value=value,
            rationale=rationale,
            source=AssessmentSource(source_type="LLM_JUDGE", source_id=source_id),
        )


def _log_spans(spans: list[dict]) -> None:
    """A span per step; a dict with `children` is a turn grouping its own steps."""
    for step in spans:
        children = step.get("children")
        with mlflow.start_span(
            name=step["name"], span_type="CHAIN" if children is not None else "TOOL"
        ) as span:
            span.set_inputs(step["inputs"])
            span.set_outputs(step["outputs"])
            if children:
                _log_spans(children)
