"""Independent completion gate for a host-committed quality approval."""

import hashlib
from typing import TYPE_CHECKING

from testgen_agent.models import TaskStatus, TestPlan

from .contracts import ExecutionAssessment, QualityAssessment
from .quality_contracts import validate_review_evidence
from .storage import verify_artifact

if TYPE_CHECKING:
    from .state import WorkflowState


def validate_quality_completion(state: "WorkflowState") -> None:
    """An approval applies only to its selected tests, spec and execution evidence.

    Invoked at terminal state validation, not while constructing intermediate
    snapshots. Disabled legacy workflows do not read any additional files.
    """
    if not state.config.quality_review_enabled or state.task_status != TaskStatus.COMPLETED:
        return
    if state.quality_status != "passed" or state.quality_stop_reason != "approved" or state.selected_round is None:
        raise ValueError("quality-enabled completion requires an explicit approval")
    approvals = tuple(item for item in state.quality_assessments if item.round == state.selected_round)
    if len(approvals) != 1 or approvals[0].decision.verdict != "approve":
        raise ValueError("selected candidate has no unique quality approval")
    quality = approvals[0]
    if quality.round != state.current_round or quality.round != state.agent_state.current_round:
        raise ValueError("completion must select the current approved candidate")
    specification = state.agent_state.behavior_specification or ""
    if quality.spec_sha256 != hashlib.sha256(specification.encode("utf-8")).hexdigest():
        raise ValueError("quality approval belongs to another specification snapshot")
    refs = tuple(item for item in state.artifacts if item.kind == "quality_assessment" and item.round == quality.round)
    if len(refs) != 1:
        raise ValueError("quality approval requires one registered assessment artifact")
    candidate = state.agent_state.history[state.selected_round - 1]
    try:
        saved = QualityAssessment.model_validate_json(verify_artifact(state.run_dir, refs[0]).read_text(encoding="utf-8"))
        if saved != quality:
            raise ValueError("quality approval differs from its saved assessment")
        for ref in (quality.execution_assessment, *quality.evidence):
            if ref not in state.artifacts:
                raise ValueError("quality approval references unregistered evidence")
            verify_artifact(state.run_dir, ref)
        tests = next(item for item in quality.evidence if item.kind == "tests")
        path = verify_artifact(state.run_dir, tests)
        if candidate.test_path != path or candidate.generated is None or candidate.generated.code != path.read_text(encoding="utf-8"):
            raise ValueError("quality approval differs from the selected candidate tests")
        execution = ExecutionAssessment.model_validate_json(
            verify_artifact(state.run_dir, quality.execution_assessment).read_text(encoding="utf-8"))
        if (execution.candidate_id != quality.candidate_id or execution.observation_mode != "real"
                or execution.task_status != TaskStatus.COMPLETED
                or execution.feedback.execution != candidate.execution or execution.feedback.coverage != candidate.coverage):
            raise ValueError("quality approval requires the selected candidate's real completion evidence")
        for ref in execution.evidence:
            if ref not in state.artifacts:
                raise ValueError("quality completion requires registered execution evidence")
            verify_artifact(state.run_dir, ref)
        source = state.agent_state.source_info
        if source is None:
            raise ValueError("quality completion requires analyzed source facts")
        plans = tuple(item for item in quality.evidence if item.kind == "plan")
        subjects = tuple(item for item in quality.evidence if item.kind == "subject")
        if len(plans) != 1 or len(subjects) != 1:
            raise ValueError("quality completion requires the submitted plan and source snapshot")
        if (TestPlan.model_validate_json(verify_artifact(state.run_dir, plans[0]).read_text(encoding="utf-8")) != candidate.generated.plan
                or verify_artifact(state.run_dir, subjects[0]).read_bytes() != source.source_code.encode(source.encoding)):
            raise ValueError("quality approval belongs to another plan or source snapshot")
        validate_review_evidence(quality.decision, specification=specification, test_code=candidate.generated.code,
                                 target_functions=tuple(function.name for function in source.functions))
    except (OSError, StopIteration, SyntaxError) as exc:
        raise ValueError("quality approval evidence cannot be verified") from exc
