"""Strict, versioned handoffs and a deliberately narrow state patch."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, StringConstraints, field_validator, model_validator
from testgen_agent.models import (
    AgentAction, CandidateRound, ContractModel, Count, Issue, RoundNumber,
    Text, TokenUsage,
)

if TYPE_CHECKING:
    from .state import WorkflowState

StableId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]


def relative_path(value: str) -> str:
    windows = PureWindowsPath(value)
    if (
        not value or "\\" in value or ":" in value or windows.drive or windows.root
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("artifact paths must be task-relative POSIX paths")
    return value


class ArtifactRef(ContractModel):
    path: Text
    kind: Literal["candidate", "tests", "demo_observation", "execution", "coverage", "report"]
    sha256: Digest
    round: RoundNumber
    candidate_id: Text

    _relative = field_validator("path")(relative_path)


class MessagePayload(ContractModel):
    candidate_id: Text
    artifact: ArtifactRef
    reason: Text
    recommended_action: AgentAction | None = None

    @model_validator(mode="after")
    def same_candidate(self):
        if self.candidate_id != self.artifact.candidate_id:
            raise ValueError("message and artifact candidate IDs disagree")
        return self


class Message(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    message_id: StableId
    run_id: StableId
    round: RoundNumber  # Source round, including feedback messages.
    sender: StableId
    receiver: StableId
    type: Literal["candidate_ready", "execution_feedback", "demo_observation"]
    payload: MessagePayload

    @model_validator(mode="after")
    def matching_round(self):
        if self.payload.artifact.round != self.round:
            raise ValueError("message and artifact rounds disagree")
        if self.payload.candidate_id != f"{self.run_id}:{self.round}":
            raise ValueError("candidate ID must identify this task and source round")
        return self


class StatePatch(ContractModel):
    # A node can propose ONE candidate, never replace identity, request or history.
    candidate: CandidateRound | None = None
    issues: tuple[Issue, ...] = ()
    next_action: AgentAction | None = None
    api_calls_delta: Count = 0
    token_usage: TokenUsage | None = None


class AgentResult(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: StableId
    round: RoundNumber
    agent_id: StableId
    status: Literal["ok", "error"] = "ok"
    patch: StatePatch = Field(default_factory=StatePatch)
    messages: tuple[Message, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    error_code: StableId | None = None

    @model_validator(mode="after")
    def consistent_error(self):
        if (self.status == "error") != (self.error_code is not None):
            raise ValueError("error results require an error code")
        if self.status == "error" and (self.messages or self.artifacts or self.patch != StatePatch()):
            raise ValueError("error results cannot commit successful output")
        return self


class AgentContext(ContractModel):
    """Serialized snapshot; each state access decodes a fresh independent copy.

    Vendor models contain dictionaries despite being frozen. Passing their shared
    instances would allow a node to mutate nested plan inputs. JSON avoids that.
    """

    run_id: StableId
    round: RoundNumber
    agent_id: StableId
    run_dir: Path
    snapshot_json: Text
    inbox: tuple[Message, ...] = ()

    @property
    def state(self) -> WorkflowState:
        from .state import WorkflowState
        return WorkflowState.model_validate_json(self.snapshot_json)

    @model_validator(mode="after")
    def consistent_snapshot(self):
        state = self.state
        if (state.run_id, state.current_round, state.current_node, state.run_dir) != (
            self.run_id, self.round, self.agent_id, self.run_dir,
        ):
            raise ValueError("context metadata differs from its state snapshot")
        if any(message.run_id != self.run_id or message.receiver != self.agent_id for message in self.inbox):
            raise ValueError("context inbox belongs to a different task or node")
        return self
