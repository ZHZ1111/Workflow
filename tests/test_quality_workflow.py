"""S4 routing with controlled generation and S5 production-node acceptance."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from testgen_agent.agent import select_best_candidate
from testgen_agent.generation import stage_revision_candidate
from testgen_agent.llm_client import LLMCallRecord, LLMGenerationResult
from testgen_agent.models import AgentAction, GeneratedTests

from test_workflow.adapters.agentdev import adapt_candidate
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_quality_review import TestQualityReviewAgent
from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext, AgentResult, GenerationTelemetry, Message, MessagePayload, StatePatch
from test_workflow.definitions import QUALITY_WORKFLOW, QUALITY_SUMMARY_WORKFLOW, TESTING_WORKFLOW
from test_workflow.demo.client import FixtureClient
from test_workflow.events import Event
from test_workflow.nodes.result_summary import ResultSummaryNode
from test_workflow.quality_client import ReviewResponse
from test_workflow.quality_contracts import Finding, ReviewDecision, ReviewTelemetry
from test_workflow.quality_routing import finding_fingerprint
from test_workflow.registry import AgentRegistry
from test_workflow.reporting import inspect_summary
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/test_workflow/demo/data"


def calculation_candidates(variant="tests"):
    first = GeneratedTests.model_validate_json((DATA / "calculation_complete.json").read_text(encoding="utf-8"))
    if variant == "duplicate":
        return (first, first)
    if variant == "tie":
        return (first, first.updated(code=first.code + "\nQUALITY_NOTE = 1\n"))
    values = [first]
    for number, (name, args, expected) in enumerate((("both_zero", (0, 0), 0), ("one_one", (1, 1), 1)), 2):
        old = values[-1]
        scenario = old.plan.scenarios[0].updated(scenario_id=name, category="boundary", inputs={"args": args, "kwargs": {}},
                    expected_behavior=f"returns {expected}")
        mapping = old.mappings[0].updated(test_function=f"test_{name}", scenario_ids=(name,))
        code = old.code + f"\ndef test_{name}():\n    assert calculate_total{args} == {expected}\n"
        values.append(old.updated(code=code, plan=old.plan.updated(scenarios=(*old.plan.scenarios, scenario)), mappings=(*old.mappings, mapping)))
    return tuple(values)


class ControlledGeneration:
    """First round uses production generation; later rounds stage fixed content.

    This verifies the S4 handoff and candidate protocol without implementing S5.
    Vendor preservation/duplicate checks are still applied on every revision.
    """
    def __init__(self, candidates):
        self.candidates = candidates
        self.contexts = []
        self.first = TestGenerationAgent(FixtureClient((DATA / "calculation_complete.json",)), client_mode="offline")

    def run(self, context):
        self.contexts.append(context)
        if context.round == 1:
            return self.first.run(context)
        business = context.state.agent_state
        llm = LLMGenerationResult(generated=self.candidates[context.round - 1], provider="deepseek", requested_model="fixed",
                                   response_model="fixed", attempts=1, format_repairs=0, latency_seconds=0,
                                   calls=(LLMCallRecord(purpose="generation"),))
        staged = stage_revision_candidate(source=business.source_info, llm_result=llm, workspace=business.request.output_dir,
                                           history=business.history, expected_oracle_mode=business.oracle_mode)
        wrapped = adapt_candidate(context, staged)
        action = AgentAction.RUN_TESTS if wrapped.candidate.test_path else AgentAction.REPAIR_GENERATED_TEST
        messages = ()
        if action == AgentAction.RUN_TESTS:
            messages = (Message(message_id=f"msg-candidate-{context.round:02d}", run_id=context.run_id, round=context.round,
                                sender="test_generation", receiver="test_execution", type="candidate_ready", payload=MessagePayload(
                                    candidate_id=wrapped.handoff.candidate_id, artifact=wrapped.handoff, reason="S4 fixed revision", recommended_action=action)),)
        return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                           patch=StatePatch(candidate=wrapped.candidate, next_action=action), messages=messages,
                           artifacts=wrapped.artifacts, generation=GenerationTelemetry(client_mode="offline", client_calls=1))


class ControlledReviewClient:
    safe_config = {"client_mode": "offline", "max_http_attempts": 1}

    def __init__(self, decisions):
        self.decisions = decisions
        self.contexts = []

    def review(self, context):
        self.contexts.append(context)
        choice = self.decisions[len(self.contexts) - 1]
        if callable(choice):
            choice = choice(context)
        if choice == "approve":
            decision = ReviewDecision(verdict="approve", summary="没有未解决的规格场景遗漏。")
        elif choice == "human":
            quote = context.specification.splitlines()[0]
            decision = ReviewDecision(verdict="request_review", summary="固定复核分支。", questions=("请人工确认该条规格的解释。",), findings=(
                Finding(finding_id="question", category="ambiguous_specification", blocking=True,
                        target_functions=(context.source_info.functions[0].name,), spec_quote=quote, description="需要确认预期解释。"),))
        elif choice in {"revision", "next_revision"}:
            next_case = choice == "next_revision"
            decision = ReviewDecision(verdict="request_revision", summary="保留通过测试并补充明确规格场景。", findings=(Finding(
                finding_id=f"model-name-{len(self.contexts)}", category="missing_boundary", blocking=True,
                target_functions=("calculate_total",), related_tests=(),
                spec_quote="返回整数 `unit_price * quantity`" if next_case else "单价或数量为 0 时返回 0。",
                description="固定审查响应，验证路由。", suggested_case="新增 calculate_total(1, 1) == 1。" if next_case else "新增 calculate_total(0, 0) == 0。"),))
        else:
            decision = choice
        return ReviewResponse(decision=decision, raw_response=decision.canonical_json(),
                              telemetry=ReviewTelemetry(client_mode="offline", client_calls=1, max_request_attempts=1, latency_seconds=0))


def prepare(tmp_path, decisions=("approve",), *, variant="tests", max_rounds=3, summary=False, review_transform=None,
            enabled=True, source="calculation", fixture="calculation_complete.json"):
    state = create_run(source=ROOT / f"examples/{source}.py", specification=ROOT / f"examples/{source}.md", output=tmp_path,
                       config=WorkflowConfig(mode="offline", max_rounds=max_rounds, quality_review_enabled=enabled))
    generator = ControlledGeneration(calculation_candidates(variant)) if source == "calculation" else TestGenerationAgent(
        FixtureClient((DATA / fixture,)), client_mode="offline")
    client = ControlledReviewClient(decisions)
    reviewer = TestQualityReviewAgent(client)
    registry = AgentRegistry()
    registry.register("test_generation", generator)
    registry.register("test_execution", TestExecutionAgent())
    if enabled:
        if review_transform:
            original = reviewer

            class Transform:
                def run(self, context):
                    return review_transform(original.run(context), context)
            reviewer = Transform()
        registry.register("test_quality_review", reviewer)
    if summary:
        registry.register("result_summary", ResultSummaryNode())
    definition = QUALITY_SUMMARY_WORKFLOW if summary else QUALITY_WORKFLOW
    return state, generator, client, WorkflowRunner(registry, definition if enabled else TESTING_WORKFLOW)


def events(state):
    return [Event.model_validate_json(line) for line in (state.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_first_approval_runs_three_nodes_and_consumes_both_handoffs(tmp_path):
    initial, generation, client, runner = prepare(tmp_path)
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "completed", final.error_code
    assert final.quality_status == "passed" and final.quality_stop_reason == "approved" and final.selected_round == 1
    assert len(client.contexts) == len(generation.contexts) == 1
    assert [message.type for message in final.messages] == ["candidate_ready", "review_ready"]
    assert set(final.consumed_message_ids) == {message.message_id for message in final.messages}
    assert [event.node for event in events(final) if event.type == "node_finished"] == ["test_generation", "test_execution", "test_quality_review"]
    assert StateStore(final.run_dir).load() == final
    assert inspect_summary(final)["pytest_processes"] == 1


@pytest.mark.parametrize("variant", ["tests", "tie"])
def test_quality_feedback_reaches_second_generation_and_approved_candidate_is_exported(tmp_path, variant):
    initial, generator, client, runner = prepare(tmp_path, ("revision", "approve"), variant=variant, summary=True, max_rounds=2)
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "completed", final.error_code
    assert final.selected_round == final.agent_state.best_available_round == 2
    assert len(generator.contexts) == len(client.contexts) == 2
    second = generator.contexts[1]
    assert second.inbox[0].type == "quality_feedback" and second.inbox[0].round == 1
    assert second.state.quality_status == "not_run" and second.state.stop_reason is None
    assert second.state.agent_state.next_action == AgentAction.ADD_MISSING_CASES
    assert client.contexts[1].history_reviews[0] == final.quality_assessments[0]
    assert client.contexts[0].execution_assessment.action == AgentAction.FINISH
    assert (final.run_dir / "final/test_subject.py").read_bytes() == final.agent_state.history[1].test_path.read_bytes()
    log = events(final)
    assert sum(event.node == "result_summary" and event.type == "node_finished" for event in log) == 1
    assert sum(event.node == "test_quality_review" and event.type == "node_finished" for event in log) == 2
    assert inspect_summary(final)["pytest_processes"] == 2
    if variant == "tie":
        assert select_best_candidate(final.agent_state.history) == 1
    assert "当前已通过质量审查" in inspect_summary(final)["selection_reason"]


@pytest.mark.parametrize("decisions,status,reason,rounds", [
    (("revision",), "partial", "quality_budget_reached", 1),
    (("human",), "needs_review", "ambiguous_specification", 1),
    (("revision", "revision"), "partial", "quality_no_progress", 2),
])
def test_review_stop_routes_preserve_findings_without_extra_feedback_or_candidates(tmp_path, decisions, status, reason, rounds):
    limit = 1 if len(decisions) == 1 else 3
    initial, generator, client, runner = prepare(tmp_path, decisions, max_rounds=limit, summary=True)
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == status, final.error_code
    assert final.quality_stop_reason == reason and len(final.agent_state.history) == rounds
    assert len(generator.contexts) == len(client.contexts) == rounds
    feedback = [message for message in final.messages if message.type == "quality_feedback"]
    assert len(feedback) == rounds - 1
    if rounds == 2:
        assert finding_fingerprint(final.quality_assessments[0].decision.findings[0]) == finding_fingerprint(final.quality_assessments[1].decision.findings[0])
        assert final.quality_assessments[0].decision.findings[0].finding_id != final.quality_assessments[1].decision.findings[0].finding_id
        proposal = AgentResult.model_validate_json((final.run_dir / "node_results/02/test_quality_review/result.json").read_text(encoding="utf-8"))
        assert len(proposal.messages) == 1  # Proposal retained for audit, never journaled.
    assert sum(event.type == "node_finished" and event.node == "result_summary" for event in events(final)) == 1


@pytest.mark.parametrize("last", ["approve", "revision"])
def test_third_and_last_candidate_can_approve_or_stop_without_round_four(tmp_path, last):
    initial, generator, client, runner = prepare(tmp_path, ("revision", "next_revision", last))
    final = runner.run(initial)
    assert final.workflow_status == "finished", final.error_code
    assert final.task_status == ("completed" if last == "approve" else "partial")
    assert final.quality_stop_reason == ("approved" if last == "approve" else "quality_budget_reached")
    assert len(generator.contexts) == len(client.contexts) == len(final.agent_state.history) == 3
    assert not (final.run_dir / "node_results/04").exists()


def test_duplicate_candidate_after_quality_feedback_stops_before_another_review(tmp_path):
    initial, generator, client, runner = prepare(tmp_path, ("revision",), variant="duplicate")
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "partial", final.error_code
    assert final.stop_reason == "no_progress" and final.quality_stop_reason == "quality_no_progress"
    assert final.quality_status == "not_run" and len(client.contexts) == 1 and len(generator.contexts) == 2
    assert len(final.quality_assessments) == 1 and inspect_summary(final)["pytest_processes"] == 1


def test_quality_disabled_preserves_two_node_workflow(tmp_path):
    initial, generation, client, runner = prepare(tmp_path, enabled=False)
    final = runner.run(initial)
    assert final.task_status == "completed" and final.quality_status == "disabled"
    assert not client.contexts and not final.quality_assessments and len(generation.contexts) == 1
    assert not any(message.type in {"quality_feedback", "review_ready"} for message in final.messages)


@pytest.mark.parametrize("source,fixture,expected", [("validation", "validation_round_01.json", "partial"), ("discount", "discount_defect.json", "needs_review")])
def test_base_noncompletion_never_invokes_review(tmp_path, source, fixture, expected):
    initial, _, client, runner = prepare(tmp_path, source=source, fixture=fixture, max_rounds=1)
    final = runner.run(initial)
    assert final.task_status == expected and final.quality_status == "not_run" and not client.contexts
    assert not any(event.node == "test_quality_review" for event in events(final))


@pytest.mark.parametrize("mutation", ["wrong_run", "missing_response", "tampered_response", "rehashed_wrong_spec", "wrong_feedback_reason"])
def test_invalid_quality_proposals_are_rejected_without_consuming_ready(tmp_path, mutation):
    def transform(result, context):
        if mutation == "wrong_run":
            return result.updated(run_id="foreign-run") if result.quality_assessment is None else result.model_copy(update={"run_id": "foreign-run"})
        if mutation == "missing_response":
            return result.updated(artifacts=tuple(ref for ref in result.artifacts if ref.kind != "quality_response"))
        if mutation == "tampered_response":
            ref = next(ref for ref in result.artifacts if ref.kind == "quality_response")
            atomic_write(context.run_dir / ref.path, "{}")
            return result
        if mutation == "wrong_feedback_reason":
            message = result.messages[0]
            return result.updated(messages=(message.updated(payload=message.payload.updated(reason="mismatched reason")),))
        quality = result.quality_assessment.updated(spec_sha256="a" * 64)
        ref = next(ref for ref in result.artifacts if ref.kind == "quality_assessment")
        atomic_write(context.run_dir / ref.path, quality.canonical_json())
        changed = ref.updated(sha256=file_digest(context.run_dir / ref.path))
        return result.updated(quality_assessment=quality, artifacts=tuple(changed if item == ref else item for item in result.artifacts))

    decisions = ("revision",) if mutation == "wrong_feedback_reason" else ("approve",)
    initial, generator, _, runner = prepare(tmp_path, decisions, review_transform=transform)
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.task_status == "partial"
    assert final.quality_status == "error" and final.quality_stop_reason == "review_error"
    assert len(generator.contexts) == 1 and not final.quality_assessments
    ready = next(message for message in final.messages if message.type == "review_ready")
    assert ready.message_id not in final.consumed_message_ids
    assert not any(ref.kind == "quality_assessment" for ref in final.artifacts)


def test_review_exception_never_falls_back_to_completed(tmp_path):
    def transform(result, context):
        raise RuntimeError("secret-value")
    initial, _, _, runner = prepare(tmp_path, review_transform=transform)
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.task_status == "partial" and final.quality_status == "error"
    assert "secret-value" not in (final.run_dir / "state.json").read_text(encoding="utf-8")


def test_duplicate_consumption_and_conflicting_feedback_cannot_commit_another_candidate(tmp_path):
    initial, _, _, runner = prepare(tmp_path, ("revision", "approve"))
    final = runner.run(initial)
    assert final.task_status == "completed", final.error_code
    ctx = AgentContext.model_validate_json((final.run_dir / "node_results/02/test_generation/context.json").read_text(encoding="utf-8"))
    result = AgentResult.model_validate_json((final.run_dir / "node_results/02/test_generation/result.json").read_text(encoding="utf-8"))
    feedback = ctx.inbox[0]
    consumed = (feedback.message_id,)
    once = runner.apply_result(ctx.state, result, consumed)
    with pytest.raises(ValueError):
        runner.apply_result(once, result, consumed)
    execution_ref = next(ref for ref in ctx.state.artifacts if ref.kind == "assessment" and ref.round == 1)
    conflict = Message(message_id="conflict", run_id=ctx.run_id, round=1, type="execution_feedback", sender="test_execution", receiver="test_generation",
                       payload=MessagePayload(candidate_id=execution_ref.candidate_id, artifact=execution_ref, reason="unexpected feedback"))
    state = ctx.state.updated(messages=(*ctx.state.messages, conflict))
    with pytest.raises(ValueError):
        runner.apply_result(state, result, consumed)
    assert len(state.agent_state.history) == 1


def test_quality_definition_and_flag_must_agree_before_any_node_runs(tmp_path):
    initial, generator, _, runner = prepare(tmp_path)
    with pytest.raises(ValueError, match="enabled quality"):
        WorkflowRunner(runner.registry, TESTING_WORKFLOW).run(initial)
    assert not generator.contexts


@pytest.mark.parametrize("mutation", ["stale_round", "consumed", "changed_findings", "changed_reason"])
def test_generation_commit_rejects_stale_or_modified_quality_feedback(tmp_path, mutation):
    initial, _, _, runner = prepare(tmp_path, ("revision", "approve"))
    final = runner.run(initial)
    assert final.task_status == "completed", final.error_code
    ctx = AgentContext.model_validate_json((final.run_dir / "node_results/02/test_generation/context.json").read_text(encoding="utf-8"))
    result = AgentResult.model_validate_json((final.run_dir / "node_results/02/test_generation/result.json").read_text(encoding="utf-8"))
    message = ctx.inbox[0]
    state = ctx.state
    if mutation == "consumed":
        state = state.updated(consumed_message_ids=(*state.consumed_message_ids, message.message_id))
    else:
        payload = message.payload
        if mutation == "stale_round":
            ref = payload.artifact.updated(round=2, candidate_id=f"{ctx.run_id}:2")
            feedback = payload.quality_feedback.updated(candidate_id=ref.candidate_id, assessment=ref)
            changed = message.updated(round=2, payload=payload.updated(candidate_id=ref.candidate_id, artifact=ref, quality_feedback=feedback))
        elif mutation == "changed_findings":
            findings = (payload.quality_feedback.findings[0].updated(suggested_case="Different unauthorized request"),)
            changed = message.updated(payload=payload.updated(quality_feedback=payload.quality_feedback.updated(findings=findings)))
        else:
            changed = message.updated(payload=payload.updated(reason="Different unauthorized reason"))
        state = state.updated(messages=tuple(changed if item == message else item for item in state.messages))
    before = state.canonical_json()
    with pytest.raises(ValueError):
        runner.apply_result(state, result, (message.message_id,))
    assert state.canonical_json() == before and len(state.agent_state.history) == 1


def test_quality_node_failure_keeps_safe_result_and_does_not_consume_ready(tmp_path):
    initial, _, _, runner = prepare(tmp_path)
    class Failed:
        def run(self, context):
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                               status="error", error_code="review_timeout", review=ReviewTelemetry(
                                   client_mode="mock", client_calls=1, request_attempts=3, error_category="transient"))
    runner.registry._agents["test_quality_review"] = Failed()
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.quality_status == "error" and final.task_status == "partial"
    result = AgentResult.model_validate_json((final.run_dir / "node_results/01/test_quality_review/result.json").read_text(encoding="utf-8"))
    assert result.review.request_attempts == 3 and final.agent_state.api_calls == 0
    ready = next(item for item in final.messages if item.type == "review_ready")
    assert ready.message_id not in final.consumed_message_ids


def test_execution_completion_cannot_skip_review_ready_handoff(tmp_path):
    initial, _, client, runner = prepare(tmp_path)
    original = runner.registry.get("test_execution")
    class SilentExecution:
        def run(self, context):
            return original.run(context).updated(messages=())
    runner.registry._agents["test_execution"] = SilentExecution()
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.task_status != "completed" and not client.contexts
    candidate_message = next(item for item in final.messages if item.type == "candidate_ready")
    assert candidate_message.message_id not in final.consumed_message_ids


def test_production_generation_consumes_quality_feedback_and_reaches_approval(tmp_path):
    initial, _, client, runner = prepare(tmp_path, ("revision", "approve"))
    second_path = tmp_path / "quality_extension.json"
    atomic_write(second_path, calculation_candidates()[1].canonical_json())
    generation_client = FixtureClient((DATA / "calculation_complete.json", second_path))
    generator = TestGenerationAgent(generation_client, client_mode="offline")
    runner.registry._agents["test_generation"] = generator
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "completed", final.error_code
    assert final.selected_round == 2 and len(client.contexts) == len(generation_client.contexts) == 2
    assert "quality_extension" in generation_client.contexts[1].user_prompt
    feedback = next(item for item in final.messages if item.type == "quality_feedback")
    assert feedback.message_id in final.consumed_message_ids
