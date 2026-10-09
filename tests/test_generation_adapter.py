import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from testgen_agent.agent import choose_action
from testgen_agent.config import AppConfig, ConfigError
from testgen_agent.llm_client import DeepSeekClient, LLMCallRecord, LLMError, LLMErrorCode, LLMGenerationResult, RetryPolicy
from testgen_agent.models import AgentAction, GeneratedTests, TokenUsage
from testgen_agent.tools import run_tests_with_coverage

from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.adapters.agentdev import cumulative_usage
from test_workflow.contracts import AgentContext, AgentResult, Message, MessagePayload
from test_workflow.definitions import DEMO_WORKFLOW
from test_workflow.demo.agents import FakeExecutionAgent
from test_workflow.demo.client import FixtureClient
from test_workflow.registry import AgentRegistry
from test_workflow.storage import file_digest, verify_artifact
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def make_context(state, number=1, inbox=()):
    active = state.updated(workflow_status="running", current_node="test_generation", current_round=number)
    return AgentContext(run_id=active.run_id, round=number, agent_id="test_generation",
                        run_dir=active.run_dir, snapshot_json=active.canonical_json(), inbox=inbox)


def engine(agent):
    registry = AgentRegistry()
    registry.register("test_generation", agent)
    registry.register("test_execution", FakeExecutionAgent())
    return WorkflowRunner(registry, DEMO_WORKFLOW)


def generate_without_execution(agent, context):
    with patch("subprocess.run", side_effect=AssertionError("generation launched a process")), \
         patch("subprocess.Popen", side_effect=AssertionError("generation launched a process")), \
         patch("testgen_agent.agent.run_agent", side_effect=AssertionError("old complete loop called")), \
         patch("testgen_agent.agent.run_initial_step", side_effect=AssertionError("old mixed step called")):
        return agent.run(context)


@pytest.fixture
def validation_state(tmp_path):
    return create_run(source=ROOT / "examples/validation.py", specification=ROOT / "examples/validation.md",
                      output=tmp_path)


def prepared_revision(state, agent, first_result):
    state = engine(agent).apply_result(make_context(state).state, first_result)
    observed = run_tests_with_coverage(state.agent_state.request.output_dir, round_number=1)
    candidate = state.agent_state.history[0].updated(execution=observed.execution, coverage=observed.coverage)
    business = state.agent_state.updated(history=(candidate,))
    action = choose_action(business).action
    business = business.updated(next_action=action)
    artifact = first_result.messages[0].payload.artifact
    feedback = Message(message_id="feedback-01", run_id=state.run_id, round=1,
                       sender="test_execution", receiver="test_generation", type="execution_feedback",
                       payload=MessagePayload(candidate_id=f"{state.run_id}:1", artifact=artifact,
                                              reason="Measured missing coverage; retain passing assertions.",
                                              recommended_action=action))
    state = state.updated(agent_state=business, messages=(*state.messages, feedback))
    return make_context(state, 2, (feedback,))


def test_initial_generates_stages_and_adapts_without_execution(initial):
    client = FixtureClient((FIXTURES / "calculation_complete.json",))
    agent = TestGenerationAgent(client, client_mode="offline")
    ctx = make_context(initial)
    result = generate_without_execution(agent, ctx)
    assert result.status == "ok"
    assert result.patch.candidate.validation_error is None
    assert result.patch.candidate.test_path.is_absolute()
    assert result.patch.candidate.execution is None
    assert result.patch.next_action == AgentAction.RUN_TESTS
    assert result.generation.client_calls == result.generation.request_attempts == 1
    assert result.generation.external_requests == result.patch.api_calls_delta == 0
    assert len(client.contexts) == 1
    assert initial.agent_state.history == ()
    raw = json.loads((initial.run_dir / "agent_workspace/rounds/01/candidate.json").read_text(encoding="utf-8"))
    assert Path(raw["test_path"]).as_posix() == "rounds/01/test_subject.py"
    for item in result.artifacts:
        verify_artifact(initial.run_dir, item)
    committed = engine(agent).apply_result(ctx.state, result)
    assert committed.agent_state.history == (result.patch.candidate,)
    assert AgentResult.model_validate_json(result.canonical_json()) == result


