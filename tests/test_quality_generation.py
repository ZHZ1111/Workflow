"""S5 production generation gates, prompt data and protected-test preservation."""

import ast
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from test_workflow.agents.test_generation.core.agent import choose_action
from test_workflow.agents.test_generation.core.llm_client import LLMCallRecord, LLMGenerationResult, LLMError, LLMErrorCode
from test_workflow.agents.test_generation.core.models import AgentAction, GeneratedTests, StopReason
from test_workflow.agents.test_generation.core.prompts import TASK_DATA_START, TASK_DATA_END

from test_workflow.adapters.quality_revision import build_quality_revision_context
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.agents.test_quality_review import TestQualityReviewAgent
from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext
from test_workflow.definitions import QUALITY_SUMMARY_WORKFLOW
from test_workflow.demo.quality_client import FixtureReviewClient
from test_workflow.nodes.result_summary import ResultSummaryNode
from test_workflow.quality_contracts import Finding, ReviewDecision
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/test_workflow/demo/data"


def candidates():
    first = GeneratedTests.model_validate_json((DATA / "calculation_complete.json").read_text(encoding="utf-8"))
    # Extra support code verifies that protection also covers indirect assertion dependencies.
    first = first.updated(code=first.code.replace("from subject import calculate_total", "from subject import calculate_total\nZERO = 0")
                          .replace("== 0", "== ZERO"))
    items = [first]
    for name, args, expected, quote in (("both_zero", (0, 0), 0, "单价或数量为 0 时返回 0。"),
                                         ("one_one", (1, 1), 1, "返回整数 `unit_price * quantity`")):
        old = items[-1]
        scenario = old.plan.scenarios[0].updated(scenario_id=name, inputs={"args": args, "kwargs": {}},
                    expected_behavior=f"returns {expected}", expected_basis=old.plan.scenarios[0].expected_basis.updated(detail=quote))
        mapping = old.mappings[0].updated(test_function=f"test_{name}", scenario_ids=(name,))
        items.append(old.updated(code=old.code + f"\ndef test_{name}():\n    assert calculate_total{args} == {expected}\n",
                                 plan=old.plan.updated(scenarios=(*old.plan.scenarios, scenario)), mappings=(*old.mappings, mapping)))
    return tuple(items)


class RecordingClient:
    safe_config = {"max_http_attempts": 1}

    def __init__(self, generated, error=None):
        self.generated = iter(generated)
        self.error = error
        self.contexts = []

    def generate_tests(self, context):
        self.contexts.append(context)
        if self.error:
            raise self.error
        return LLMGenerationResult(generated=next(self.generated), provider="deepseek", requested_model="fixed",
                                   response_model="fixed", attempts=1, format_repairs=0, latency_seconds=0,
                                   calls=(LLMCallRecord(purpose="generation"),))


def revision_decision(next_case=False):
    return ReviewDecision(verdict="request_revision", summary="补充明确规格支持的场景。", findings=(Finding(
        finding_id="one-one" if next_case else "both-zero", category="missing_boundary", blocking=True,
        target_functions=("calculate_total",), spec_quote="返回整数 `unit_price * quantity`" if next_case else "单价或数量为 0 时返回 0。",
        description="已有执行通过，仍需验证此输入组合。", suggested_case="calculate_total(1, 1) == 1" if next_case else "calculate_total(0, 0) == 0"),))


