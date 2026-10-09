"""Workflow metadata wraps (rather than duplicates) AgentDev business state."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictInt, model_validator
from testgen_agent.models import AgentState, ContractModel, Count, RoundNumber, StopReason, TaskStatus

from .config import WorkflowConfig
from .contracts import ArtifactRef, Message, StableId
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
        if (self.workflow_status in {"finished", "error"}) != (self.finished_at is not None):
            raise ValueError("terminal states require a finish time")
        if (self.workflow_status == "error") != (self.error_code is not None):
            raise ValueError("error states require an error code")
        if self.config.mode == "demo" and self.task_status is not None:
            raise ValueError("demo handoff cannot claim a measured test conclusion")
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
