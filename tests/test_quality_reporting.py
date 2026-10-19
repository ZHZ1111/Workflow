"""S7 traceable quality decisions, complete costs and strict read-only inspection."""

import json
from unittest.mock import patch

import pytest
from test_workflow.agents.test_generation.core.models import TokenUsage

from test_workflow.contracts import AgentContext, AgentResult, GenerationTelemetry
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_quality_review import TestQualityReviewAgent
from test_workflow.quality_client import ReviewResponse, ReviewClientError, ReviewErrorCode
from test_workflow.quality_contracts import ReviewTelemetry
from test_workflow.report_metrics import model_usage
from test_workflow.reporting import build_summary, inspect_summary
from test_workflow.storage import file_digest
from test_workflow.web import WebJob, job_snapshot
from test_quality_generation import prepare, revision_decision, candidates, RecordingClient
from test_quality_workflow import prepare as prepare_branch


def recorded(identity, telemetry, *, committed=True, matches=True):
    return {"node": identity, "protocol_matches": matches, "generation": telemetry.model_dump(mode="json") if identity == "test_generation" else None,
            "review": telemetry.model_dump(mode="json") if identity == "test_quality_review" else None, "committed": committed}


def generation():
    return GenerationTelemetry(client_mode="real", client_calls=1, request_attempts=2,
                               total_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))


def test_costs_include_review_retries_and_partial_token_fields():
    review = ReviewTelemetry(client_mode="real", client_calls=1, request_attempts=3, format_repairs=1,
                             total_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30))
    usage = model_usage([recorded("test_generation", generation()), recorded("test_quality_review", review)])
    assert usage["generation"]["external_requests"] == 2 and usage["review"]["external_requests"] == 3
    assert usage["total"]["external_requests"] == usage["total"]["request_attempts"] == 5
    assert usage["total"]["client_calls"] == 2 and usage["total"]["token_usage"]["total_tokens"] == 45
    partial = review.updated(total_usage=TokenUsage(prompt_tokens=20))
    usage = model_usage([recorded("test_generation", generation()), recorded("test_quality_review", partial)])
    assert usage["total"]["token_usage"]["prompt_tokens"] == 30
    assert usage["total"]["token_usage"]["total_tokens"] is None
    assert usage["total"]["known_token_usage"]["total_tokens"] == 15


@pytest.mark.parametrize("failure", ["unknown_attempts", "no_result", "foreign_result"])
def test_unknown_review_counts_do_not_become_zero_or_a_complete_total(failure):
    review = ReviewTelemetry(client_mode="real", client_calls=1, request_attempts=None, format_repairs=None, error_category="internal")
    node = recorded("test_quality_review", review, committed=False, matches=failure != "foreign_result")
    if failure == "no_result":
        node.update(review=None, protocol_matches=False)
    usage = model_usage([recorded("test_generation", generation()), node])
    assert usage["total"]["external_requests"] is None and usage["total"]["known_external_requests"] == 2
    assert usage["total"]["token_usage"]["total_tokens"] is None
    assert usage["total"]["known_token_usage"]["total_tokens"] == 15


def test_skipped_review_and_zero_call_gate_failure_have_no_external_requests():
    usage = model_usage([recorded("test_generation", generation())])
    assert usage["review"]["client_calls"] == usage["review"]["external_requests"] == 0
    zero = ReviewTelemetry(client_mode="real", client_calls=0, error_category="contract")
    assert model_usage([recorded("test_quality_review", zero)])["total"]["token_usage"]["total_tokens"] == 0


@pytest.mark.parametrize("enabled", [False, True])
def test_queued_snapshot_keeps_accepted_quality_option_before_state_exists(tmp_path, enabled):
    snapshot = job_snapshot(WebJob("queued", tmp_path, quality_review_enabled=enabled))
    assert snapshot["quality_review_enabled"] is enabled and snapshot["state"] is None


@pytest.fixture(scope="module")
def approved(tmp_path_factory):
    initial, runner, _ = prepare(tmp_path_factory.mktemp("s7-approved"), rounds=2)
    state = runner.run(initial)
    assert state.task_status == "completed", state.error_code
    return state


