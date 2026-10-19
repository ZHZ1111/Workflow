"""S1 protocol rejection, provenance, terminal approval and legacy compatibility."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_workflow.agents.test_generation.core.models import AgentAction, TokenUsage

from test_workflow.config import WorkflowConfig
from test_workflow.contracts import (
    AgentContext, AgentResult, ArtifactRef, Message, MessagePayload,
    QualityAssessment, QualityFeedback, StatePatch,
)
from test_workflow.quality_contracts import ReviewDecision, ReviewTelemetry, validate_review_evidence
from test_workflow.quality_state import validate_quality_completion
from test_workflow.service import fixture_client, run_workflow
from test_workflow.state import StateStore, WorkflowState
from test_workflow.storage import atomic_write, file_digest

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "examples/contracts/quality"
CODE = """def test_below_threshold():
    assert discounted_total(50) == 50

def test_above_threshold():
    assert discounted_total(150) == 135
"""
SPEC = "金额大于等于 100 时按九折计算。"


def decision(verdict="request_revision"):
    return ReviewDecision.model_validate_json((SAMPLES / f"{verdict}.json").read_text(encoding="utf-8"))


def reference(kind, *, round=1, sha256="a" * 64):
    return ArtifactRef(path=f"evidence/{round}/{kind}.json", kind=kind, sha256=sha256,
                       round=round, candidate_id=f"run-quality:{round}")


def assessment(verdict="request_revision"):
    return QualityAssessment(run_id="run-quality", round=1, candidate_id="run-quality:1",
                             candidate_sha256="a" * 64, spec_sha256="b" * 64,
                             execution_assessment=reference("assessment"), decision=decision(verdict),
                             evidence=(reference("tests"),), created_at=datetime.now(timezone.utc))


def feedback_message(quality):
    ref = reference("quality_assessment")
    feedback = QualityFeedback(candidate_id=quality.candidate_id, assessment=ref,
                               findings=tuple(item for item in quality.decision.findings if item.blocking))
    return Message(message_id="quality-feedback-1", run_id=quality.run_id, round=quality.round,
                   sender="test_quality_review", receiver="test_generation", type="quality_feedback",
                   payload=MessagePayload(candidate_id=quality.candidate_id, artifact=ref, reason="补充规格场景",
                                          recommended_action=AgentAction.ADD_MISSING_CASES, quality_feedback=feedback))


def review_result(quality):
    return AgentResult(run_id=quality.run_id, round=quality.round, agent_id="test_quality_review",
                       quality_assessment=quality, artifacts=(reference("quality_assessment"),))


@pytest.mark.parametrize("verdict", ["approve", "request_revision", "request_review"])
def test_business_samples_are_frozen_strict_and_roundtrip(verdict):
    value = decision(verdict)
    assert ReviewDecision.model_validate_json(value.canonical_json()) == value
    with pytest.raises(ValidationError):
        value.summary = "mutated"
    with pytest.raises(ValidationError):
        value.updated(run_id="model-controlled-host-metadata")


@pytest.mark.parametrize("change", [
    {"verdict": "approve"}, {"findings": ()}, {"questions": ("未确定的预期？",)},
    {"summary": " "}, {"summary": "x" * 2001}, {"findings": decision().findings * 21},
    {"questions": ("问题",) * 11},
])
def test_invalid_revision_or_approval_is_rejected(change):
    with pytest.raises(ValidationError):
        decision().updated(**change)


@pytest.mark.parametrize("change", [
    {"suggested_case": None}, {"spec_quote": None}, {"blocking": "true"},
    {"category": "redundancy"}, {"target_functions": ()}, {"target_functions": ("missing()",)},
    {"target_functions": ("discounted_total",) * 2}, {"related_tests": ("new_missing_test",)},
    {"related_tests": ("test_below_threshold",) * 51}, {"spec_quote": "x" * 4001},
])
def test_invalid_finding_is_rejected(change):
    with pytest.raises(ValidationError):
        decision().findings[0].updated(**change)


def test_verdicts_require_concrete_and_consistent_evidence():
    revision = decision()
    with pytest.raises(ValidationError):
        revision.updated(findings=revision.findings * 2)
    with pytest.raises(ValidationError):
        decision("request_review").updated(questions=())
    with pytest.raises(ValidationError):
        decision("request_review").updated(findings=())
    advisory = revision.findings[0].updated(blocking=False, category="redundancy", suggested_case=None, spec_quote=None)
    assert decision("approve").updated(findings=(advisory,)).verdict == "approve"
    with pytest.raises(ValidationError):
        revision.updated(findings=(advisory,))
    with pytest.raises(ValidationError):
        decision("approve").updated(findings=(advisory.updated(category="ambiguous_specification"),))


def test_quotes_and_name_references_use_the_actual_review_input():
    value = decision()
    validate_review_evidence(value, specification=SPEC, test_code=CODE, target_functions=("discounted_total",))
    for field, replacement in (
        ("target_functions", ("unknown",)), ("related_tests", ("test_missing",)),
        ("spec_quote", "规格没有说过的话"), ("test_code_quote", "assert False"),
    ):
        changed = value.updated(findings=(value.findings[0].updated(**{field: replacement}),))
        with pytest.raises(ValueError):
            validate_review_evidence(changed, specification=SPEC, test_code=CODE, target_functions=("discounted_total",))


@pytest.mark.parametrize("change", [
    {"candidate_id": "other:1"}, {"round": 2}, {"candidate_sha256": "c" * 64},
    {"spec_sha256": "not-a-sha"}, {"execution_assessment": reference("tests")},
    {"evidence": ()}, {"evidence": (reference("tests"), reference("tests"))},
    {"evidence": (reference("tests", round=2),)},
    {"evidence": (reference("tests"), reference("quality_response"))},
    {"created_at": datetime(2026, 10, 16)},
])
def test_host_assessment_rejects_invalid_identity_or_evidence(change):
    with pytest.raises(ValidationError):
        assessment().updated(**change)


def test_later_review_can_reuse_registered_source_but_not_other_candidate_evidence():
    value = assessment().updated(round=2, candidate_id="run-quality:2",
                                 execution_assessment=reference("assessment", round=2),
                                 evidence=(reference("tests", round=2), reference("subject")))
    assert value.evidence[-1].round == 1
    with pytest.raises(ValidationError):
        value.updated(evidence=(*value.evidence, reference("plan")))
    with pytest.raises(ValidationError):
        value.updated(evidence=(reference("tests", round=2), reference("subject").updated(candidate_id="foreign:1")))


def test_quality_messages_and_results_bind_to_the_same_assessment():
    value = assessment()
    message = feedback_message(value)
    result = review_result(value).updated(messages=(message,), review=ReviewTelemetry(client_mode="offline", client_calls=1))
    assert AgentResult.model_validate_json(result.canonical_json()) == result
    for change in ({"sender": "test_execution"}, {"receiver": "test_execution"}, {"type": "execution_feedback"}, {"round": 2}):
        with pytest.raises(ValidationError):
            message.updated(**change)
    for change in ({"agent_id": "test_execution"}, {"round": 2}, {"generation": {"client_mode": "offline"}},
                   {"patch": StatePatch(api_calls_delta=1)}, {"patch": StatePatch(next_action=AgentAction.FINISH)}):
        with pytest.raises(ValidationError):
            result.updated(**change)
    changed = value.decision.findings[0].updated(suggested_case="模型消息擅自替换的建议")
    different = message.updated(payload=message.payload.updated(quality_feedback=message.payload.quality_feedback.updated(findings=(changed,))))
    with pytest.raises(ValidationError):
        result.updated(messages=(different,))
    with pytest.raises(ValidationError):
        review_result(assessment("approve")).updated(messages=(message,))


def test_review_ready_is_an_exclusive_execution_to_review_handoff():
    payload = MessagePayload(candidate_id="run-quality:1", artifact=reference("assessment"), reason="基础执行已达标")
    value = Message(message_id="ready-1", run_id="run-quality", round=1, type="review_ready",
                    sender="test_execution", receiver="test_quality_review", payload=payload)
    assert Message.model_validate_json(value.canonical_json()) == value
    for change in ({"sender": "test_generation"}, {"receiver": "test_generation"},
                   {"payload": payload.updated(artifact=reference("tests"))},
                   {"payload": payload.updated(recommended_action=AgentAction.ADD_MISSING_CASES)}):
        with pytest.raises(ValidationError):
            value.updated(**change)


def test_error_result_may_preserve_review_telemetry_but_no_success_output():
    telemetry = ReviewTelemetry(client_mode="real", client_calls=1, request_attempts=3,
                                format_repairs=1, error_category="format")
    value = AgentResult(run_id="run-quality", round=1, agent_id="test_quality_review", status="error",
                        error_code="invalid_review_response", review=telemetry)
    assert value.review.external_requests == 3
    for change in ({"quality_assessment": assessment()}, {"artifacts": (reference("quality_response"),)},
                   {"messages": (feedback_message(assessment()),)}):
        with pytest.raises(ValidationError):
            value.updated(**change)
    with pytest.raises(ValidationError):
        AgentResult(run_id="run-quality", round=1, agent_id="test_execution", artifacts=(reference("quality_response"),))


@pytest.mark.parametrize("change", [
    {"request_attempts": 4}, {"max_request_attempts": 4}, {"client_calls": 0, "request_attempts": 1},
    {"request_attempts": 2, "max_request_attempts": 1}, {"request_attempts": 2, "format_repairs": 2},
    {"client_mode": "offline", "request_attempts": 1}, {"request_attempts": True},
])
def test_review_telemetry_enforces_one_shared_retry_budget(change):
    with pytest.raises(ValidationError):
        ReviewTelemetry(client_mode="real", client_calls=1).updated(**change)


def test_unknown_review_usage_is_not_counted_as_zero():
    value = ReviewTelemetry(client_mode="real", client_calls=1, request_attempts=None, format_repairs=None)
    assert value.external_requests is None and value.total_usage is None
    assert ReviewTelemetry(client_mode="mock", client_calls=1, request_attempts=2).external_requests == 0
    assert value.updated(total_usage=TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5)).total_usage.total_tokens == 5


@pytest.mark.parametrize("invalid", [0, 1, "true", "false", None])
def test_quality_switch_is_a_strict_bool(invalid):
    with pytest.raises(ValidationError):
        WorkflowConfig(quality_review_enabled=invalid)


def test_initial_quality_state_defaults_and_cross_run_rejection(initial):
    assert initial.quality_status == "disabled" and not initial.config.quality_review_enabled
    enabled = initial.model_dump(round_trip=True)
    enabled["config"]["quality_review_enabled"] = True
    enabled.pop("quality_status")
    assert WorkflowState.model_validate(enabled).quality_status == "not_run"
    with pytest.raises(ValidationError):
        initial.updated(quality_status="passed", quality_stop_reason="approved", quality_assessments=(assessment("approve"),))
    with pytest.raises(ValidationError):
        WorkflowState.model_validate(enabled).updated(quality_status="passed", quality_stop_reason="approved")


@pytest.mark.parametrize("relative,model", [
    ("p1/state.json", WorkflowState), ("p3/passed_result.json", AgentResult),
    ("p3/passed_context.json", AgentContext), ("p4/passed_state.json", WorkflowState),
    ("p4/revision_context.json", AgentContext), ("p4/feedback_message.json", Message),
    ("p6/summary_node_result.json", AgentResult),
])
def test_saved_legacy_contracts_still_load(relative, model, contract_example):
    value = contract_example(ROOT / "examples/contracts" / relative, model)
    assert model.model_validate_json(value.canonical_json()) == value


@pytest.fixture(scope="module")
def completed_legacy(tmp_path_factory):
    output = tmp_path_factory.mktemp("quality-completion") / "run"
    return run_workflow(source=ROOT / "examples/validation.py", specification=ROOT / "examples/validation.md",
                        output=output, config=WorkflowConfig(mode="offline"), client=fixture_client("validation"))


@pytest.fixture(scope="module")
def approved_state(completed_legacy):
    import hashlib

    old = completed_legacy
    assert old.task_status == "completed" and old.selected_round == 2
    execution = next(item for item in old.artifacts if item.kind == "assessment" and item.round == 2)
    evidence = tuple(item for item in old.artifacts if (item.round == 2 and item.kind in {"tests", "plan", "candidate", "execution", "coverage"})
                     or item.kind == "subject")
    tests = next(item for item in evidence if item.kind == "tests")
    quality = QualityAssessment(run_id=old.run_id, round=2, candidate_id=f"{old.run_id}:2", candidate_sha256=tests.sha256,
                                spec_sha256=hashlib.sha256(old.agent_state.behavior_specification.encode("utf-8")).hexdigest(),
                                execution_assessment=execution, decision=decision("approve"), evidence=evidence,
                                created_at=datetime.now(timezone.utc))
    path = old.run_dir / "quality_assessment.json"
    atomic_write(path, quality.canonical_json())
    ref = ArtifactRef(path=path.name, kind="quality_assessment", sha256=file_digest(path), round=2, candidate_id=quality.candidate_id)
    return old.updated(config=old.config.updated(quality_review_enabled=True), quality_status="passed",
                       quality_stop_reason="approved", quality_assessments=(quality,), artifacts=(*old.artifacts, ref))


def test_completed_quality_state_verifies_its_saved_approval(approved_state):
    validate_quality_completion(approved_state)
    assert WorkflowState.model_validate_json(approved_state.canonical_json()) == approved_state
    StateStore(approved_state.run_dir).save(approved_state)
    assert StateStore(approved_state.run_dir).load() == approved_state


def test_enabled_completion_cannot_fall_back_to_legacy_success(completed_legacy):
    with pytest.raises(ValidationError):
        completed_legacy.updated(config=completed_legacy.config.updated(quality_review_enabled=True), quality_status="not_run")


def test_completion_rejects_changed_spec_or_unapproved_selection(approved_state):
    for change in (
        {"agent_state": approved_state.agent_state.updated(behavior_specification="changed specification")},
        {"selected_round": 1, "agent_state": approved_state.agent_state.updated(best_available_round=1)},
        {"quality_assessments": approved_state.quality_assessments * 2},
        {"artifacts": tuple(item for item in approved_state.artifacts if item.kind != "quality_assessment")},
        {"artifacts": tuple(item for item in approved_state.artifacts if item.kind != "plan")},
        {"quality_status": "not_run", "quality_stop_reason": None},
    ):
        with pytest.raises(ValidationError):
            approved_state.updated(**change)


@pytest.mark.parametrize("kind", ["tests", "assessment", "quality_assessment", "plan", "subject"])
def test_completion_rejects_tampered_saved_evidence(approved_state, kind):
    ref = next(item for item in approved_state.artifacts if item.kind == kind and (item.round == 2 or kind == "subject"))
    path = approved_state.run_dir / ref.path
    original = path.read_bytes()
    try:
        path.write_bytes(original + b"\n ")
        with pytest.raises(ValidationError):
            WorkflowState.model_validate_json(approved_state.canonical_json())
    finally:
        path.write_bytes(original)


def test_completion_binds_analyzed_source_and_plan_to_the_approved_files(approved_state):
    source = approved_state.agent_state.source_info
    with pytest.raises(ValidationError):
        approved_state.updated(agent_state=approved_state.agent_state.updated(
            source_info=source.updated(source_code=source.source_code + "\n# changed snapshot\n")))
    candidate = approved_state.agent_state.history[-1]
    plan = candidate.generated.plan
    changed = candidate.updated(generated=candidate.generated.updated(plan=plan.updated(
        scenarios=(plan.scenarios[0].updated(expected_behavior="changed behavior"), *plan.scenarios[1:]))))
    with pytest.raises(ValidationError):
        approved_state.updated(agent_state=approved_state.agent_state.updated(history=(*approved_state.agent_state.history[:-1], changed)))


def test_quality_status_resets_for_a_new_candidate(approved_state):
    running = approved_state.updated(workflow_status="running", finished_at=None, task_status=None,
                                     stop_reason=None, quality_status="not_run", quality_stop_reason=None, current_round=3)
    assert running.quality_assessments[-1].round == 2 and running.quality_status == "not_run"
    with pytest.raises(ValidationError):
        running.updated(quality_status="passed", quality_stop_reason="approved")
