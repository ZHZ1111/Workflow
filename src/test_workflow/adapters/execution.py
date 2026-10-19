"""Execution gate, evidence registration and conservative AgentDev evaluation."""

import json
from pathlib import Path

from test_workflow.agents.test_generation.core.agent import ActionDecision, choose_action
from test_workflow.agents.test_generation.core.failure_analysis import classify_failures
from test_workflow.agents.test_generation.core.generation import CandidateValidationError, preserved_tests, validate_generated_tests
from test_workflow.agents.test_generation.core.models import AgentAction, CandidateRound, CoverageResult, ExecutionResult, Issue, StopReason, TaskStatus
from test_workflow.agents.test_generation.core.tools.coverage_runner import CoverageRunResult
from test_workflow.agents.test_generation.core.workspace import round_directory

from ..contracts import AgentContext, ArtifactRef, ExecutionAssessment, ExecutionFeedback
from ..storage import atomic_write, file_digest, task_path, verify_artifact


class ExecutionGateError(ValueError):
    """A fixed public code; never include source/client exception text."""
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def preflight_execution(context: AgentContext) -> tuple[CandidateRound, str | None]:
    state = context.state
    business = state.agent_state
    root = context.run_dir
    if (context.agent_id != "test_execution" or state.workflow_status != "running" or not business.history
            or business.current_round != context.round or business.history[-1].number != context.round):
        raise ExecutionGateError("invalid_execution_context")
    candidate = business.history[-1]
    if candidate.execution is not None or business.stop_reason is not None:
        raise ExecutionGateError("candidate_already_evaluated")
    if business.source_info is None or not (business.behavior_specification or "").strip():
        raise ExecutionGateError("missing_execution_input")
    workspace = business.request.output_dir
    if (root.is_symlink() or root.is_junction() or workspace != root / "agent_workspace"
            or workspace.is_symlink() or workspace.is_junction() or not workspace.is_dir()):
        raise ExecutionGateError("invalid_execution_workspace")
    saved_bytes = business.source_info.source_code.encode(business.source_info.encoding)
    if business.request.source_path.read_bytes() != saved_bytes:
        raise ExecutionGateError("source_input_changed")
    if task_path(root, "agent_workspace/subject.py").read_bytes() != saved_bytes:
        raise ExecutionGateError("source_snapshot_changed")
    assessment_dir = task_path(root, f"evaluations/{context.round:02d}", must_exist=False)
    executions = task_path(root, f"agent_workspace/{round_directory(context.round).as_posix()}/executions", must_exist=False)
    if assessment_dir.exists() or (executions.exists() and any(executions.iterdir())):
        raise ExecutionGateError("candidate_already_evaluated")

    references = tuple(item for item in state.artifacts if item.round == context.round and item.kind == "candidate")
    if len(references) != 1:
        raise ExecutionGateError("missing_candidate_reference")
    reference = references[0]
    saved = CandidateRound.model_validate_json(verify_artifact(root, reference).read_text(encoding="utf-8"))
    if saved != candidate:
        raise ExecutionGateError("candidate_handoff_changed")
    gate_error = candidate.validation_error
    blocked = gate_error is not None or candidate.generated is None or candidate.generated.code is None or bool(candidate.generated.questions)
    # Generation does not emit candidate_ready for invalid/ambiguous candidates.
    if not context.inbox and not blocked:
        raise ExecutionGateError("missing_candidate_message")
    if context.inbox:
        if len(context.inbox) != 1:
            raise ExecutionGateError("invalid_candidate_message")
        message = context.inbox[0]
        if (message.type != "candidate_ready" or message.sender != "test_generation"
                or message.round != context.round or message not in state.messages
                or message.message_id in state.consumed_message_ids or message.payload.artifact != reference):
            raise ExecutionGateError("invalid_candidate_message")
    for artifact in state.artifacts:
        if artifact.kind in {"tests", "subject", "plan", "llm_result", "agentdev_candidate"}:
            verify_artifact(root, artifact)
    if candidate.test_path is not None:
        expected = workspace / round_directory(context.round) / "test_subject.py"
        if candidate.test_path != expected or not any(
            item.kind == "tests" and item.round == context.round and verify_artifact(root, item) == expected
            for item in state.artifacts
        ):
            raise ExecutionGateError("invalid_execution_test_path")
        if expected.read_text(encoding="utf-8") != candidate.generated.code:
            raise ExecutionGateError("test_snapshot_changed")
    if not blocked:
        if candidate.test_path is None:
            raise ExecutionGateError("missing_execution_test_path")
        try:
            validate_generated_tests(candidate.generated, business.source_info)
        except CandidateValidationError as error:
            gate_error = error.summary
    return candidate, gate_error


def reference_file(context: AgentContext, path: Path, kind: str) -> ArtifactRef:
    relative = path.relative_to(context.run_dir).as_posix()
    path = task_path(context.run_dir, relative)
    return ArtifactRef(path=relative, kind=kind, sha256=file_digest(path), round=context.round,
                       candidate_id=f"{context.run_id}:{context.round}")


