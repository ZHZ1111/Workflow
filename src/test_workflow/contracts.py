"""Strict, versioned handoffs and a deliberately narrow state patch."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, StrictBool, StrictInt, StringConstraints, computed_field, field_validator, model_validator
from testgen_agent.models import (
    AgentAction, CandidateRound, ContractModel, Count, Issue, RoundNumber,
    Text, TokenUsage, CoverageResult, ExecutionResult, StopReason, TaskStatus,
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
    kind: Literal["candidate", "tests", "demo_observation", "execution", "coverage", "report",
                  "agentdev_candidate", "plan", "llm_result", "subject", "assessment", "execution_evidence"]
    sha256: Digest
    round: RoundNumber
    candidate_id: Text

    _relative = field_validator("path")(relative_path)


class ExecutionFeedback(ContractModel):
    candidate_id: Text
    execution: ExecutionResult | None = None
    coverage: CoverageResult | None = None
    validation_error: Text | None = None
    issues: tuple[Issue, ...] = ()
    protected_tests: tuple[Text, ...] = ()
    source_hints_json: str | None = None

    @model_validator(mode="after")
    def consistent_feedback(self):
        if self.coverage is not None and self.execution is None:
            raise ValueError("coverage feedback requires an execution")
        if self.validation_error and self.execution:
            raise ValueError("a rejected candidate cannot have execution feedback")
        if self.source_hints_json is not None and not isinstance(json.loads(self.source_hints_json), dict):
            raise ValueError("source hints must encode an object")
        return self


class ExecutionAssessment(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate_id: Text
    observation_mode: Literal["real", "injected"] = "real"
    tool_calls: Annotated[StrictInt, Field(ge=0, le=1)] = 0
    pytest_started: StrictBool = False
    action: AgentAction
    recommendation: Literal["finish", "repair_tests", "extend_tests", "request_review", "stop_error"]
    reason: Text
    stop_reason: StopReason | None = None
    task_status: TaskStatus | None = None  # Advice, not an orchestrator commit.
    feedback: ExecutionFeedback
    evidence: tuple[ArtifactRef, ...] = ()
    integrity_error: StableId | None = None

    @model_validator(mode="after")
    def consistent_observation(self):
        if self.candidate_id != self.feedback.candidate_id:
            raise ValueError("assessment feedback belongs to another candidate")
        if self.pytest_started != bool(self.feedback.execution and self.feedback.execution.kind != "launch_error"):
            raise ValueError("pytest start flag differs from the execution observation")
        if (self.feedback.execution is not None) != bool(self.tool_calls):
            raise ValueError("execution observation requires exactly one tool invocation")
        if any(item.candidate_id != self.candidate_id for item in self.evidence):
            raise ValueError("assessment evidence belongs to another candidate")
        expected = {AgentAction.FINISH: "finish", AgentAction.REQUEST_REVIEW: "request_review",
                    AgentAction.REPAIR_GENERATED_TEST: "repair_tests", AgentAction.ADD_MISSING_CASES: "extend_tests"}.get(self.action)
        if self.stop_reason in {StopReason.UNRECOVERABLE_ERROR, StopReason.EXECUTION_TIMEOUT}:
            expected = "stop_error"
        if self.recommendation != expected:
            raise ValueError("public recommendation differs from the vendor action")
        if self.task_status == TaskStatus.COMPLETED and (
            self.action != AgentAction.FINISH or self.stop_reason != StopReason.COVERAGE_TARGET_MET
            or not self.feedback.execution or not self.feedback.execution.all_passed
            or not self.feedback.coverage or not self.feedback.coverage.meets_target
            or self.integrity_error or self.feedback.issues
        ):
            raise ValueError("completion advice requires verified passing results and coverage")
        return self


class MessagePayload(ContractModel):
    candidate_id: Text
    artifact: ArtifactRef
    reason: Text
    recommended_action: AgentAction | None = None
    feedback: ExecutionFeedback | None = None

    @model_validator(mode="after")
    def same_candidate(self):
        if self.candidate_id != self.artifact.candidate_id:
            raise ValueError("message and artifact candidate IDs disagree")
        if self.feedback is not None and self.feedback.candidate_id != self.candidate_id:
            raise ValueError("structured feedback belongs to another candidate")
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
    token_usage_reported: StrictBool = False


class GenerationTelemetry(ContractModel):
    """One generation action; offline/model transport counts remain distinct."""

    client_mode: Literal["offline", "mock", "real"]
    client_calls: Annotated[StrictInt, Field(ge=0, le=1)] = 0
    request_attempts: Count | None = 0
    max_request_attempts: Annotated[StrictInt, Field(ge=1, le=7)] = 5
    format_repairs: Annotated[StrictInt, Field(ge=0, le=2)] | None = 0
    total_usage: TokenUsage | None = None
    error_category: Literal["transient", "authentication", "configuration", "format",
                            "workspace", "budget", "contract", "internal"] | None = None
    provider_code: str | None = None
    retryable: StrictBool = False
    status_code: Annotated[StrictInt, Field(ge=100, le=599)] | None = None

    @computed_field
    @property
    def external_requests(self) -> int | None:
        return self.request_attempts if self.client_mode == "real" else 0


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
    diagnostic_paths: tuple[Text, ...] = ()
    generation: GenerationTelemetry | None = None
    assessment: ExecutionAssessment | None = None

    @field_validator("diagnostic_paths")
    @classmethod
    def relative_diagnostics(cls, paths):
        return tuple(relative_path(path) for path in paths)

    @model_validator(mode="after")
    def consistent_error(self):
        if (self.status == "error") != (self.error_code is not None):
            raise ValueError("error results require an error code")
        if self.status == "error" and (self.messages or self.artifacts or self.assessment or self.patch != StatePatch()):
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
