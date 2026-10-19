"""Review input gates, evidence provenance and host-owned output artifacts."""

from dataclasses import dataclass
import hashlib
from pathlib import Path

from test_workflow.agents.test_generation.core.agent import choose_action
from test_workflow.agents.test_generation.core.generation import validate_generated_tests
from test_workflow.agents.test_generation.core.models import (
    AgentAction, CandidateRound, CoverageResult, ExecutionResult, StopReason, TaskStatus, TestPlan,
)
from test_workflow.agents.test_generation.core.workspace import round_directory

from ..contracts import AgentContext, ArtifactRef, ExecutionAssessment, QualityAssessment
from ..quality_client import ReviewResponse
from ..quality_contracts import validate_review_evidence
from ..quality_prompts import ReviewContext
from ..state import utc_now
from ..storage import atomic_write, file_digest, task_path, verify_artifact


class QualityGateError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PreparedReview:
    review: ReviewContext
    execution_assessment: ArtifactRef
    evidence: tuple[ArtifactRef, ...]


def review_directory(context: AgentContext) -> Path:
    root = context.run_dir
    base = root / "quality_reviews"
    if root.is_symlink() or root.is_junction() or base.is_symlink() or base.is_junction():
        raise QualityGateError("invalid_quality_workspace")
    path = base / f"{context.round:02d}"
    if path.is_symlink() or path.is_junction():
        raise QualityGateError("invalid_quality_workspace")
    return task_path(root, f"quality_reviews/{context.round:02d}", must_exist=False)