def validate_observation(context: AgentContext, observed: CoverageRunResult) -> tuple[CandidateRound, tuple[ArtifactRef, ...]]:
    if not isinstance(observed, CoverageRunResult):
        raise ExecutionGateError("invalid_execution_observation")
    workspace = context.state.agent_state.request.output_dir
    expected_parent = workspace / round_directory(context.round) / "executions"
    directory = observed.artifacts
    if (directory.is_symlink() or directory.is_junction() or directory.parent != expected_parent
            or not directory.name.startswith("run-") or not directory.is_dir()):
        raise ExecutionGateError("invalid_execution_evidence_directory")
    execution = ExecutionResult.model_validate_json(observed.execution.canonical_json())
    coverage = CoverageResult.model_validate_json(observed.coverage.canonical_json())
    if coverage.target_file != workspace / "subject.py":
        raise ExecutionGateError("invalid_coverage_target")
    saved_execution = ExecutionResult.model_validate_json(task_path(context.run_dir,
        (directory / "execution.json").relative_to(context.run_dir).as_posix()).read_text(encoding="utf-8"))
    saved_coverage = CoverageResult.model_validate_json(task_path(context.run_dir,
        (directory / "coverage_result.json").relative_to(context.run_dir).as_posix()).read_text(encoding="utf-8"))
    if (saved_execution, saved_coverage) != (execution, coverage):
        raise ExecutionGateError("execution_evidence_changed")
    references = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ExecutionGateError("invalid_execution_evidence_file")
        kind = "execution" if path.name == "execution.json" else "coverage" if path.name == "coverage_result.json" else "execution_evidence"
        references.append(reference_file(context, path, kind))
    return context.state.agent_state.history[-1].updated(execution=execution, coverage=coverage), tuple(references)


def evaluate(context: AgentContext, candidate: CandidateRound, gate_error: str | None,
             evidence: tuple[ArtifactRef, ...], *, tool_calls: int, observation_mode: str,
             integrity_error: str | None = None) -> ExecutionAssessment:
    business = context.state.agent_state
    provisional = candidate.updated(validation_error=gate_error) if gate_error is not None and candidate.execution is None else candidate
    history = (*business.history[:-1], provisional)
    local = business.updated(history=history)
    classified = classify_failures(local, provisional)
    decision = choose_action(local)
    issues = tuple(item for index, item in enumerate((*classified, *decision.issues))
                   if item not in (*classified, *decision.issues)[:index])
    if candidate.coverage is not None and candidate.coverage.status == "unavailable":
        issues += (Issue(kind="coverage_unavailable", summary="同次执行的目标覆盖率不可用，保留真实执行结果。",
                         evidence=(candidate.coverage.error,), round_number=context.round),)
    if integrity_error is not None:
        issues += (Issue(kind="other", summary="执行前后输入摘要不一致，停止自动处理。",
                         evidence=(integrity_error,), round_number=context.round),)
        decision = ActionDecision(action=AgentAction.FINISH, reason="输入完整性发生变化，保留已取得证据并停止。",
                                  stop_reason=StopReason.UNRECOVERABLE_ERROR,
                                  status=TaskStatus.PARTIAL if candidate.available else TaskStatus.FAILED)
    recommendation = {
        AgentAction.REPAIR_GENERATED_TEST: "repair_tests", AgentAction.ADD_MISSING_CASES: "extend_tests",
        AgentAction.REQUEST_REVIEW: "request_review", AgentAction.FINISH: "finish",
    }.get(decision.action)
    if recommendation is None:
        raise ExecutionGateError("invalid_evaluation_action")
    if decision.stop_reason in {StopReason.UNRECOVERABLE_ERROR, StopReason.EXECUTION_TIMEOUT}:
        recommendation = "stop_error"
    feedback = ExecutionFeedback(candidate_id=f"{context.run_id}:{context.round}", execution=candidate.execution,
                                 coverage=candidate.coverage, validation_error=gate_error, issues=issues,
                                 protected_tests=tuple(sorted(preserved_tests(history))),
                                 source_hints_json=json.dumps(decision.feedback, ensure_ascii=False, sort_keys=True))
    return ExecutionAssessment(candidate_id=feedback.candidate_id, observation_mode=observation_mode,
                               tool_calls=tool_calls,
                               pytest_started=bool(candidate.execution and candidate.execution.kind != "launch_error"),
                               action=decision.action, recommendation=recommendation, reason=decision.reason,
                               stop_reason=decision.stop_reason, task_status=decision.status,
                               feedback=feedback, evidence=evidence, integrity_error=integrity_error)


def save_assessment(context: AgentContext, assessment: ExecutionAssessment) -> ArtifactRef:
    path = task_path(context.run_dir, f"evaluations/{context.round:02d}/assessment.json", must_exist=False)
    path.parent.mkdir(parents=True, exist_ok=False)
    atomic_write(path, assessment.canonical_json(indent=2) + "\n")
    return reference_file(context, path, "assessment")
