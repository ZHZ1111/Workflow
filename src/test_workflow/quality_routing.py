"""Host checks and deterministic continuation policy for committed quality output."""

import hashlib
import json
import unicodedata

from .adapters.quality import load_review_inputs
from .contracts import AgentContext, AgentResult, QualityAssessment, StatePatch
from .quality_client import ReviewResponse
from .quality_contracts import Finding, validate_review_evidence
from .state import WorkflowState
from .storage import verify_artifact


def finding_fingerprint(finding: Finding) -> str:
    """Normalize evidence and the requested case, excluding model IDs and prose."""
    def normalized(value):
        return " ".join(unicodedata.normalize("NFKC", value or "").split())
    data = {"category": finding.category, "target_functions": sorted(finding.target_functions),
            "spec_quote": normalized(finding.spec_quote), "test_code_quote": normalized(finding.test_code_quote),
            "suggested_case": normalized(finding.suggested_case)}
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def quality_revision_stop(state: WorkflowState, quality: QualityAssessment) -> str | None:
    if quality.round >= state.config.max_rounds:
        return "quality_budget_reached"
    previous = tuple(item for item in state.quality_assessments if item.round < quality.round)
    if any(item.candidate_sha256 == quality.candidate_sha256 for item in previous):
        return "quality_no_progress"
    if previous and previous[-1].decision.verdict == "request_revision":
        before = {finding_fingerprint(item) for item in previous[-1].decision.findings if item.blocking}
        after = {finding_fingerprint(item) for item in quality.decision.findings if item.blocking}
        if before and before.issubset(after):
            return "quality_no_progress"
    return None


def validate_quality_result(state: WorkflowState, result: AgentResult, inbox) -> str | None:
    """Recheck proposals even when an injected node bypasses the S3 implementation."""
    quality = result.quality_assessment
    if quality is None or result.review is None or result.agent_id != "test_quality_review":
        raise ValueError("quality node requires a typed assessment and review telemetry")
    if result.patch != StatePatch():
        raise ValueError("quality review cannot patch business state")
    context = AgentContext(run_id=state.run_id, round=state.current_round, agent_id="test_quality_review",
                           run_dir=state.run_dir, snapshot_json=state.canonical_json(), inbox=inbox)
    prepared = load_review_inputs(context, result.review.max_request_attempts)
    expected = {"quality_assessment": f"quality_reviews/{state.current_round:02d}/assessment.json",
                "quality_response": f"quality_reviews/{state.current_round:02d}/response.json"}
    refs = {item.kind: item for item in result.artifacts}
    if len(result.artifacts) != 2 or set(refs) != set(expected) or any(refs[kind].path != path for kind, path in expected.items()):
        raise ValueError("quality result requires its two canonical output references")
    saved = QualityAssessment.model_validate_json(verify_artifact(state.run_dir, refs["quality_assessment"]).read_text(encoding="utf-8"))
    response = ReviewResponse.model_validate_json(verify_artifact(state.run_dir, refs["quality_response"]).read_text(encoding="utf-8"))
    tests = next(item for item in prepared.evidence if item.kind == "tests")
    if (saved != quality or response.decision != quality.decision or response.telemetry != result.review
            or quality.evidence != prepared.evidence or quality.execution_assessment != prepared.execution_assessment
            or quality.candidate_sha256 != tests.sha256
            or quality.spec_sha256 != hashlib.sha256(prepared.review.specification.encode("utf-8")).hexdigest()
            or (result.review.format_repairs is not None and result.review.format_repairs > 1)):
        raise ValueError("quality result differs from its host inputs or saved response")
    validate_review_evidence(quality.decision, specification=prepared.review.specification,
                             test_code=prepared.review.candidate.generated.code,
                             target_functions=tuple(item.name for item in prepared.review.source_info.functions))
    if quality.decision.verdict != "request_revision":
        if result.messages:
            raise ValueError("only a revision may propose quality feedback")
        return None
    stop = quality_revision_stop(state, quality)
    if quality.round >= state.config.max_rounds and result.messages:
        raise ValueError("last candidate cannot propose another generation")
    if stop is None and (len(result.messages) != 1 or result.messages[0].type != "quality_feedback"):
        raise ValueError("continuing review requires exactly one quality feedback proposal")
    return stop
