import json
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from testgen_agent.llm_client import LLMCallRecord, LLMGenerationResult
from testgen_agent.models import AgentAction, GeneratedTests, TaskStatus
from testgen_agent.tools import run_tests_with_coverage

from test_workflow.adapters.execution import evaluate
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext, ExecutionAssessment
from test_workflow.definitions import EVALUATION_WORKFLOW
from test_workflow.demo.client import FixtureClient
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import file_digest, verify_artifact
from test_workflow.workflow import HandoffError, WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


class StaticClient:
    def __init__(self, generated):
        self.generated = generated

    def generate_tests(self, context):
        return LLMGenerationResult(generated=self.generated, provider="deepseek", requested_model="fixture",
                                   response_model="fixture", calls=(LLMCallRecord(purpose="generation"),),
                                   attempts=1, format_repairs=0, latency_seconds=0)


def context(state, node, number=1, inbox=()):
    state = state.updated(workflow_status="running", current_node=node, current_round=number)
    return AgentContext(run_id=state.run_id, round=number, agent_id=node, run_dir=state.run_dir,
                        snapshot_json=state.canonical_json(), inbox=inbox)


def engine(generator, executor):
    registry = AgentRegistry()
    registry.register("test_generation", generator)
    registry.register("test_execution", executor)
    return WorkflowRunner(registry, EVALUATION_WORKFLOW)


def prepare(tmp_path, source="calculation", fixture="calculation_complete.json", generated=None, timeout=30):
    state = create_run(source=ROOT / f"examples/{source}.py", specification=ROOT / f"examples/{source}.md",
                       output=tmp_path, config=WorkflowConfig(execution_timeout_seconds=timeout))
    client = StaticClient(generated) if generated else FixtureClient((FIXTURES / fixture,))
    generator = TestGenerationAgent(client, client_mode="offline")
    executor = TestExecutionAgent()
    runner = engine(generator, executor)
    ctx = context(state, "test_generation")
    result = generator.run(ctx)
    assert result.status == "ok", result.error_code
    state = runner.apply_result(ctx.state, result)
    return context(state, "test_execution", inbox=result.messages), runner, result


def test_once_exact_round_real_target_evidence_and_immutable_state(tmp_path):
    ctx, runner, generated = prepare(tmp_path)
    before = ctx.canonical_json()
    digest = file_digest(ctx.state.agent_state.request.source_path)
    with patch("test_workflow.agents.test_execution.run_tests_with_coverage", wraps=run_tests_with_coverage) as tool:
        result = TestExecutionAgent().run(ctx)
    assert result.status == "ok", result.error_code
    tool.assert_called_once_with(ctx.state.agent_state.request.output_dir, timeout=30, round_number=1)
    assessment = result.assessment
    assert assessment.observation_mode == "real" and assessment.tool_calls == 1 and assessment.pytest_started
    assert assessment.recommendation == "finish" and assessment.task_status == TaskStatus.COMPLETED
    assert assessment.feedback.execution.passed == 3
    with pytest.raises(ValueError, match="completion advice"):
        assessment.updated(action=AgentAction.REPAIR_GENERATED_TEST, recommendation="repair_tests")
    coverage = assessment.feedback.coverage
    assert coverage.line_percent == 100 and coverage.branch_percent is None and coverage.meets_target
    paths = {Path(item.path).name: verify_artifact(ctx.run_dir, item) for item in result.artifacts}
    assert {"execution.json", "pytest_result.json", "pytest.xml", "stdout.txt", "stderr.txt",
            "coverage.json", "coverage_result.json", "coverage_source_hints.json", "assessment.json"} <= paths.keys()
    assert len(json.loads(paths["coverage.json"].read_text())["files"]) == 1
    assert coverage.target_file == ctx.state.agent_state.request.output_dir / "subject.py"
    assert ctx.canonical_json() == before and generated.patch.candidate.execution is None
    committed = runner.apply_result(ctx.state, result, tuple(item.message_id for item in ctx.inbox))
    StateStore(ctx.run_dir).save(committed)
    assert StateStore(ctx.run_dir).load() == committed
    assert committed.agent_state.history[-1].execution == assessment.feedback.execution
    assert committed.task_status is None  # P3 supplies advice; P4 commits final business status.
    assert file_digest(ctx.state.agent_state.request.source_path) == digest
    repeated = TestExecutionAgent(Mock(side_effect=AssertionError("duplicate pytest"))).run(ctx)
    assert repeated.error_code == "candidate_already_evaluated"