def test_real_generation_node_replaces_fake_node_in_p1_chain(initial):
    agent = TestGenerationAgent(FixtureClient((FIXTURES / "calculation_complete.json",)), client_mode="offline")
    with patch("subprocess.run", side_effect=AssertionError("pytest should not start")):
        result = engine(agent).run(initial)
    assert result.workflow_status == "finished"
    assert len(result.agent_state.history) == 1
    assert result.consumed_message_ids == ("msg-candidate-01",)


class StaticClient:
    safe_config = {"max_http_attempts": 5}

    def __init__(self, generated=None, error=None, attempts=1, usage=None):
        self.generated = generated or GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
        self.error = error
        self.attempts = attempts
        self.usage = usage
        self.calls = 0

    def generate_tests(self, context):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LLMGenerationResult(generated=self.generated, provider="deepseek", requested_model="mock-model",
                                   response_model="mock-model", attempts=self.attempts, format_repairs=0,
                                   latency_seconds=0, calls=(LLMCallRecord(purpose="generation"),), total_usage=self.usage)


def test_syntax_rejection_preserves_candidate_but_emits_no_ready_message(initial):
    generated = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    generated = generated.updated(code="def test_broken(:\n    pass\n")
    client = StaticClient(generated)
    result = generate_without_execution(TestGenerationAgent(client, client_mode="offline"), make_context(initial))
    assert result.status == "ok"
    assert result.patch.candidate.validation_error.startswith("test_syntax")
    assert result.patch.candidate.test_path is None and result.messages == ()
    assert result.patch.next_action == AgentAction.REPAIR_GENERATED_TEST
    assert not (initial.run_dir / "agent_workspace/rounds/01/test_subject.py").exists()
    assert any(item.kind == "agentdev_candidate" for item in result.artifacts)


def test_revision_uses_measured_feedback_and_retains_passing_tests(validation_state):
    client = FixtureClient((FIXTURES / "validation_round_01.json", FIXTURES / "validation_round_02.json"))
    agent = TestGenerationAgent(client, client_mode="offline")
    first = generate_without_execution(agent, make_context(validation_state))
    revision = prepared_revision(validation_state, agent, first)
    old_hash = file_digest(first.patch.candidate.test_path)
    result = generate_without_execution(agent, revision)
    assert result.status == "ok" and result.patch.candidate.validation_error is None
    assert result.patch.candidate.number == 2 and result.patch.candidate.execution is None
    assert file_digest(first.patch.candidate.test_path) == old_hash
    assert len(client.contexts) == 2
    prompt = client.contexts[1].user_prompt
    for text in ("CONTROLLER_FEEDBACK_DATA", "WORKFLOW_HANDOFF_DATA", "protected_test_functions",
                 "coverage_source_hints", "remaining_rounds_including_requested", "behavior_specification", "feedback-01"):
        assert text in prompt
    assert result.messages[0].message_id == "msg-candidate-02"
    assert "rounds/02" in result.patch.candidate.test_path.as_posix()
    committed = engine(agent).apply_result(revision.state, result, ("feedback-01",))
    assert len(committed.agent_state.history) == 2
    assert committed.agent_state.history[0].execution is not None


