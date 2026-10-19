"""Strict, versioned handoffs and a deliberately narrow state patch."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Annotated, Literal, TYPE_CHECKING

from pydantic import Field, StrictBool, StrictInt, StringConstraints, computed_field, field_validator, model_validator
from test_workflow.agents.test_generation.core.models import (
    AgentAction, CandidateRound, ContractModel, Count, Issue, RoundNumber,
    Text, TokenUsage, CoverageResult, ExecutionResult, StopReason, TaskStatus,
)

if TYPE_CHECKING:
    from .state import WorkflowState

from .quality_contracts import Finding, ReviewDecision, ReviewTelemetry

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
                  "agentdev_candidate", "plan", "llm_result", "subject", "assessment", "execution_evidence",
                  "quality_assessment", "quality_response"]
    sha256: Digest
    round: RoundNumber
    candidate_id: Text

    _relative = field_validator("path")(relative_path)


class QualityAssessment(ContractModel):
    """Host-owned identity and references; never accepted as raw model output."""

    schema_version: Literal["1.0"] = "1.0"
    run_id: StableId
    round: RoundNumber
    candidate_id: Text
    candidate_sha256: Digest  # Digest of the exact submitted tests file.
    spec_sha256: Digest  # UTF-8 bytes of the submitted specification snapshot.
    execution_assessment: ArtifactRef
    decision: ReviewDecision
    evidence: Annotated[tuple[ArtifactRef, ...], Field(min_length=1, max_length=64)]
    created_at: datetime

    @model_validator(mode="after")
    def consistent_identity(self):
        if self.candidate_id != f"{self.run_id}:{self.round}":
            raise ValueError("quality assessment belongs to another task or round")
        references = (self.execution_assessment, *self.evidence)
        if any(
            item.candidate_id != f"{self.run_id}:{item.round}"
            or (item.round != self.round and not (item.kind == "subject" and item.round < self.round))
            for item in references
        ):
            raise ValueError("quality evidence belongs to another candidate")
        if self.execution_assessment.kind != "assessment":
            raise ValueError("quality review requires an execution assessment reference")
        allowed = {"candidate", "tests", "plan", "subject", "execution", "coverage", "execution_evidence"}
        if any(item.kind not in allowed for item in self.evidence):
            raise ValueError("unsupported quality evidence kind")
        if len({item.path for item in references}) != len(references):
            raise ValueError("quality evidence paths must be unique")
        tests = tuple(item for item in self.evidence if item.kind == "tests")
        if len(tests) != 1 or tests[0].sha256 != self.candidate_sha256:
            raise ValueError("quality review must reference the exact submitted tests digest")
        if self.created_at.utcoffset() != timedelta(0):
            raise ValueError("quality assessment requires an aware UTC timestamp")
        return self


class QualityFeedback(ContractModel):
    candidate_id: Text
    assessment: ArtifactRef
    findings: Annotated[tuple[Finding, ...], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def actionable_feedback(self):
        if self.assessment.kind != "quality_assessment" or self.assessment.candidate_id != self.candidate_id:
            raise ValueError("quality feedback requires its matching quality assessment reference")
        # Reuse decision invariants; ambiguous/advisory findings cannot trigger generation.
        ReviewDecision(verdict="request_revision", summary="Validated quality feedback", findings=self.findings)
        if any(not finding.blocking for finding in self.findings):
            raise ValueError("quality feedback contains only authorized blocking findings")
        return self


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
    quality_feedback: QualityFeedback | None = None

    @model_validator(mode="after")
    def same_candidate(self):
        if self.candidate_id != self.artifact.candidate_id:
            raise ValueError("message and artifact candidate IDs disagree")
        if self.feedback is not None and self.feedback.candidate_id != self.candidate_id:
            raise ValueError("structured feedback belongs to another candidate")
        if self.quality_feedback is not None and (
            self.feedback is not None or self.quality_feedback.candidate_id != self.candidate_id
            or self.quality_feedback.assessment != self.artifact
            or self.recommended_action != AgentAction.ADD_MISSING_CASES
        ):
            raise ValueError("quality feedback requires a matching artifact and an exclusive extension action")
        return self


class Message(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    message_id: StableId
    run_id: StableId
    round: RoundNumber  # Source round, including feedback messages.
    sender: StableId
    receiver: StableId
    type: Literal["candidate_ready", "execution_feedback", "demo_observation", "review_ready", "quality_feedback"]
    payload: MessagePayload

    @model_validator(mode="after")
    def matching_round(self):
        if self.payload.artifact.round != self.round:
            raise ValueError("message and artifact rounds disagree")
        if self.payload.candidate_id != f"{self.run_id}:{self.round}":
            raise ValueError("candidate ID must identify this task and source round")
        if self.type == "review_ready" and (
            (self.sender, self.receiver) != ("test_execution", "test_quality_review")
            or self.payload.artifact.kind != "assessment" or self.payload.feedback is not None
            or self.payload.quality_feedback is not None
            or self.payload.recommended_action not in {None, AgentAction.FINISH}
        ):
            raise ValueError("review_ready requires an exclusive execution-to-review assessment handoff")
        if self.type == "quality_feedback":
            if (self.sender, self.receiver) != ("test_quality_review", "test_generation") or self.payload.quality_feedback is None:
                raise ValueError("quality feedback requires the review-to-generation route and typed findings")
        elif self.payload.quality_feedback is not None:
            raise ValueError("quality findings require a quality_feedback message")
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
    quality_assessment: QualityAssessment | None = None
    review: ReviewTelemetry | None = None

    @field_validator("diagnostic_paths")
    @classmethod
    def relative_diagnostics(cls, paths):
        return tuple(relative_path(path) for path in paths)

    @model_validator(mode="after")
    def consistent_error(self):
        if (self.status == "error") != (self.error_code is not None):
            raise ValueError("error results require an error code")
        if self.status == "error" and (self.messages or self.artifacts or self.assessment or self.quality_assessment or self.patch != StatePatch()):
            raise ValueError("error results cannot commit successful output")
        return self

    @model_validator(mode="after")
    def consistent_quality_output(self):
        quality_artifacts = tuple(item for item in self.artifacts if item.kind in {"quality_assessment", "quality_response"})
        if (self.quality_assessment is not None or self.review is not None or quality_artifacts
                or any(message.type == "quality_feedback" for message in self.messages)) and self.agent_id != "test_quality_review":
            raise ValueError("only the quality review node can submit review output")
        if self.agent_id == "test_quality_review":
            if self.patch != StatePatch() or self.generation is not None or self.assessment is not None:
                raise ValueError("review nodes cannot change business candidates or execution/generation observations")
            if self.status == "ok" and self.quality_assessment is None:
                raise ValueError("successful review requires a quality assessment")
        if self.quality_assessment is not None:
            quality = self.quality_assessment
            if (quality.run_id, quality.round) != (self.run_id, self.round):
                raise ValueError("quality result identity differs from its assessment")
            refs = tuple(item for item in quality_artifacts if item.kind == "quality_assessment")
            if len(refs) != 1 or any(
                (item.round, item.candidate_id) != (self.round, quality.candidate_id) for item in self.artifacts
            ):
                raise ValueError("quality output requires one matching quality assessment artifact")
            if any(item.kind not in {"quality_assessment", "quality_response"} for item in self.artifacts):
                raise ValueError("review nodes can only produce review artifacts")
            if len(self.messages) > 1 or any(message.type != "quality_feedback" for message in self.messages):
                raise ValueError("review nodes may only emit one quality feedback message")
            for message in self.messages:
                expected = tuple(finding for finding in quality.decision.findings if finding.blocking)
                if (quality.decision.verdict != "request_revision" or message.run_id != self.run_id
                        or message.round != self.round or message.payload.artifact != refs[0]
                        or message.payload.quality_feedback.findings != expected):
                    raise ValueError("quality feedback differs from its result assessment")
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