def load_review_inputs(context: AgentContext, max_request_attempts: int) -> PreparedReview:
    state = context.state
    business = state.agent_state
    root, number = context.run_dir, context.round
    if (context.agent_id != "test_quality_review" or state.workflow_status != "running"
            or not state.config.quality_review_enabled or state.config.mode == "demo"
            or state.task_status is not None or state.stop_reason is not None or business.stop_reason is not None
            or business.current_round != number or not business.history or business.history[-1].number != number
            or state.quality_status != "not_run"):
        raise QualityGateError("invalid_quality_context")
    if any(item.round == number for item in state.quality_assessments) or any(
        item.round == number and item.kind in {"quality_assessment", "quality_response"} for item in state.artifacts
    ):
        raise QualityGateError("candidate_already_reviewed")
    if business.source_info is None or not (business.behavior_specification or "").strip():
        raise QualityGateError("missing_quality_input")
    workspace = business.request.output_dir
    if (root.is_symlink() or root.is_junction() or workspace != root / "agent_workspace"
            or workspace.is_symlink() or workspace.is_junction() or not workspace.is_dir()):
        raise QualityGateError("invalid_quality_workspace")
    source_bytes = business.source_info.source_code.encode(business.source_info.encoding)
    if business.request.source_path.read_bytes() != source_bytes:
        raise QualityGateError("quality_source_input_changed")
    if (business.request.spec_path is None
            or business.request.spec_path.read_text(encoding="utf-8") != business.behavior_specification):
        raise QualityGateError("quality_spec_input_changed")

    candidate = business.history[-1]
    if (candidate.generated is None or candidate.generated.code is None or candidate.generated.questions
            or candidate.validation_error is not None or not candidate.execution or not candidate.execution.all_passed
            or not candidate.coverage or not candidate.coverage.meets_target):
        raise QualityGateError("quality_base_not_completed")
    decision = choose_action(business)
    if (decision.action, decision.stop_reason, decision.status) != (
        AgentAction.FINISH, StopReason.COVERAGE_TARGET_MET, TaskStatus.COMPLETED,
    ) or business.next_action != AgentAction.FINISH:
        raise QualityGateError("quality_base_not_completed")

    def one(kind, path=None, *, shared=False):
        refs = tuple(item for item in state.artifacts if item.kind == kind and (shared or item.round == number))
        if len(refs) != 1 or (path is not None and refs[0].path != path):
            raise QualityGateError("invalid_quality_reference")
        return refs[0]

    current_dir = f"agent_workspace/{round_directory(number).as_posix()}"
    candidate_ref = one("candidate", f"handoffs/{number:02d}/candidate.json")
    tests_ref = one("tests", f"{current_dir}/test_subject.py")
    plan_ref = one("plan", f"{current_dir}/plan.json")
    subject_ref = one("subject", "agent_workspace/subject.py", shared=True)
    if subject_ref.round != 1:
        raise QualityGateError("invalid_quality_reference")
    assessment_ref = one("assessment", f"evaluations/{number:02d}/assessment.json")
    if len(context.inbox) != 1:
        raise QualityGateError("invalid_review_ready")
    message = context.inbox[0]
    pending = tuple(item for item in state.messages if item.receiver == "test_quality_review"
                    and item.message_id not in state.consumed_message_ids)
    if (message.type != "review_ready" or message.sender != "test_execution" or message.round != number
            or message.payload.artifact != assessment_ref or message not in state.messages
            or message.message_id in state.consumed_message_ids or pending != (message,)):
        raise QualityGateError("invalid_review_ready")

    # Recheck generation evidence, including protected historical inputs.
    for ref in state.artifacts:
        if ref.kind in {"candidate", "tests", "plan", "subject", "agentdev_candidate", "llm_result"}:
            verify_artifact(root, ref)
    baseline = CandidateRound.model_validate_json(verify_artifact(root, candidate_ref).read_text(encoding="utf-8"))
    if baseline != candidate.updated(execution=None, coverage=None):
        raise QualityGateError("quality_candidate_changed")
    test_path = verify_artifact(root, tests_ref)
    if candidate.test_path != test_path or test_path.read_text(encoding="utf-8") != candidate.generated.code:
        raise QualityGateError("quality_tests_changed")
    if TestPlan.model_validate_json(verify_artifact(root, plan_ref).read_text(encoding="utf-8")) != candidate.generated.plan:
        raise QualityGateError("quality_plan_changed")
    if verify_artifact(root, subject_ref).read_bytes() != source_bytes:
        raise QualityGateError("quality_source_snapshot_changed")
    validate_generated_tests(candidate.generated, business.source_info)

    assessment = ExecutionAssessment.model_validate_json(verify_artifact(root, assessment_ref).read_text(encoding="utf-8"))
    if (assessment.observation_mode != "real" or assessment.task_status != TaskStatus.COMPLETED
            or assessment.candidate_id != f"{state.run_id}:{number}" or assessment.integrity_error
            or assessment.feedback.issues or assessment.feedback.validation_error is not None
            or assessment.feedback.execution != candidate.execution or assessment.feedback.coverage != candidate.coverage):
        raise QualityGateError("invalid_quality_execution_assessment")
    paths = [item.path for item in assessment.evidence]
    if len(paths) != len(set(paths)):
        raise QualityGateError("invalid_quality_execution_evidence")
    parents = set()
    for ref in assessment.evidence:
        if (ref not in state.artifacts or ref.round != number or ref.candidate_id != assessment.candidate_id
                or ref.kind not in {"execution", "coverage", "execution_evidence"}):
            raise QualityGateError("invalid_quality_execution_evidence")
        path = verify_artifact(root, ref)
        if (path.parent.parent != workspace / round_directory(number) / "executions"
                or not path.parent.name.startswith("run-")):
            raise QualityGateError("invalid_quality_execution_evidence")
        parents.add(path.parent)
    executions = tuple(ref for ref in assessment.evidence if ref.kind == "execution")
    coverages = tuple(ref for ref in assessment.evidence if ref.kind == "coverage")
    if len(executions) != 1 or len(coverages) != 1 or len(parents) != 1:
        raise QualityGateError("invalid_quality_execution_evidence")
    saved_execution = ExecutionResult.model_validate_json(verify_artifact(root, executions[0]).read_text(encoding="utf-8"))
    saved_coverage = CoverageResult.model_validate_json(verify_artifact(root, coverages[0]).read_text(encoding="utf-8"))
    if (saved_execution != candidate.execution or saved_coverage != candidate.coverage
            or saved_coverage.target_file != workspace / "subject.py"):
        raise QualityGateError("quality_execution_observation_changed")

    for old in state.quality_assessments:
        refs = tuple(ref for ref in state.artifacts if ref.kind == "quality_assessment" and ref.round == old.round)
        if (len(refs) != 1 or old.spec_sha256 != hashlib.sha256(business.behavior_specification.encode("utf-8")).hexdigest()
                or QualityAssessment.model_validate_json(verify_artifact(root, refs[0]).read_text(encoding="utf-8")) != old):
            raise QualityGateError("invalid_quality_history")
        for ref in (old.execution_assessment, *old.evidence):
            if ref not in state.artifacts:
                raise QualityGateError("invalid_quality_history")
            verify_artifact(root, ref)

    review = ReviewContext(run_id=state.run_id, round=number, candidate_id=assessment.candidate_id,
                           specification=business.behavior_specification, source_info=business.source_info,
                           candidate=candidate, execution_assessment=assessment, history_reviews=state.quality_assessments,
                           max_rounds=state.config.max_rounds, max_request_attempts=max_request_attempts)
    evidence = (candidate_ref, tests_ref, plan_ref, subject_ref, *assessment.evidence)
    if len(evidence) > 64 or len({ref.path for ref in evidence}) != len(evidence):
        raise QualityGateError("invalid_quality_reference")
    return PreparedReview(review, assessment_ref, evidence)