def test_report_answers_quality_provenance_consumption_execution_and_export(approved):
    summary = inspect_summary(approved)
    first, second = summary["round_details"]
    assert first["assessment"]["task_status"] == second["assessment"]["task_status"] == "completed"
    assert first["quality"]["status"] == "changes_requested" and second["quality"]["status"] == "passed"
    assert first["quality"]["evidence_verified"] and second["quality"]["evidence_verified"]
    feedback = first["quality"]["feedback"][0]
    assert feedback["source_round"] == 1 and feedback["consumer_round"] == 2 and feedback["consumer_node"] == "test_generation"
    assert feedback["consumed"] and feedback["findings"][0]["spec_quote"]
    assert summary["selected_round"] == 2 and summary["selected_quality_approved"] is True
    assert summary["latest_quality_approved"] is True and summary["exported_quality_approved"] is True
    assert summary["unresolved_findings"] == [] and summary["unresolved_findings_round"] is None
    assert summary["model_usage"]["review"]["client_calls"] == summary["review_calls"] == 2
    assert summary["external_requests"] == summary["total_external_requests"] == 0 and summary["generation_calls"] == 2
    assert summary["schema_version"] == "1.2"
    assert "不执行测试" in summary["agents"]["test_quality_review"]["role"]
    report = (approved.run_dir / "report.md").read_text(encoding="utf-8")
    for text in ("规格原文", "基础执行结论", "质量决定", "消费轮次 2", "实际导出已批准", "模型请求与用量", "quality_reviews/01/assessment.json"):
        assert text in report


def test_quality_inspection_does_not_call_clients_or_change_any_files(approved):
    before = {path: (file_digest(path), path.stat().st_mtime_ns) for path in approved.run_dir.rglob("*") if path.is_file()}
    with patch("test_workflow.workflow.WorkflowRunner.run", side_effect=AssertionError("inspection cannot resume")), \
         patch("test_workflow.quality_client.DeepSeekReviewClient.review", side_effect=AssertionError("inspection cannot call model")):
        inspect_summary(approved)
    after = {path: (file_digest(path), path.stat().st_mtime_ns) for path in approved.run_dir.rglob("*") if path.is_file()}
    assert before == after


def test_inspect_revalidates_even_when_model_copy_bypassed_the_state_contract(approved):
    invalid = approved.model_copy(update={"quality_status":"not_run"})
    with pytest.raises(ValueError):
        inspect_summary(invalid)


@pytest.mark.parametrize("damage", ["response_bytes", "rehashed_telemetry", "result_telemetry", "consumer_context"])
def test_inspection_detects_response_result_or_consumption_tampering(approved, damage):
    state = approved
    if damage in {"response_bytes", "rehashed_telemetry"}:
        ref = next(ref for ref in state.artifacts if ref.kind == "quality_response" and ref.round == 1)
        path = state.run_dir / ref.path
    elif damage == "result_telemetry":
        path = state.run_dir / "node_results/01/test_quality_review/result.json"
    else:
        path = state.run_dir / "node_results/02/test_generation/context.json"
    original = path.read_bytes()
    try:
        if damage == "response_bytes":
            path.write_bytes(original + b" ")
        elif damage == "rehashed_telemetry":
            response = ReviewResponse.model_validate_json(original)
            changed = response.updated(telemetry=response.telemetry.updated(total_usage=TokenUsage(prompt_tokens=7, completion_tokens=3, total_tokens=10)))
            path.write_text(changed.canonical_json(), encoding="utf-8")
            replacement = ref.updated(sha256=file_digest(path))
            state = state.updated(artifacts=tuple(replacement if item == ref else item for item in state.artifacts))
        elif damage == "result_telemetry":
            result = AgentResult.model_validate_json(original)
            path.write_text(result.updated(review=result.review.updated(total_usage=TokenUsage(total_tokens=42))).canonical_json(), encoding="utf-8")
        else:
            context = AgentContext.model_validate_json(original)
            message = context.inbox[0]
            changed = message.updated(payload=message.payload.updated(reason="unauthorized request"))
            path.write_text(context.updated(inbox=(changed,)).canonical_json(), encoding="utf-8")
        with pytest.raises((ValueError, OSError)):
            inspect_summary(state)
    finally:
        path.write_bytes(original)


def test_quality_feedback_state_cannot_claim_a_different_consumed_finding(approved):
    message = next(message for message in approved.messages if message.type == "quality_feedback")
    finding = message.payload.quality_feedback.findings[0].updated(suggested_case="different unauthorized case")
    changed = message.updated(payload=message.payload.updated(quality_feedback=message.payload.quality_feedback.updated(findings=(finding,))))
    state = approved.updated(messages=tuple(changed if item == message else item for item in approved.messages))
    with pytest.raises(ValueError):
        inspect_summary(state)


