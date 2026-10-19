"""Workflow metadata wraps (rather than duplicates) AgentDev business state."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator
from test_workflow.agents.test_generation.core.models import AgentState, ContractModel, Count, RoundNumber, StopReason, TaskStatus

from .config import WorkflowConfig
from .contracts import ArtifactRef, Message, QualityAssessment, StableId
from .quality_contracts import QualityStatus, QualityStopReason
from .quality_state import validate_quality_completion
from .storage import atomic_write


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowState(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: StableId
    run_dir: Path
    workflow_status: Literal["pending", "running", "finished", "error"] = "pending"
    task_status: TaskStatus | None = None
    current_node: StableId | None = None
    current_round: Annotated[StrictInt, Field(ge=0, le=3)] = 0
    config: WorkflowConfig
    agent_state: AgentState
    selected_round: RoundNumber | None = None
    stop_reason: StopReason | None = None
    last_event_seq: Count = 0
    messages: tuple[Message, ...] = ()
    consumed_message_ids: tuple[StableId, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    started_at: datetime = Field(default_factory=utc_now)
    finished_at: datetime | None = None
    error_code: StableId | None = None
    quality_status: QualityStatus = "disabled"
    quality_stop_reason: QualityStopReason | None = None
    quality_assessments: Annotated[tuple[QualityAssessment, ...], Field(max_length=3)] = ()

    @model_validator(mode="before")
    @classmethod
    def default_quality_status(cls, value):
        if isinstance(value, dict) and "quality_status" not in value:
            config = value.get("config")
            enabled = config.quality_review_enabled if isinstance(config, WorkflowConfig) else (
                isinstance(config, dict) and config.get("quality_review_enabled") is True
            )
            value = {**value, "quality_status": "not_run" if enabled else "disabled"}
        return value

    @model_validator(mode="after")
    def consistent_state(self):
        business = self.agent_state
        if business.run_id != self.run_id:
            raise ValueError("business state belongs to another task")
        if not self.run_dir.is_absolute():
            raise ValueError("run directory must be absolute")
        if business.request.output_dir != self.run_dir / "agent_workspace":
            raise ValueError("AgentDev workspace must be the task's inner directory")
        if self.current_round > self.config.max_rounds:
            raise ValueError("workflow round exceeds budget")
        if (business.request.max_rounds, business.request.execution_timeout_seconds) != (
            self.config.max_rounds, self.config.execution_timeout_seconds,
        ):
            raise ValueError("workflow and business budgets disagree")
        if self.config.model is not None and business.request.model != self.config.model:
            raise ValueError("workflow and business model metadata disagree")
        ids = [message.message_id for message in self.messages]
        if len(ids) != len(set(ids)) or any(message.run_id != self.run_id for message in self.messages):
            raise ValueError("message IDs must be unique within this task")
        if len(set(self.consumed_message_ids)) != len(self.consumed_message_ids) or not set(self.consumed_message_ids).issubset(ids):
            raise ValueError("consumed messages must reference unique saved messages")
        for artifact in self.artifacts:
            if artifact.candidate_id != f"{self.run_id}:{artifact.round}":
                raise ValueError("artifact belongs to a different task or round")
        if len({artifact.path for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError("artifact paths must be unique")
        if self.selected_round != business.best_available_round:
            raise ValueError("selected round differs from business state")
        if not self.config.quality_review_enabled:
            if self.quality_status != "disabled" or self.quality_stop_reason is not None or self.quality_assessments:
                raise ValueError("disabled review cannot carry quality conclusions")
        else:
            if self.quality_status == "disabled":
                raise ValueError("enabled review must have an explicit quality state")
            rounds = [item.round for item in self.quality_assessments]
            if rounds != sorted(set(rounds)) or any(
                item.run_id != self.run_id or item.round > self.current_round or item.round > len(business.history)
                for item in self.quality_assessments
            ):
                raise ValueError("quality assessments must uniquely identify committed candidates in round order")
            reasons = {"not_run": {None}, "passed": {"approved"},
                       "changes_requested": {"quality_gaps", "quality_budget_reached", "quality_no_progress"},
                       "needs_review": {"ambiguous_specification"}, "error": {"review_error"}}
            deferred_no_progress = (
                self.quality_status == "not_run" and self.quality_stop_reason == "quality_no_progress"
                and self.task_status == TaskStatus.PARTIAL and self.stop_reason == StopReason.NO_PROGRESS
                and self.quality_assessments and self.quality_assessments[-1].round < self.current_round
                and self.quality_assessments[-1].decision.verdict == "request_revision"
            )
            if self.quality_stop_reason not in reasons[self.quality_status] and not deferred_no_progress:
                raise ValueError("quality status and stop reason disagree")
            verdict = {"passed": "approve", "changes_requested": "request_revision", "needs_review": "request_review"}.get(self.quality_status)
            if verdict and (not self.quality_assessments or self.quality_assessments[-1].round != self.current_round
                            or self.quality_assessments[-1].decision.verdict != verdict):
                raise ValueError("quality conclusion requires the current candidate's matching assessment")
            if self.quality_status == "not_run" and self.quality_assessments and self.quality_assessments[-1].round == self.current_round:
                raise ValueError("a reviewed candidate cannot be marked not_run")
        if (self.workflow_status in {"finished", "error"}) != (self.finished_at is not None):
            raise ValueError("terminal states require a finish time")
        if (self.workflow_status == "error") != (self.error_code is not None):
            raise ValueError("error states require an error code")
        if self.config.mode == "demo" and self.task_status is not None:
            raise ValueError("demo handoff cannot claim a measured test conclusion")
        if self.config.mode != "demo" and self.workflow_status in {"finished", "error"}:
            if self.task_status is None or self.stop_reason is None or self.stop_reason != business.stop_reason:
                raise ValueError("terminal testing workflows require a consistent business conclusion")
            if self.task_status == TaskStatus.COMPLETED:
                latest = business.history[-1] if business.history else None
                if (self.workflow_status != "finished" or self.stop_reason != StopReason.COVERAGE_TARGET_MET
                        or latest is None or latest.execution is None or not latest.execution.all_passed
                        or latest.coverage is None or not latest.coverage.meets_target):
                    raise ValueError("completion requires the latest real passing and target coverage evidence")
                validate_quality_completion(self)
        return self


class StateStore:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir.resolve()
        self.path = self.run_dir / "state.json"

    def save(self, state: WorkflowState) -> None:
        # Revalidate even when a caller bypassed validation via model_copy().
        state = WorkflowState.model_validate_json(state.canonical_json())
        if state.run_dir != self.run_dir:
            raise ValueError("state belongs to another run directory")
        atomic_write(self.path, state.canonical_json(indent=2) + "\n")

    def load(self) -> WorkflowState:
        state = WorkflowState.model_validate_json(self.path.read_text(encoding="utf-8"))
        if state.run_dir != self.run_dir:
            raise ValueError("saved state belongs to another run directory")
        return state