def preflight_quality(context: AgentContext, max_request_attempts: int = 3) -> PreparedReview:
    prepared = load_review_inputs(context, max_request_attempts)
    if review_directory(context).exists():
        raise QualityGateError("candidate_already_reviewed")
    return prepared


def validate_quality_response(context: AgentContext, prepared: PreparedReview, response: ReviewResponse) -> None:
    if load_review_inputs(context, prepared.review.max_request_attempts) != prepared:
        raise QualityGateError("quality_input_changed_during_review")
    if any(review_directory(context).iterdir()):
        raise QualityGateError("quality_output_changed_during_review")
    try:
        validate_review_evidence(response.decision, specification=prepared.review.specification,
                                 test_code=prepared.review.candidate.generated.code,
                                 target_functions=tuple(item.name for item in prepared.review.source_info.functions))
    except (ValueError, SyntaxError):
        raise QualityGateError("quality_evidence_invalid") from None


def save_quality_assessment(context: AgentContext, prepared: PreparedReview,
                            response: ReviewResponse) -> tuple[QualityAssessment, tuple[ArtifactRef, ...]]:
    directory = review_directory(context)
    tests_ref = next(ref for ref in prepared.evidence if ref.kind == "tests")
    quality = QualityAssessment(run_id=context.run_id, round=context.round, candidate_id=prepared.review.candidate_id,
                                candidate_sha256=tests_ref.sha256,
                                spec_sha256=hashlib.sha256(prepared.review.specification.encode("utf-8")).hexdigest(),
                                execution_assessment=prepared.execution_assessment, decision=response.decision,
                                evidence=prepared.evidence, created_at=utc_now())
    paths = ((directory / "response.json", "quality_response", response.canonical_json(indent=2)),
             (directory / "assessment.json", "quality_assessment", quality.canonical_json(indent=2)))
    references = []
    for path, kind, content in paths:
        atomic_write(path, content + "\n")
        ref = ArtifactRef(path=path.relative_to(context.run_dir).as_posix(), kind=kind,
                           sha256=file_digest(path), round=context.round, candidate_id=quality.candidate_id)
        verify_artifact(context.run_dir, ref)
        references.append(ref)
    if (ReviewResponse.model_validate_json(verify_artifact(context.run_dir, references[0]).read_text(encoding="utf-8")) != response
            or QualityAssessment.model_validate_json(verify_artifact(context.run_dir, references[1]).read_text(encoding="utf-8")) != quality):
        raise QualityGateError("quality_saved_output_changed")
    return quality, tuple(references)