@pytest.mark.parametrize("change", ["delete", "weaken"])
def test_revision_cannot_remove_or_weaken_passing_assertions(validation_state, change):
    client = FixtureClient((FIXTURES / "validation_round_01.json",))
    agent = TestGenerationAgent(client, client_mode="offline")
    first = agent.run(make_context(validation_state))
    revision = prepared_revision(validation_state, agent, first)
    generated = GeneratedTests.model_validate_json((FIXTURES / "validation_round_02.json").read_text(encoding="utf-8"))
    import ast
    tree = ast.parse(generated.code)
    old_names = {item.test_function for item in first.patch.candidate.generated.mappings}
    if change == "delete":
        removed = next(iter(old_names))
        tree.body = [node for node in tree.body if not isinstance(node, ast.FunctionDef) or node.name != removed]
        mappings = tuple(mapping for mapping in generated.mappings if mapping.test_function != removed)
        scenario_ids = {sid for mapping in mappings for sid in mapping.scenario_ids}
        generated = generated.updated(code=ast.unparse(tree), mappings=mappings,
                                      plan=generated.plan.updated(scenarios=tuple(
                                          item for item in generated.plan.scenarios if item.scenario_id in scenario_ids)))
    else:
        target = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in old_names)
        assertion = next(node for node in target.body if isinstance(node, ast.Assert))
        assertion.test = ast.Compare(left=assertion.test.left, ops=[ast.In()],
                                     comparators=[ast.List(elts=[ast.Constant("pass"), ast.Constant("fail")], ctx=ast.Load())])
        generated = generated.updated(code=ast.unparse(tree))
    replacement = StaticClient(generated)
    result = generate_without_execution(TestGenerationAgent(replacement, client_mode="offline"), revision)
    assert result.status == "ok" and result.patch.candidate.validation_error.startswith("preserve_tests")
    assert result.patch.candidate.test_path is None and result.messages == ()
    assert revision.state.agent_state.history[0].generated.code == first.patch.candidate.generated.code


@pytest.mark.parametrize("code,retryable,category", [
    (LLMErrorCode.TIMEOUT, True, "transient"), (LLMErrorCode.RATE_LIMIT, True, "transient"),
    (LLMErrorCode.AUTHENTICATION, False, "authentication"), (LLMErrorCode.PERMISSION, False, "authentication"),
    (LLMErrorCode.NOT_FOUND, False, "configuration"), (LLMErrorCode.INVALID_GENERATED_OUTPUT, False, "format"),
])
def test_provider_errors_are_typed_counted_and_not_retried_by_agent(initial, code, retryable, category):
    error = LLMError(code, "API_KEY=do-not-leak Authorization: Bearer do-not-leak", retryable=retryable, attempts=3)
    client = StaticClient(error=error)
    result = generate_without_execution(TestGenerationAgent(client, client_mode="mock"), make_context(initial))
    assert result.status == "error" and result.error_code == f"llm_{code.value}"
    assert result.generation.error_category == category and result.generation.request_attempts == 3
    assert client.calls == 1 and result.generation.external_requests == 0
    assert "do-not-leak" not in result.canonical_json()
    assert result.messages == result.artifacts == ()
    assert not (initial.run_dir / "agent_workspace").exists()


@pytest.mark.parametrize("error,code", [(ConfigError("secret"), "generation_configuration_error"),
                                      (RuntimeError("secret"), "generation_internal_error")])
def test_local_configuration_and_unknown_failures(initial, error, code):
    client = StaticClient(error=error)
    result = TestGenerationAgent(client, client_mode="mock").run(make_context(initial))
    assert result.error_code == code and "secret" not in result.canonical_json()


def test_existing_workspace_is_rejected_before_client_call(initial):
    (initial.run_dir / "agent_workspace").mkdir()
    client = StaticClient()
    result = TestGenerationAgent(client, client_mode="mock").run(make_context(initial))
    assert result.error_code == "generation_workspace_exists" and client.calls == 0
    assert result.generation.request_attempts == 0


def test_invalid_target_round_is_rejected_before_client_call(initial):
    client = StaticClient()
    result = TestGenerationAgent(client, client_mode="mock").run(make_context(initial, 2))
    assert result.error_code == "invalid_generation_round" and client.calls == 0


def test_client_returning_too_many_requests_fails_before_staging(initial):
    client = StaticClient(attempts=6)
    result = TestGenerationAgent(client, client_mode="mock").run(make_context(initial))
    assert result.error_code == "request_budget_exceeded" and result.generation.request_attempts == 6
    assert client.calls == 1 and not (initial.run_dir / "agent_workspace").exists()