def test_real_feedback_drives_one_manual_revision_with_protected_tests(tmp_path):
    state = create_run(source=ROOT / "examples/validation.py", specification=ROOT / "examples/validation.md", output=tmp_path)
    client = FixtureClient((FIXTURES / "validation_round_01.json", FIXTURES / "validation_round_02.json"))
    generator, executor = TestGenerationAgent(client, client_mode="offline"), TestExecutionAgent()
    runner = engine(generator, executor)
    gc = context(state, "test_generation")
    gr = generator.run(gc)
    state = runner.apply_result(gc.state, gr)
    ec = context(state, "test_execution", inbox=gr.messages)
    er = executor.run(ec)
    assert er.status == "ok", er.error_code
    assert er.assessment.recommendation == "extend_tests"
    assert er.assessment.feedback.protected_tests == ("test_score_pass",)
    hints = json.loads(er.assessment.feedback.source_hints_json)["coverage_source_hints"]
    assert hints["missing_lines"] and hints["missing_branches"]
    assert er.messages[0].payload.feedback == er.assessment.feedback
    state = runner.apply_result(ec.state, er, (gr.messages[0].message_id,))
    gc2 = context(state, "test_generation", 2, er.messages)
    gr2 = generator.run(gc2)
    assert gr2.status == "ok", gr2.error_code
    assert "WORKFLOW_HANDOFF_DATA" in client.contexts[-1].user_prompt
    state = runner.apply_result(gc2.state, gr2, (er.messages[0].message_id,))
    ec2 = context(state, "test_execution", 2, gr2.messages)
    with patch("test_workflow.agents.test_execution.run_tests_with_coverage", wraps=run_tests_with_coverage) as tool:
        er2 = TestExecutionAgent().run(ec2)
    assert er2.status == "ok", er2.error_code
    tool.assert_called_once_with(state.agent_state.request.output_dir, timeout=60, round_number=2)
    assert er2.assessment.recommendation == "finish"
    assert er2.assessment.feedback.coverage.branch_percent == 100
    assert len(runner.apply_result(ec2.state, er2).agent_state.history) == 2


@pytest.mark.parametrize("fixture,expected", [("calculation_invalid_syntax.json", "repair_tests")])
def test_invalid_candidate_produces_feedback_without_any_process(tmp_path, fixture, expected):
    ctx, runner, generated = prepare(tmp_path, fixture=fixture)
    tool = Mock(side_effect=AssertionError("invalid code launched pytest"))
    with patch("subprocess.Popen", side_effect=AssertionError("process launched")):
        result = TestExecutionAgent(tool).run(ctx)
    assert result.status == "ok", result.error_code
    tool.assert_not_called()
    assert result.assessment.tool_calls == 0 and not result.assessment.pytest_started
    assert result.assessment.feedback.execution is None and result.assessment.recommendation == expected
    assert result.assessment.feedback.validation_error == generated.patch.candidate.validation_error
    assert runner.apply_result(ctx.state, result).agent_state.history[-1].execution is None


@pytest.mark.parametrize("mutation", ["test_file", "subject", "handoff", "missing_file", "missing_message", "consumed", "unregistered"])
def test_corrupt_or_stale_inputs_never_launch_pytest(tmp_path, mutation):
    ctx, _, _ = prepare(tmp_path)
    state = ctx.state
    if mutation == "test_file":
        state.agent_state.history[-1].test_path.write_text("assert True", encoding="utf-8")
    elif mutation == "subject":
        (state.agent_state.request.output_dir / "subject.py").write_text("def replaced(): return 1", encoding="utf-8")
    elif mutation == "handoff":
        verify_artifact(ctx.run_dir, ctx.inbox[0].payload.artifact).write_text("{}", encoding="utf-8")
    elif mutation == "missing_file":
        state.agent_state.history[-1].test_path.unlink()
    elif mutation == "missing_message":
        ctx = ctx.updated(inbox=())
    elif mutation == "consumed":
        state = state.updated(consumed_message_ids=(ctx.inbox[0].message_id,))
        ctx = ctx.updated(snapshot_json=state.canonical_json())
    elif mutation == "unregistered":
        ctx = ctx.updated(snapshot_json=state.updated(messages=()).canonical_json())
    tool = Mock(side_effect=AssertionError("corrupt input launched pytest"))
    result = TestExecutionAgent(tool).run(ctx)
    tool.assert_not_called()
    assert result.status == "error" and result.patch.candidate is None