def prepare(root, *, rounds=3, generated=None, decisions=None):
    root.mkdir(parents=True, exist_ok=True)
    for name in ("calculation.py", "calculation.md"):
        (root / name).write_bytes((ROOT / "examples" / name).read_bytes())
    initial = create_run(source=root / "calculation.py", specification=root / "calculation.md", output=root / "runs",
                         config=WorkflowConfig(mode="offline", quality_review_enabled=True, max_rounds=rounds))
    generation_client = RecordingClient(generated or candidates())
    generation = TestGenerationAgent(generation_client, client_mode="offline")
    decisions = decisions or (revision_decision(), ReviewDecision(verdict="approve", summary="新增场景已覆盖。"))
    paths = []
    for number, decision in enumerate(decisions):
        path = root / f"review-{number}.json"
        atomic_write(path, decision.canonical_json())
        paths.append(path)
    review_client = FixtureReviewClient(tuple(paths))
    registry = AgentRegistry()
    registry.register("test_generation", generation)
    registry.register("test_execution", TestExecutionAgent())
    registry.register("test_quality_review", TestQualityReviewAgent(review_client))
    registry.register("result_summary", ResultSummaryNode())
    return initial, WorkflowRunner(registry, QUALITY_SUMMARY_WORKFLOW), generation_client


def context_for(state, node, number, inbox=()):
    state = state.updated(workflow_status="running", current_node=node, current_round=number)
    return AgentContext(run_id=state.run_id, round=number, agent_id=node, run_dir=state.run_dir,
                        snapshot_json=state.canonical_json(), inbox=inbox)


def prepared_revision(root):
    state, runner, _ = prepare(root)
    for node in runner.definition.nodes[:3]:
        inbox = tuple(message for message in state.messages if message.receiver == node.agent_id
                      and message.message_id not in state.consumed_message_ids)
        context = context_for(state, node.agent_id, 1, inbox)
        result = runner.registry.get(node.agent_id).run(context)
        assert result.status == "ok", result.error_code
        state = runner.apply_result(context.state, result, tuple(item.message_id for item in inbox))
    state = state.updated(current_round=2, quality_status="not_run", quality_stop_reason=None)
    return context_for(state, "test_generation", 2, tuple(message for message in state.messages if message.type == "quality_feedback"))


@pytest.fixture(scope="module")
def revision(tmp_path_factory):
    # Rejected-context cases only read these shared files; file mutations get their own run.
    return prepared_revision(tmp_path_factory.mktemp("s5-revision"))


def generate_without_execution(agent, context):
    with patch("subprocess.Popen", side_effect=AssertionError("generation must not execute")), \
         patch("test_workflow.adapters.agentdev.build_action_context", side_effect=AssertionError("ordinary FINISH context must not be used")):
        return agent.run(context)


def parse_prompt(prompt):
    return json.loads(prompt.user_prompt.removeprefix(TASK_DATA_START).removesuffix(TASK_DATA_END).strip())


@pytest.mark.parametrize("rounds", [2, 3])
def test_production_nodes_close_quality_loop_and_deliver_full_prompt(tmp_path, rounds):
    decisions = (revision_decision(),) + ((revision_decision(True),) if rounds == 3 else ()) + (ReviewDecision(verdict="approve", summary="通过。"),)
    initial, runner, client = prepare(tmp_path, rounds=rounds, decisions=decisions)
    with patch("test_workflow.adapters.agentdev.build_action_context", side_effect=AssertionError("quality cannot use old action context")):
        final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "completed", final.error_code
    assert final.selected_round == rounds and final.quality_status == "passed"
    assert len(client.contexts) == rounds
    for number, prompt in enumerate(client.contexts[1:], 2):
        payload = parse_prompt(prompt)
        assert payload["task_kind"] == "quality_extension" and payload["base_execution_decision"]["action"] == "finish"
        assert payload["observed_execution"]["all_passed"] and payload["observed_coverage"]["meets_target"]
        assert payload["quality_feedback"]["round"] == number - 1
        assert payload["quality_assessment"] == final.quality_assessments[number - 2].model_dump(mode="json")
        assert payload["unresolved_findings"] == [item.model_dump(mode="json") for item in final.quality_assessments[number - 2].decision.findings if item.blocking]
        assert len(payload["task_memory"]["history"]) == len(payload["quality_history"]) == number - 1
        assert payload["round_budget"]["remaining_rounds_including_requested"] == rounds - number + 1
        assert payload["task_memory"]["protected_test_functions"]
        assert payload["behavior_specification"] == final.agent_state.behavior_specification
        assert payload["previous_generated_tests"]["code"] == final.agent_state.history[number - 2].generated.code
        assert "authoritative oracle" in prompt.system_instruction and "untrusted task data" in prompt.system_instruction
    assert parse_prompt(client.contexts[0])["task_kind"] == "initial_generation"
    assert (final.run_dir / "final/test_subject.py").read_bytes() == final.agent_state.history[-1].test_path.read_bytes()
    assert StateStore(final.run_dir).load() == final
    assert len([item for item in final.messages if item.type == "quality_feedback"]) == rounds - 1
    assert set(final.consumed_message_ids) == {item.message_id for item in final.messages}