@pytest.mark.parametrize("mutate,code", [
    (lambda ctx: ctx.updated(inbox=()), "missing_revision_feedback"),
    (lambda ctx: ctx.updated(inbox=(ctx.inbox[0].updated(sender="unknown"),)), "invalid_revision_feedback"),
    (lambda ctx: ctx.updated(inbox=(ctx.inbox[0].updated(payload=ctx.inbox[0].payload.updated(
        recommended_action=AgentAction.REPAIR_GENERATED_TEST)),)), "invalid_revision_feedback"),
])
def test_revision_rejects_missing_or_foreign_feedback(validation_state, mutate, code):
    client = FixtureClient((FIXTURES / "validation_round_01.json",))
    agent = TestGenerationAgent(client, client_mode="offline")
    first = agent.run(make_context(validation_state))
    context = mutate(prepared_revision(validation_state, agent, first))
    result = agent.run(context)
    assert result.error_code == code and len(client.contexts) == 1


def test_changed_source_snapshot_prevents_spending_a_request(validation_state):
    client = FixtureClient((FIXTURES / "validation_round_01.json",))
    agent = TestGenerationAgent(client, client_mode="offline")
    first = agent.run(make_context(validation_state))
    context = prepared_revision(validation_state, agent, first)
    (validation_state.run_dir / "agent_workspace/subject.py").write_text("def changed(): pass", encoding="utf-8")
    result = agent.run(context)
    assert result.error_code == "source_snapshot_changed" and len(client.contexts) == 1


