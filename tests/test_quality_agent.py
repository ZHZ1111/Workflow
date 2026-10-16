"""S3 uses real generation/execution prerequisites, then reviews without pytest."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from pydantic import SecretStr
from testgen_agent.config import AppConfig
from testgen_agent.models import AgentAction, CandidateRound, TaskStatus

from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_quality_review import TestQualityReviewAgent
from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext, AgentResult, ExecutionAssessment, Message, MessagePayload, QualityAssessment, StatePatch
from test_workflow.definitions import EVALUATION_WORKFLOW
from test_workflow.demo.client import FixtureClient
from test_workflow.demo.quality_client import FixtureReviewClient
from test_workflow.quality_client import DeepSeekReviewClient, ReviewResponse
from test_workflow.quality_contracts import Finding, ReviewDecision
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore, utc_now
from test_workflow.storage import atomic_write, file_digest, verify_artifact
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src/test_workflow/demo/data"
SECRET = "quality-agent-exception-secret"


def make_context(state, node, inbox=(), number=1):
    state = state.updated(workflow_status="running", current_node=node, current_round=number)
    return AgentContext(run_id=state.run_id, round=number, agent_id=node, run_dir=state.run_dir,
                        snapshot_json=state.canonical_json(), inbox=inbox)


def prepare(tmp_path, *, source="calculation", fixture="calculation_complete.json", max_rounds=3, spec_append="", second_fixture=None):
    # Copies ensure corruption tests never edit repository example inputs.
    source_path = tmp_path / f"{source}.py"
    spec_path = tmp_path / f"{source}.md"
    source_path.write_bytes((ROOT / f"examples/{source}.py").read_bytes())
    spec_path.write_text((ROOT / f"examples/{source}.md").read_text(encoding="utf-8") + spec_append, encoding="utf-8")
    state = create_run(source=source_path, specification=spec_path, output=tmp_path / "runs",
                       config=WorkflowConfig(mode="offline", max_rounds=max_rounds, quality_review_enabled=True))
    fixtures = (DATA / fixture,) + ((DATA / second_fixture,) if second_fixture else ())
    generator = TestGenerationAgent(FixtureClient(fixtures), client_mode="offline")
    executor = TestExecutionAgent()
    registry = AgentRegistry()
    registry.register("test_generation", generator)
    registry.register("test_execution", executor)
    runner = WorkflowRunner(registry, EVALUATION_WORKFLOW)
    gc = make_context(state, "test_generation")
    generated = generator.run(gc)
    assert generated.status == "ok", generated.error_code
    state = runner.apply_result(gc.state, generated)
    ec = make_context(state, "test_execution", generated.messages)
    executed = executor.run(ec)
    assert executed.status == "ok", executed.error_code
    state = runner.apply_result(ec.state, executed, tuple(item.message_id for item in generated.messages))
    number = 1
    if second_fixture:
        gc = make_context(state, "test_generation", executed.messages, number=2)
        generated = generator.run(gc)
        assert generated.status == "ok", generated.error_code
        state = runner.apply_result(gc.state, generated, tuple(item.message_id for item in executed.messages))
        ec = make_context(state, "test_execution", generated.messages, number=2)
        executed = executor.run(ec)
        assert executed.status == "ok", executed.error_code
        state = runner.apply_result(ec.state, executed, tuple(item.message_id for item in generated.messages))
        number = 2
    ref = next(item for item in executed.artifacts if item.kind == "assessment")
    # S4 will produce/register this message in the real coordinator. This S3 host
    # fixture supplies the registered handoff after committing real tool evidence.
    ready = Message(message_id=f"review-ready-{number}", run_id=state.run_id, round=number,
                    sender="test_execution", receiver="test_quality_review", type="review_ready",
                    payload=MessagePayload(candidate_id=ref.candidate_id, artifact=ref, reason="Ready for review"))
    state = state.updated(messages=(*state.messages, ready))
    ctx = make_context(state, "test_quality_review", (ready,), number=number)
    StateStore(ctx.run_dir).save(ctx.state)
    return ctx


@pytest.fixture
def ctx(tmp_path):
    return prepare(tmp_path)


def change_state(ctx, **changes):
    return ctx.updated(snapshot_json=ctx.state.updated(**changes).canonical_json())


def approval_client():
    return FixtureReviewClient((DATA / "quality_approve.json",))


def revision(ctx, *, related=True):
    candidate = ctx.state.agent_state.history[-1]
    return ReviewDecision(verdict="request_revision", summary="补充规格支持的输入组合。", findings=(Finding(
        finding_id="more-inputs", category="missing_requirement", blocking=True, target_functions=("calculate_total",),
        related_tests=(candidate.generated.mappings[0].test_function,) if related else (),
        spec_quote="返回整数 `unit_price * quantity`", description="建议再验证一组符合输入域的组合。",
        suggested_case="保留原测试，新增 calculate_total(7, 3) == 21。"),))


def decision_client(ctx, value):
    path = ctx.run_dir / "controlled-review.json"
    atomic_write(path, value.canonical_json())
    return FixtureReviewClient((path,))


def refresh_reference(ctx, old):
    new = old.updated(sha256=file_digest(ctx.run_dir / old.path))
    messages = tuple(message.updated(payload=message.payload.updated(artifact=new))
                     if message.type == "review_ready" and message.payload.artifact == old else message
                     for message in ctx.state.messages)
    state = ctx.state.updated(artifacts=tuple(new if ref == old else ref for ref in ctx.state.artifacts), messages=messages)
    return ctx.updated(snapshot_json=state.canonical_json(), inbox=tuple(message for message in messages if message.receiver == "test_quality_review"))


def test_one_review_preserves_inputs_state_and_records_exact_approval(ctx):
    client = approval_client()
    agent = TestQualityReviewAgent(client)
    registry = AgentRegistry()
    registry.register("test_quality_review", agent)
    before = ctx.canonical_json()
    files = [ctx.run_dir / ref.path for ref in ctx.state.artifacts]
    files += [ctx.state.agent_state.request.source_path, ctx.state.agent_state.request.spec_path, ctx.run_dir / "state.json"]
    snapshots = {path: (file_digest(path), path.stat().st_mtime_ns) for path in files}
    with patch("subprocess.Popen", side_effect=AssertionError("review executed pytest")), patch(
        "test_workflow.agents.test_execution.run_tests_with_coverage", side_effect=AssertionError("execution called")
    ):
        result = registry.get("test_quality_review").run(ctx)
    assert result.status == "ok", result.error_code
    assert len(client.contexts) == result.review.client_calls == 1 and result.review.external_requests == 0
    quality = result.quality_assessment
    assert quality.decision.verdict == "approve" and result.patch == StatePatch() and not result.messages
    assert result.assessment is result.generation is None
    assert (quality.run_id, quality.round, quality.candidate_id) == (ctx.run_id, 1, f"{ctx.run_id}:1")
    tests = next(ref for ref in quality.evidence if ref.kind == "tests")
    assert quality.candidate_sha256 == tests.sha256
    assert quality.spec_sha256 == hashlib.sha256(client.contexts[0].specification.encode("utf-8")).hexdigest()
    assert quality.execution_assessment in ctx.state.artifacts and all(ref in ctx.state.artifacts for ref in quality.evidence)
    saved = {ref.kind: verify_artifact(ctx.run_dir, ref) for ref in result.artifacts}
    assert QualityAssessment.model_validate_json(saved["quality_assessment"].read_text(encoding="utf-8")) == quality
    response = ReviewResponse.model_validate_json(saved["quality_response"].read_text(encoding="utf-8"))
    assert response.decision == quality.decision and response.telemetry == result.review
    assert AgentResult.model_validate_json(result.canonical_json()) == result
    assert ctx.canonical_json() == before
    assert snapshots == {path: (file_digest(path), path.stat().st_mtime_ns) for path in files}


@pytest.mark.parametrize("related", [True, False])
def test_revision_feedback_is_typed_and_missing_scenario_needs_no_existing_test_name(ctx, related):
    value = revision(ctx, related=related)
    result = TestQualityReviewAgent(decision_client(ctx, value)).run(ctx)
    assert result.status == "ok", result.error_code
    assert result.quality_assessment.decision == value
    message = result.messages[0]
    assert message.type == "quality_feedback" and message.sender == "test_quality_review" and message.receiver == "test_generation"
    assert message.payload.recommended_action == AgentAction.ADD_MISSING_CASES
    assert message.payload.quality_feedback.findings == value.findings
    assert message.payload.artifact in result.artifacts and not message.payload.feedback
    assert message.message_id not in ctx.state.consumed_message_ids and message not in ctx.state.messages


def test_revision_on_last_candidate_preserves_findings_without_feedback(tmp_path):
    ctx = prepare(tmp_path, max_rounds=1)
    result = TestQualityReviewAgent(decision_client(ctx, revision(ctx))).run(ctx)
    assert result.status == "ok" and result.quality_assessment.decision.verdict == "request_revision"
    assert not result.messages and ctx.state.task_status is None and ctx.state.stop_reason is None


def test_concrete_spec_conflict_returns_human_review_without_generation_feedback(tmp_path):
    quote = "输入 12 和 3 必须返回 36，同时必须返回 37。"
    ctx = prepare(tmp_path, spec_append="\n" + quote)
    value = ReviewDecision(verdict="request_review", summary="规格中的同一输入有冲突预期。", questions=("该输入应返回 36 还是 37？",),
                            findings=(Finding(finding_id="conflict", category="ambiguous_specification", blocking=True,
                                              target_functions=("calculate_total",), spec_quote=quote, description="两项预期矛盾。"),))
    result = TestQualityReviewAgent(decision_client(ctx, value)).run(ctx)
    assert result.status == "ok", result.error_code
    assert result.quality_assessment.decision == value and not result.messages and not result.patch.issues


@pytest.mark.parametrize("field,value", [
    ("target_functions", ("unknown_function",)), ("related_tests", ("test_does_not_exist",)),
    ("spec_quote", "规格中不存在的引用"), ("test_code_quote", "assert fabricated_result == True"),
])
def test_invalid_model_evidence_cannot_be_saved_as_success(ctx, field, value):
    good = revision(ctx)
    value = good.updated(findings=(good.findings[0].updated(**{field: value}),))
    client = decision_client(ctx, value)
    result = TestQualityReviewAgent(client).run(ctx)
    assert result.status == "error" and result.error_code == "quality_evidence_invalid"
    assert len(client.contexts) == 1 and result.review.request_attempts == 0
    assert result.quality_assessment is None and not result.messages and not result.artifacts
    assert not (ctx.run_dir / "quality_reviews/01/assessment.json").exists()


@pytest.mark.parametrize("mutation", [
    "disabled", "pending", "wrong_node", "wrong_run", "wrong_round", "business_stopped",
    "missing_message", "unregistered_message", "consumed_message", "duplicate_message", "wrong_message_type",
    "test_digest", "plan_digest", "subject_digest", "assessment_digest", "execution_digest", "coverage_digest",
    "source_changed", "spec_changed", "missing_tests_reference", "unregistered_execution", "future_subject", "existing_output",
])
def test_preflight_rejects_invalid_context_or_evidence_before_model_call(ctx, mutation):
    state = ctx.state
    if mutation == "disabled":
        ctx = change_state(ctx, config=state.config.updated(quality_review_enabled=False), quality_status="disabled")
    elif mutation == "pending":
        ctx = change_state(ctx, workflow_status="pending")
    elif mutation == "wrong_node":
        ctx = ctx.updated(agent_id="test_execution", snapshot_json=state.updated(current_node="test_execution").canonical_json(), inbox=())
    elif mutation == "wrong_run":
        ctx = ctx.model_copy(update={"run_id": "foreign-task"})
    elif mutation == "wrong_round":
        ctx = ctx.model_copy(update={"round": 2})
    elif mutation == "business_stopped":
        ctx = change_state(ctx, agent_state=state.agent_state.updated(stop_reason="coverage_target_met"))
    elif mutation == "missing_message":
        ctx = ctx.updated(inbox=())
    elif mutation == "unregistered_message":
        ctx = change_state(ctx, messages=tuple(message for message in state.messages if message.type != "review_ready"))
    elif mutation == "consumed_message":
        ctx = change_state(ctx, consumed_message_ids=(*state.consumed_message_ids, ctx.inbox[0].message_id))
    elif mutation == "duplicate_message":
        duplicate = ctx.inbox[0].updated(message_id="duplicate-review-ready")
        ctx = change_state(ctx, messages=(*state.messages, duplicate))
    elif mutation == "wrong_message_type":
        ctx = ctx.updated(inbox=(ctx.inbox[0].updated(type="demo_observation"),))
    elif mutation.endswith("_digest"):
        kind = mutation.removesuffix("_digest")
        kind = "tests" if kind == "test" else kind
        ref = next(ref for ref in state.artifacts if ref.kind == kind)
        path = ctx.run_dir / ref.path
        path.write_bytes(path.read_bytes() + b"\n ")
    elif mutation == "source_changed":
        path = state.agent_state.request.source_path
        path.write_bytes(path.read_bytes() + b"\n# changed\n")
    elif mutation == "spec_changed":
        state.agent_state.request.spec_path.write_text("Changed specification", encoding="utf-8")
    elif mutation in {"missing_tests_reference", "unregistered_execution"}:
        kind = "tests" if mutation == "missing_tests_reference" else "execution"
        ctx = change_state(ctx, artifacts=tuple(ref for ref in state.artifacts if ref.kind != kind))
    elif mutation == "future_subject":
        ctx = change_state(ctx, artifacts=tuple(ref.updated(round=2, candidate_id=f"{ctx.run_id}:2") if ref.kind == "subject" else ref
                                                for ref in state.artifacts))
    else:
        (ctx.run_dir / "quality_reviews/01").mkdir(parents=True)
    client = approval_client()
    result = TestQualityReviewAgent(client).run(ctx)
    assert result.status == "error", mutation
    assert not client.contexts and result.review.client_calls == result.review.request_attempts == 0
    assert result.patch == StatePatch() and not result.artifacts and result.quality_assessment is None


@pytest.mark.parametrize("source,fixture", [("validation", "validation_round_01.json"), ("discount", "discount_defect.json")])
def test_coverage_gap_or_source_defect_never_calls_review(tmp_path, source, fixture):
    ctx = prepare(tmp_path, source=source, fixture=fixture)
    client = approval_client()
    result = TestQualityReviewAgent(client).run(ctx)
    assert result.status == "error" and result.error_code == "quality_base_not_completed" and not client.contexts


@pytest.mark.parametrize("case", ["candidate_contents", "injected_assessment", "empty_execution_evidence"])
def test_rehashed_forged_content_is_rejected_by_semantic_checks(ctx, case):
    if case == "candidate_contents":
        ref = next(ref for ref in ctx.state.artifacts if ref.kind == "candidate")
        value = CandidateRound.model_validate_json((ctx.run_dir / ref.path).read_text(encoding="utf-8"))
        value = value.updated(generated=value.generated.updated(code=value.generated.code + "\n# changed\n"))
    else:
        ref = ctx.inbox[0].payload.artifact
        value = ExecutionAssessment.model_validate_json((ctx.run_dir / ref.path).read_text(encoding="utf-8"))
        value = value.updated(observation_mode="injected") if case == "injected_assessment" else value.updated(evidence=())
    atomic_write(ctx.run_dir / ref.path, value.canonical_json())
    ctx = refresh_reference(ctx, ref)
    client = approval_client()
    result = TestQualityReviewAgent(client).run(ctx)
    assert result.status == "error" and not client.contexts


def test_same_candidate_cannot_call_review_twice_even_without_state_commit(ctx):
    client = approval_client()
    agent = TestQualityReviewAgent(client)
    assert agent.run(ctx).status == "ok"
    repeated = agent.run(ctx)
    assert repeated.error_code == "candidate_already_reviewed" and repeated.review.client_calls == 0
    assert len(client.contexts) == 1


@pytest.mark.parametrize("kind", ["tests", "plan", "subject", "assessment", "spec"])
def test_inputs_changed_by_client_are_rejected_after_call(ctx, kind):
    wrapped = approval_client()

    def review(request):
        result = wrapped.review(request)
        path = ctx.state.agent_state.request.spec_path if kind == "spec" else ctx.run_dir / next(
            ref.path for ref in ctx.state.artifacts if ref.kind == kind)
        path.write_bytes(path.read_bytes() + b"\n# changed during review\n")
        return result

    result = TestQualityReviewAgent(Mock(review=review, safe_config={}), client_mode="offline").run(ctx)
    assert result.status == "error" and result.review.client_calls == 1
    assert not result.artifacts and not (ctx.run_dir / "quality_reviews/01/assessment.json").exists()


@pytest.mark.parametrize("failure,attempts,code", [
    ("authentication", 1, "review_authentication"), ("timeout", 3, "review_timeout"), ("format", 2, "invalid_review_output"),
])
def test_client_failures_keep_actual_telemetry_without_outer_retry_or_false_approval(ctx, failure, attempts, code):
    requests = []

    def handle(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if failure == "authentication":
            return httpx.Response(401, text=SECRET)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": SECRET}}]})

    config = AppConfig(model="quality-test", api_key=SecretStr(SECRET))
    client = DeepSeekReviewClient(config, transport=httpx.MockTransport(handle), sleep=lambda _: None)
    result = TestQualityReviewAgent(client).run(ctx)
    assert result.status == "error" and result.error_code == code
    assert result.review.request_attempts == len(requests) == attempts and result.review.external_requests == 0
    assert result.review.format_repairs == (1 if failure == "format" else 0)
    assert result.quality_assessment is None and not result.artifacts and not result.messages
    assert SECRET not in result.canonical_json()
    saved = (ctx.run_dir / "quality_reviews/01/failure.json").read_text(encoding="utf-8")
    assert SECRET not in saved and json.loads(saved)["error_code"] == code
    repeated = TestQualityReviewAgent(client).run(ctx)
    assert repeated.review.client_calls == 0 and len(requests) == attempts


def test_unknown_client_exception_keeps_unknown_counts_and_safe_diagnostics(ctx):
    client = Mock()
    client.safe_config = {"client_mode": "real", "max_http_attempts": 3}
    client.review.side_effect = RuntimeError(SECRET)
    result = TestQualityReviewAgent(client).run(ctx)
    assert result.error_code == "quality_internal_error" and result.review.request_attempts is None
    assert result.review.external_requests is None and result.review.client_calls == 1
    assert SECRET not in result.canonical_json() + (ctx.run_dir / "quality_reviews/01/failure.json").read_text(encoding="utf-8")


def test_write_failure_keeps_diagnostics_and_no_success_artifacts(ctx):
    original = atomic_write

    def fail_assessment(path, content):
        if path.name == "assessment.json":
            raise PermissionError(SECRET)
        original(path, content)

    with patch("test_workflow.adapters.quality.atomic_write", side_effect=fail_assessment):
        result = TestQualityReviewAgent(approval_client()).run(ctx)
    assert result.error_code == "quality_io_error" and result.review.client_calls == 1
    assert result.diagnostic_paths == ("quality_reviews/01",) and not result.artifacts
    assert (ctx.run_dir / "quality_reviews/01/response.json").exists()
    assert not (ctx.run_dir / "quality_reviews/01/assessment.json").exists()


def test_output_reservation_failure_happens_before_client_call(ctx):
    client = approval_client()
    with patch.object(Path, "mkdir", side_effect=PermissionError(SECRET)):
        result = TestQualityReviewAgent(client).run(ctx)
    assert result.error_code == "quality_io_error" and not client.contexts and result.review.request_attempts == 0


def test_second_candidate_reuses_verified_first_round_source_reference(tmp_path):
    ctx = prepare(tmp_path, source="validation", fixture="validation_round_01.json", second_fixture="validation_round_02.json")
    result = TestQualityReviewAgent(approval_client()).run(ctx)
    assert result.status == "ok", result.error_code
    assert result.quality_assessment.round == 2
    assert next(ref for ref in result.quality_assessment.evidence if ref.kind == "subject").round == 1


def test_saved_approval_can_satisfy_the_independent_terminal_state_gate(ctx):
    result = TestQualityReviewAgent(approval_client()).run(ctx)
    assert result.status == "ok", result.error_code
    # This manual host transition exercises the S1 gate, not an S4 runner route.
    state = ctx.state.updated(quality_status="passed", quality_stop_reason="approved", quality_assessments=(result.quality_assessment,),
                              artifacts=(*ctx.state.artifacts, *result.artifacts))
    finished = state.updated(workflow_status="finished", task_status=TaskStatus.COMPLETED, stop_reason="coverage_target_met",
                              current_node=None, selected_round=1, finished_at=utc_now(),
                              agent_state=state.agent_state.updated(best_available_round=1, stop_reason="coverage_target_met"))
    assert finished.quality_status == "passed" and finished.selected_round == 1


def test_client_cannot_change_host_budget_by_mutating_its_request_copy(tmp_path):
    ctx = prepare(tmp_path, max_rounds=1)
    wrapped = decision_client(ctx, revision(ctx))

    def review(request):
        object.__setattr__(request, "max_rounds", 3)
        return wrapped.review(request)

    result = TestQualityReviewAgent(Mock(review=review, safe_config={}), client_mode="offline").run(ctx)
    assert result.status == "ok", result.error_code
    assert not result.messages and ctx.state.config.max_rounds == 1


@pytest.mark.parametrize("case", ["wrong_type", "wrong_raw_response", "over_budget", "wrong_mode"])
def test_injected_client_cannot_bypass_response_and_telemetry_contracts(ctx, case):
    wrapped = approval_client()

    def review(request):
        response = wrapped.review(request)
        if case == "wrong_type":
            return {"verdict": "approve", "candidate_id": "foreign"}
        if case == "wrong_raw_response":
            return response.model_copy(update={"raw_response": "{}"})
        if case == "over_budget":
            return response.updated(telemetry=response.telemetry.updated(client_mode="mock", request_attempts=2, max_request_attempts=3))
        return response

    mode = "mock" if case in {"over_budget", "wrong_mode"} else "offline"
    limit = 1 if case == "over_budget" else 3
    result = TestQualityReviewAgent(Mock(review=review, safe_config={}), client_mode=mode, max_request_attempts=limit).run(ctx)
    assert result.status == "error" and not result.quality_assessment and not result.artifacts
    if case == "over_budget":
        assert result.error_code == "quality_request_budget_exceeded" and result.review.error_category == "budget"


def test_silent_output_corruption_cannot_be_returned_as_approval(ctx):
    original = atomic_write

    def corrupt_assessment(path, content):
        original(path, "{}" if path.name == "assessment.json" else content)

    with patch("test_workflow.adapters.quality.atomic_write", side_effect=corrupt_assessment):
        result = TestQualityReviewAgent(approval_client()).run(ctx)
    assert result.status == "error" and not result.artifacts and result.quality_assessment is None


def test_advisory_quote_is_also_checked_and_valid_code_quote_is_accepted(ctx):
    value = revision(ctx)
    line = next(line.strip() for line in ctx.state.agent_state.history[-1].generated.code.splitlines() if "assert " in line)
    advisory = value.findings[0].updated(blocking=False, category="redundancy", suggested_case=None, test_code_quote=line)
    approval = ReviewDecision(verdict="approve", summary="通过，保留非阻塞建议。", findings=(advisory,))
    result = TestQualityReviewAgent(decision_client(ctx, approval)).run(ctx)
    assert result.status == "ok", result.error_code
    assert result.quality_assessment.decision.findings == (advisory,)