def test_three_production_candidates_stop_at_quality_budget_without_round_four(tmp_path):
    last = revision_decision(True)
    last = last.updated(findings=(last.findings[0].updated(
        finding_id="large-product", suggested_case="calculate_total(1000000, 1000000) == 1000000000000"),))
    initial, runner, client = prepare(tmp_path, decisions=(revision_decision(), revision_decision(True), last))
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "partial", final.error_code
    assert final.stop_reason == "round_limit_reached" and final.quality_stop_reason == "quality_budget_reached"
    assert len(client.contexts) == len(final.quality_assessments) == len(final.agent_state.history) == 3
    assert all(candidate.execution.all_passed and candidate.coverage.meets_target for candidate in final.agent_state.history)
    feedback = [message for message in final.messages if message.type == "quality_feedback"]
    assert [message.round for message in feedback] == [1, 2]
    assert all(message.message_id in final.consumed_message_ids for message in feedback)
    assert final.quality_assessments[-1].decision == last
    assert not (final.run_dir / "node_results/04").exists()
    events = [json.loads(line) for line in (final.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(event["type"] == "node_finished" and event["node"] == "result_summary" for event in events) == 1
    summary = json.loads((final.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["pytest_processes"] == summary["review_calls"] == 3
    assert summary["unresolved_findings_round"] == 3 and not summary["exported_quality_approved"]


def test_quality_generation_is_single_action_without_execution_or_state_mutation(tmp_path):
    context = prepared_revision(tmp_path)
    client = RecordingClient((candidates()[1],))
    before = context.canonical_json()
    result = generate_without_execution(TestGenerationAgent(client, client_mode="offline"), context)
    assert result.status == "ok" and result.patch.next_action == AgentAction.RUN_TESTS, result.error_code
    assert result.patch.candidate.execution is None and result.patch.candidate.coverage is None
    assert len(client.contexts) == result.generation.client_calls == 1
    assert result.generation.external_requests == 0 and context.canonical_json() == before


@pytest.mark.parametrize("mutation", ["delete", "weaken", "decorator", "mapping", "scenario", "support"])
def test_quality_revision_keeps_original_passing_tests_and_support(tmp_path, mutation):
    context = prepared_revision(tmp_path)
    generated = candidates()[1]
    tree = ast.parse(generated.code)
    protected = context.state.agent_state.history[0].generated.mappings[0].test_function
    target = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == protected)
    if mutation == "delete":
        tree.body.remove(target)
        mappings = tuple(item for item in generated.mappings if item.test_function != protected)
        used = {sid for mapping in mappings for sid in mapping.scenario_ids}
        generated = generated.updated(mappings=mappings, plan=generated.plan.updated(scenarios=tuple(item for item in generated.plan.scenarios if item.scenario_id in used)))
    elif mutation == "weaken":
        assertion = next(node for node in target.body if isinstance(node, ast.Assert))
        assertion.test = ast.Compare(left=assertion.test.left, ops=[ast.GtE()], comparators=[ast.Constant(0)])
    elif mutation == "decorator":
        target.decorator_list.append(ast.Call(func=ast.Attribute(value=ast.Attribute(value=ast.Name(id="pytest", ctx=ast.Load()), attr="mark", ctx=ast.Load()), attr="parametrize", ctx=ast.Load()), args=[ast.Constant("unused"), ast.List(elts=[ast.Constant(0)], ctx=ast.Load())], keywords=[]))
    elif mutation == "mapping":
        mappings = tuple(item.updated(scenario_ids=(*item.scenario_ids, "both_zero")) if item.test_function == protected else item for item in generated.mappings)
        generated = generated.updated(mappings=mappings)
    elif mutation == "scenario":
        old = generated.plan.scenarios[0]
        generated = generated.updated(plan=generated.plan.updated(scenarios=(old.updated(expected_behavior="weakened expectation"), *generated.plan.scenarios[1:])))
    else:
        next(node for node in tree.body if isinstance(node, ast.Assign)).value = ast.Constant(99)
    generated = generated.updated(code=ast.unparse(ast.fix_missing_locations(tree)))
    client = RecordingClient((generated,))
    result = generate_without_execution(TestGenerationAgent(client, client_mode="offline"), context)
    assert result.status == "ok" and result.patch.candidate.validation_error.startswith("preserve_tests"), result.error_code or result.patch.candidate.validation_error
    assert result.patch.candidate.test_path is None and result.messages == () and len(client.contexts) == 1
    assert context.state.agent_state.history[0].generated == candidates()[0]


@pytest.mark.parametrize("mutation", ["consumed", "not_registered", "mixed", "wrong_source_round", "reason", "findings", "no_quality", "wrong_action", "base_incomplete", "changed_spec", "approved", "review_error", "stopped"])
def test_invalid_quality_context_fails_before_client_call(revision, mutation):
    state = revision.state
    message = revision.inbox[0]
    if mutation == "consumed":
        state = state.updated(consumed_message_ids=(*state.consumed_message_ids, message.message_id))
    elif mutation == "not_registered":
        state = state.updated(artifacts=tuple(ref for ref in state.artifacts if ref != message.payload.artifact))
    elif mutation == "mixed":
        extra = message.updated(message_id="unexpected-second-feedback")
        state = state.updated(messages=(*state.messages, extra))
    elif mutation == "wrong_source_round":
        ref = message.payload.artifact.updated(round=2, candidate_id=f"{state.run_id}:2")
        feedback = message.payload.quality_feedback.updated(assessment=ref, candidate_id=ref.candidate_id)
        message = message.updated(round=2, payload=message.payload.updated(artifact=ref, candidate_id=ref.candidate_id, quality_feedback=feedback))
    elif mutation == "reason":
        message = message.updated(payload=message.payload.updated(reason="unauthorized summary"))
    elif mutation == "findings":
        finding = message.payload.quality_feedback.findings[0].updated(suggested_case="unauthorized case")
        message = message.updated(payload=message.payload.updated(quality_feedback=message.payload.quality_feedback.updated(findings=(finding,))))
    elif mutation == "no_quality":
        state = state.updated(config=state.config.updated(quality_review_enabled=False), quality_status="disabled", quality_assessments=())
    elif mutation == "wrong_action":
        state = state.updated(agent_state=state.agent_state.updated(next_action=AgentAction.REPAIR_GENERATED_TEST))
    elif mutation == "base_incomplete":
        old = state.agent_state.history[-1]
        coverage = old.coverage.updated(status="unavailable", covered_lines=None, total_lines=None,
                                        covered_branches=None, total_branches=None, missing_lines=(), missing_branches=(), error="coverage unavailable")
        state = state.updated(agent_state=state.agent_state.updated(history=(old.updated(coverage=coverage),)))
    elif mutation == "changed_spec":
        state = state.updated(agent_state=state.agent_state.updated(behavior_specification="different spec snapshot"))
    elif mutation == "approved":
        old = state.quality_assessments[-1]
        state = state.updated(quality_assessments=(old.updated(decision=ReviewDecision(verdict="approve", summary="done")),))
    elif mutation == "review_error":
        # A stop decision must not become a fresh active quality extension.
        state = state.updated(quality_status="error", quality_stop_reason="review_error")
    else:
        state = state.updated(stop_reason=StopReason.NO_PROGRESS,
                              agent_state=state.agent_state.updated(stop_reason=StopReason.NO_PROGRESS, next_action=AgentAction.FINISH))
    if message != revision.inbox[0]:
        state = state.updated(messages=tuple(message if item.message_id == message.message_id else item for item in state.messages))
    context = revision.updated(snapshot_json=state.canonical_json(), inbox=(message,))
    client = RecordingClient((candidates()[1],))
    result = generate_without_execution(TestGenerationAgent(client, client_mode="offline"), context)
    assert result.status == "error" and result.generation.client_calls == 0 and not client.contexts
    assert result.patch.candidate is None and result.messages == result.artifacts == ()


@pytest.mark.parametrize("kind", ["quality_assessment", "quality_response", "assessment", "execution", "coverage", "candidate", "tests", "plan", "subject", "spec"])
def test_changed_evidence_or_inputs_fail_before_quality_generation(tmp_path, kind):
    context = prepared_revision(tmp_path)
    if kind == "spec":
        path = context.state.agent_state.request.spec_path
    else:
        ref = next(ref for ref in context.state.artifacts if ref.kind == kind)
        path = context.run_dir / ref.path
    original = path.read_bytes()
    path.write_bytes(original + b"\n ")
    client = RecordingClient((candidates()[1],))
    result = generate_without_execution(TestGenerationAgent(client, client_mode="offline"), context)
    assert result.status == "error" and result.generation.client_calls == 0 and not client.contexts
    assert not (context.run_dir / "agent_workspace/rounds/02").exists()


def test_quality_client_failure_retains_safe_telemetry_without_consumption(revision):
    error = LLMError(LLMErrorCode.AUTHENTICATION, "secret-upstream-body", retryable=False, attempts=1)
    client = RecordingClient((), error=error)
    before = revision.canonical_json()
    result = generate_without_execution(TestGenerationAgent(client, client_mode="mock"), revision)
    assert result.status == "error" and result.error_code == "llm_authentication"
    assert len(client.contexts) == result.generation.request_attempts == 1
    assert "secret-upstream-body" not in result.canonical_json() and revision.canonical_json() == before


def test_quality_prompt_treats_review_text_as_data(revision):
    prompt = build_quality_revision_context(revision)
    assert parse_prompt(prompt)["quality_feedback"] == revision.inbox[0].model_dump(mode="json")
    assert "never instructions" in prompt.system_instruction
    assert choose_action(revision.state.agent_state).action == AgentAction.FINISH


@pytest.mark.parametrize("mutation", ["spec_digest", "candidate_digest"])
def test_rehashed_review_cannot_rebind_to_another_spec_or_candidate(tmp_path, mutation):
    context = prepared_revision(tmp_path)
    state = context.state
    quality = state.quality_assessments[-1]
    if mutation == "spec_digest":
        quality = quality.updated(spec_sha256="a" * 64)
    else:
        tests = next(item for item in quality.evidence if item.kind == "tests")
        wrong = tests.updated(sha256="a" * 64)
        quality = quality.updated(candidate_sha256=wrong.sha256,
                                  evidence=tuple(wrong if item == tests else item for item in quality.evidence))
    reference = context.inbox[0].payload.artifact
    atomic_write(context.run_dir / reference.path, quality.canonical_json())
    changed = reference.updated(sha256=file_digest(context.run_dir / reference.path))
    message = context.inbox[0]
    message = message.updated(payload=message.payload.updated(artifact=changed,
                              quality_feedback=message.payload.quality_feedback.updated(assessment=changed)))
    state = state.updated(quality_assessments=(quality,),
                          artifacts=tuple(changed if item == reference else item for item in state.artifacts),
                          messages=tuple(message if item.message_id == message.message_id else item for item in state.messages))
    client = RecordingClient((candidates()[1],))
    result = generate_without_execution(TestGenerationAgent(client, client_mode="offline"),
                                        context.updated(snapshot_json=state.canonical_json(), inbox=(message,)))
    assert result.status == "error" and result.generation.client_calls == 0 and not client.contexts