@pytest.mark.parametrize("branch,status", [("disabled", "disabled"), ("base_incomplete", "skipped"), ("budget", "changes_requested"), ("human", "needs_review")])
def test_no_approval_is_invented_for_disabled_skipped_budget_or_human(tmp_path, branch, status):
    kwargs, decisions = {}, ("approve",)
    if branch == "disabled":
        kwargs["enabled"] = False
    elif branch == "base_incomplete":
        kwargs.update(source="validation", fixture="validation_round_01.json", max_rounds=1)
    elif branch == "budget":
        kwargs["max_rounds"] = 1
        decisions = ("revision",)
    else:
        decisions = ("human",)
    initial, _, _, runner = prepare_branch(tmp_path, decisions, **kwargs)
    state = runner.run(initial)
    summary = inspect_summary(state)
    assert summary["round_details"][-1]["quality"]["status"] == status
    assert summary["exported_quality_approved"] is not True
    assert (summary["review_calls"] == 0) == (branch in {"disabled", "base_incomplete"})
    if branch in {"budget", "human"}:
        assert summary["unresolved_findings"] and summary["unresolved_findings_round"] == 1


def test_earlier_export_and_unreviewed_latest_keep_unresolved_findings(tmp_path):
    first = candidates()[0]
    initial, runner, _ = prepare(tmp_path, generated=(first, first))
    state = runner.run(initial)
    summary = inspect_summary(state)
    assert state.task_status == "partial" and summary["selected_round"] == 1
    assert summary["selected_all_passed"] and summary["latest_all_passed"] is None
    assert summary["exported_quality_approved"] is False and summary["latest_quality_approved"] is False
    assert summary["unresolved_findings_round"] == 1 and summary["unresolved_findings"]
    assert summary["round_details"][1]["quality"]["status"] == "skipped"


@pytest.mark.parametrize("result_exists", [False, True])
def test_review_error_counts_unknown_attempts_without_default_approval(tmp_path, result_exists):
    initial, runner, _ = prepare(tmp_path)
    class FailedReview:
        def run(self, context):
            if not result_exists:
                raise RuntimeError("untrusted-secret")
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                               status="error", error_code="review_network", review=ReviewTelemetry(
                                   client_mode="real", client_calls=1, request_attempts=None, format_repairs=None, error_category="transient"))
    runner.registry._agents["test_quality_review"] = FailedReview()
    state = runner.run(initial)
    summary = inspect_summary(state)
    assert state.workflow_status == "error" and summary["quality_status"] == "error"
    assert summary["round_details"][0]["quality"]["status"] == "error"
    assert summary["external_requests"] is None and summary["known_external_requests"] == 0
    assert summary["review_calls"] == (1 if result_exists else None)
    assert not summary["quality_assessments"] and not summary["exported_quality_approved"]
    assert "untrusted-secret" not in (state.run_dir / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("outcome", ["approve", "known_failure", "unknown_failure"])
def test_export_totals_use_both_production_node_telemetries(tmp_path, outcome):
    initial, runner, _ = prepare(tmp_path)
    initial = initial.updated(config=initial.config.updated(mode="real"))
    class CostGeneration(RecordingClient):
        safe_config = {"max_http_attempts": 2}
        def generate_tests(self, context):
            return super().generate_tests(context).updated(attempts=2,
                total_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    class CostReview:
        safe_config = {"client_mode":"real", "max_http_attempts":3}
        def review(self, context):
            if outcome == "unknown_failure":
                raise RuntimeError("untrusted-secret")
            telemetry = ReviewTelemetry(client_mode="real", client_calls=1, request_attempts=3,
                total_usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
                error_category="transient" if outcome == "known_failure" else None)
            if outcome == "known_failure":
                raise ReviewClientError(ReviewErrorCode.TIMEOUT, telemetry)
            decision = revision_decision().updated(verdict="approve", findings=(), summary="通过。")
            return ReviewResponse(decision=decision, raw_response=decision.canonical_json(), telemetry=telemetry)
    runner.registry._agents["test_generation"] = TestGenerationAgent(CostGeneration(candidates()), client_mode="real")
    runner.registry._agents["test_quality_review"] = TestQualityReviewAgent(CostReview())
    state = runner.run(initial)
    summary = inspect_summary(state)
    assert state.agent_state.api_calls == 2  # Vendor counter continues to describe generation only.
    assert summary["generation_external_requests"] == 2
    assert summary["external_requests"] == (None if outcome == "unknown_failure" else 5)
    assert summary["known_external_requests"] == (2 if outcome == "unknown_failure" else 5)
    assert summary["token_usage"]["total_tokens"] == (None if outcome == "unknown_failure" else 45)
    assert summary["review_calls"] == 1
    assert (state.task_status == "completed") == (outcome == "approve")