def test_mock_http_reuses_client_retries_and_format_repair(initial):
    requests = []
    fixture = (FIXTURES / "calculation_complete.json").read_text(encoding="utf-8")
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        content = "{}" if len(requests) == 2 else fixture
        return httpx.Response(200, json={"id": "mock-response", "model": "mock-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
    config = AppConfig(model="mock-model", api_key="local-mock-key")
    client = DeepSeekClient(config, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    agent = TestGenerationAgent(client, client_mode="mock")
    result = generate_without_execution(agent, make_context(initial))
    assert result.status == "ok" and len(requests) == result.generation.request_attempts == 3
    assert result.generation.client_calls == 1 and result.generation.format_repairs == 1
    assert result.generation.total_usage == TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)
    assert result.patch.token_usage == result.generation.total_usage
    assert result.patch.api_calls_delta == result.generation.external_requests == 0
    assert "local-mock-key" not in result.canonical_json()


def test_http_authentication_has_no_outer_retry(initial):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(401, text="secret-body")
    client = DeepSeekClient(AppConfig(model="mock-model", api_key="secret-key"), transport=httpx.MockTransport(handler))
    result = TestGenerationAgent(client, client_mode="mock").run(make_context(initial))
    assert result.error_code == "llm_authentication" and len(requests) == 1
    assert result.generation.status_code == 401 and "secret" not in result.canonical_json()


def test_invalid_candidate_can_be_repaired_without_executing_it(initial):
    valid = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    first_agent = TestGenerationAgent(StaticClient(valid.updated(code="def test_broken(: pass")), client_mode="offline")
    first = generate_without_execution(first_agent, make_context(initial))
    state = engine(first_agent).apply_result(make_context(initial).state, first)
    artifact = next(item for item in first.artifacts if item.kind == "candidate")
    feedback = Message(message_id="gate-feedback-01", run_id=state.run_id, round=1,
                       sender="workflow", receiver="test_generation", type="execution_feedback",
                       payload=MessagePayload(candidate_id=f"{state.run_id}:1", artifact=artifact,
                                              reason="Host syntax gate rejected the candidate at line 1.",
                                              recommended_action=AgentAction.REPAIR_GENERATED_TEST))
    state = state.updated(messages=(*state.messages, feedback))
    client = StaticClient(valid)
    repaired = generate_without_execution(TestGenerationAgent(client, client_mode="offline"),
                                          make_context(state, 2, (feedback,)))
    assert repaired.status == "ok" and repaired.patch.candidate.validation_error is None
    assert repaired.patch.candidate.number == 2 and repaired.patch.candidate.execution is None
    assert client.calls == 1 and first.patch.candidate.test_path is None


def test_feedback_cannot_override_a_completed_decision(initial):
    first_agent = TestGenerationAgent(FixtureClient((FIXTURES / "calculation_complete.json",)), client_mode="offline")
    first = first_agent.run(make_context(initial))
    state = engine(first_agent).apply_result(make_context(initial).state, first)
    result = run_tests_with_coverage(state.agent_state.request.output_dir, round_number=1)
    candidate = first.patch.candidate.updated(execution=result.execution, coverage=result.coverage)
    business = state.agent_state.updated(history=(candidate,), next_action=AgentAction.REPAIR_GENERATED_TEST)
    artifact = first.messages[0].payload.artifact
    feedback = Message(message_id="unjustified-repair", run_id=state.run_id, round=1,
                       sender="test_execution", receiver="test_generation", type="execution_feedback",
                       payload=MessagePayload(candidate_id=f"{state.run_id}:1", artifact=artifact,
                                              reason="Try again", recommended_action=AgentAction.REPAIR_GENERATED_TEST))
    state = state.updated(agent_state=business, messages=(*state.messages, feedback))
    client = StaticClient()
    error = TestGenerationAgent(client, client_mode="offline").run(make_context(state, 2, (feedback,)))
    assert error.error_code == "generation_action_mismatch" and client.calls == 0


def test_blocking_questions_do_not_emit_ready_candidate(initial):
    generated = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    result = TestGenerationAgent(StaticClient(generated.updated(questions=("Which oracle applies?",))),
                                 client_mode="offline").run(make_context(initial))
    assert result.status == "ok" and result.patch.next_action == AgentAction.REQUEST_REVIEW
    assert result.messages == ()


@pytest.mark.parametrize("status,content,expected,category", [
    (503, None, "llm_service_unavailable", "transient"),
    (200, "{}", "llm_invalid_generated_output", "format"),
])
def test_provider_retry_exhaustion_is_bounded(initial, status, content, expected, category):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"id": "mock", "model": "mock-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}]})
    client = DeepSeekClient(AppConfig(model="mock-model", api_key="mock-key"),
                            transport=httpx.MockTransport(handler), sleep=lambda _: None)
    result = TestGenerationAgent(client, client_mode="mock").run(make_context(initial))
    assert result.error_code == expected and result.generation.error_category == category
    assert len(requests) == result.generation.request_attempts == 3
    assert result.generation.client_calls == 1
    assert result.generation.format_repairs is None  # Provider errors do not expose a repair counter.


def test_declared_retry_policy_must_fit_requested_budget():
    client = DeepSeekClient(AppConfig(model="mock-model", api_key="mock-key"),
                            retry_policy=RetryPolicy(max_transient_retries=4, max_format_repairs=2))
    with pytest.raises(ValueError):
        TestGenerationAgent(client, max_request_attempts=5)


def test_unknown_failed_request_count_stays_unknown(initial):
    client = StaticClient(error=RuntimeError("secret"))
    result = TestGenerationAgent(client, client_mode="real").run(make_context(initial))
    assert result.status == "error" and result.generation.request_attempts is None
    assert result.generation.external_requests is None


def test_token_usage_accumulates_without_inventing_unknown_counts(initial):
    agent = TestGenerationAgent(StaticClient(), client_mode="offline")
    result = agent.run(make_context(initial))
    business = initial.agent_state.updated(history=(result.patch.candidate,), current_round=1,
                                            token_usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    assert cumulative_usage(business, TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)) == TokenUsage(
        prompt_tokens=30, completion_tokens=15, total_tokens=45)
    assert cumulative_usage(business, None) is None
    updated = engine(agent)._business_patch(make_context(initial).state.updated(agent_state=business),
                                            result.patch.updated(candidate=None, token_usage=None, token_usage_reported=True), ())
    assert updated.token_usage is None