@pytest.mark.parametrize("case,kind,recommendation,issue", [
    ("ambiguous", "assertion_failed", "request_review", "ambiguous_specification"),
    ("name_error", "execution_error", "repair_tests", "generation_error"),
    ("collection", "collection_error", "request_review", "ambiguous_specification"),
    ("import", "collection_error", "repair_tests", "generation_error"),
])
def test_real_failure_attribution(tmp_path, case, kind, recommendation, issue):
    base = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    code = base.code
    if case == "ambiguous":
        code = code.replace("== 36", "== 37")
    elif case == "name_error":
        code = code.replace("== 36", "== missing_expected")
    elif case == "import":
        code = code.replace("import pytest", "import pytest\nfrom pytest import nonexistent_p3_symbol")
    else:
        code = code.replace("def test_total_normal():", '@pytest.mark.parametrize("value", [1])\ndef test_total_normal():')
    ctx, runner, _ = prepare(tmp_path, generated=base.updated(code=code))
    result = TestExecutionAgent().run(ctx)
    assert result.status == "ok", result.error_code
    assert result.assessment.feedback.execution.kind == kind
    assert result.assessment.recommendation == recommendation
    assert issue in {item.kind for item in result.assessment.feedback.issues}
    assert result.assessment.task_status != TaskStatus.COMPLETED
    runner.apply_result(ctx.state, result)


def test_spec_evidence_keeps_suspected_defect_and_failing_test(tmp_path):
    ctx, runner, _ = prepare(tmp_path, source="discount", fixture="discount_defect.json")
    original = ctx.state.agent_state.history[-1].generated.code
    result = TestExecutionAgent().run(ctx)
    assert result.status == "ok", result.error_code
    assert result.assessment.recommendation == "request_review"
    assert "suspected_source_defect" in {item.kind for item in result.assessment.feedback.issues}
    assert any(item.evidence for item in result.assessment.feedback.issues)
    assert runner.apply_result(ctx.state, result).agent_state.history[-1].generated.code == original


def test_no_tests_real_backend_observation_cannot_complete(tmp_path):
    # Manual tool fixture, deliberately outside the production static gate.
    # Empty candidates are rejected by that gate before reaching pytest.
    ctx, _, _ = prepare(tmp_path / "task")
    workspace = tmp_path / "manual_workspace"
    (workspace / "rounds/01").mkdir(parents=True)
    (workspace / "subject.py").write_text("def calculate_total(a, b): return a * b\n", encoding="utf-8")
    (workspace / "rounds/01/test_subject.py").write_text("# no tests\n", encoding="utf-8")
    observed = run_tests_with_coverage(workspace)
    assert observed.execution.kind == "no_tests" and observed.execution.collected == 0
    candidate = ctx.state.agent_state.history[-1].updated(execution=observed.execution, coverage=observed.coverage)
    assessment = evaluate(ctx, candidate, None, (), tool_calls=1, observation_mode="injected")
    assert assessment.recommendation == "repair_tests" and assessment.task_status != TaskStatus.COMPLETED


def test_coverage_export_failure_retains_real_execution(tmp_path):
    ctx, runner, _ = prepare(tmp_path)
    with patch("testgen_agent.tools.coverage_runner.subprocess.run", side_effect=OSError("controlled export fault")):
        result = TestExecutionAgent().run(ctx)
    assert result.status == "ok", result.error_code
    assert result.assessment.feedback.execution.all_passed
    assert result.assessment.feedback.execution.passed == 3
    assert result.assessment.feedback.coverage.status == "unavailable"
    assert result.assessment.feedback.coverage.line_percent is None
    assert result.assessment.recommendation == "stop_error"
    assert any(item.kind == "coverage_unavailable" for item in result.assessment.feedback.issues)
    assert any(item.path.endswith("pytest.xml") for item in result.artifacts)
    runner.apply_result(ctx.state, result)


def test_timeout_terminates_pytest_and_keeps_source_and_diagnostics(tmp_path):
    source = tmp_path / "loop.py"
    source.write_text("def calculate_total(unit_price, quantity):\n    while True:\n        pass\n", encoding="utf-8")
    spec = tmp_path / "loop.md"
    spec.write_text("calculate_total(12, 3) returns 36. Controlled timeout fixture.", encoding="utf-8")
    state = create_run(source=source, specification=spec, output=tmp_path / "tasks", config=WorkflowConfig(execution_timeout_seconds=2))
    generator = TestGenerationAgent(FixtureClient((FIXTURES / "calculation_complete.json",)), client_mode="offline")
    runner = engine(generator, TestExecutionAgent())
    gc = context(state, "test_generation")
    gr = generator.run(gc)
    assert gr.status == "ok", gr.error_code
    ec = context(runner.apply_result(gc.state, gr), "test_execution", inbox=gr.messages)
    processes = []
    popen = subprocess.Popen
    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append((args[0], process))
        return process
    digest = file_digest(source)
    with patch("testgen_agent.tools.test_runner.subprocess.Popen", side_effect=record):
        result = TestExecutionAgent().run(ec)
    assert result.status == "ok", result.error_code
    assert result.assessment.feedback.execution.kind == "timeout"
    assert result.assessment.recommendation == "stop_error"
    pytest_processes = [proc for command, proc in processes if "pytest" in command]
    assert len(pytest_processes) == 1 and pytest_processes[0].poll() is not None
    assert file_digest(source) == digest
    assert any(item.path.endswith("execution.json") for item in result.artifacts)
    runner.apply_result(ec.state, result)


def test_forged_assessment_or_message_rejected_before_commit(tmp_path):
    ctx, runner, _ = prepare(tmp_path, source="validation", fixture="validation_round_01.json")
    result = TestExecutionAgent().run(ctx)
    assert result.status == "ok", result.error_code
    reference = next(item for item in result.artifacts if item.kind == "assessment")
    modified = result.assessment.updated(reason="forged reason")
    with pytest.raises(HandoffError, match="assessment"):
        runner.apply_result(ctx.state, result.updated(assessment=modified))
    message = result.messages[0]
    with pytest.raises(HandoffError, match="feedback"):
        runner.apply_result(ctx.state, result.updated(messages=(message.updated(payload=message.payload.updated(feedback=None)),)))
    assert reference in result.artifacts
    with pytest.raises(ValueError):
        ExecutionAssessment.model_validate_json(result.assessment.updated(task_status=TaskStatus.COMPLETED).canonical_json())


@pytest.mark.parametrize("fault", ["source", "test", "evidence"])
def test_post_execution_integrity_or_artifact_fault_preserves_diagnostics(tmp_path, fault):
    ctx, _, _ = prepare(tmp_path)
    original_digest = file_digest(ctx.state.agent_state.request.source_path)
    calls = []
    def execute(workspace, timeout, *, round_number):
        calls.append(round_number)
        observed = run_tests_with_coverage(workspace, timeout, round_number=round_number)
        if fault == "evidence":
            (observed.artifacts / "execution.json").write_text("{}", encoding="utf-8")
        elif fault == "source":
            with (workspace / "subject.py").open("a", encoding="utf-8") as stream:
                stream.write("\n# controlled snapshot change\n")
        else:
            with ctx.state.agent_state.history[-1].test_path.open("a", encoding="utf-8") as stream:
                stream.write("\n# controlled test change\n")
        return observed
    result = TestExecutionAgent(execute).run(ctx)
    assert calls == [1] and file_digest(ctx.state.agent_state.request.source_path) == original_digest
    if fault == "evidence":
        assert result.status == "error" and result.error_code == "execution_contract_error"
        assert result.diagnostic_paths and result.patch.candidate is None
        assert (ctx.run_dir / result.diagnostic_paths[0] / "pytest.xml").is_file()
    else:
        assert result.status == "ok", result.error_code
        assert result.assessment.integrity_error == f"{'source' if fault == 'source' else 'tests'}_changed_during_execution"
        assert result.assessment.recommendation == "stop_error"
        assert result.assessment.feedback.execution.all_passed
        assert result.assessment.task_status != TaskStatus.COMPLETED


def test_foreign_observation_directory_never_commits(tmp_path):
    ctx, _, _ = prepare(tmp_path)
    def execute(workspace, timeout, *, round_number):
        observed = run_tests_with_coverage(workspace, timeout, round_number=round_number)
        return replace(observed, artifacts=tmp_path)
    result = TestExecutionAgent(execute).run(ctx)
    assert result.status == "error" and result.error_code == "invalid_execution_evidence_directory"
    assert result.diagnostic_paths and result.patch.candidate is None


@pytest.mark.parametrize("node,number", [("test_generation", 1), ("test_execution", 2)])
def test_wrong_node_or_round_rejected_before_tool(tmp_path, node, number):
    ctx, _, _ = prepare(tmp_path)
    wrong = context(ctx.state, node, number)
    tool = Mock(side_effect=AssertionError("wrong context executed"))
    result = TestExecutionAgent(tool).run(wrong)
    assert result.error_code == "invalid_execution_context"
    tool.assert_not_called()


def test_empty_candidate_rejected_without_pytest(tmp_path):
    base = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    ctx, runner, gr = prepare(tmp_path, generated=base.updated(code="# empty\n"))
    tool = Mock(side_effect=AssertionError("empty candidate executed"))
    result = TestExecutionAgent(tool).run(ctx)
    assert result.status == "ok", result.error_code
    assert gr.patch.candidate.validation_error.startswith("target_import")
    assert result.assessment.recommendation == "repair_tests"
    tool.assert_not_called()
    runner.apply_result(ctx.state, result)
