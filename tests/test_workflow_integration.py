"""P4 integration: real tools, actual feedback and bounded orchestrator routes."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from testgen_agent.models import TaskStatus, StopReason
from testgen_agent.config import AppConfig
from testgen_agent.llm_client import LLMError, LLMErrorCode
from testgen_agent.tools import run_tests_with_coverage

from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.cli import main
from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext, AgentResult
from test_workflow.definitions import TESTING_WORKFLOW, NodeDefinition
from test_workflow.demo.client import FixtureClient
from test_workflow.events import Event
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import file_digest, verify_artifact
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def prepare(tmp_path, source, fixtures, *, max_rounds=3, timeout=30, generation=None, execution=None):
    state = create_run(source=ROOT / f"examples/{source}.py", specification=ROOT / f"examples/{source}.md",
                       output=tmp_path, config=WorkflowConfig(mode="offline", max_rounds=max_rounds,
                                                             execution_timeout_seconds=timeout))
    client = FixtureClient(tuple(FIXTURES / fixture for fixture in fixtures))
    registry = AgentRegistry()
    registry.register("test_generation", generation or TestGenerationAgent(client, client_mode="offline"))
    registry.register("test_execution", execution or TestExecutionAgent())
    return state, client, WorkflowRunner(registry, TESTING_WORKFLOW)


def read_summary(state):
    assert StateStore(state.run_dir).load() == state
    return json.loads((state.run_dir / "summary.json").read_text(encoding="utf-8"))


def test_real_feedback_is_consumed_and_changes_next_prompt(tmp_path):
    with patch("test_workflow.agents.test_execution.run_tests_with_coverage", wraps=run_tests_with_coverage) as tool:
        state, client, runner = prepare(tmp_path, "validation", ("validation_round_01.json", "validation_round_02.json"))
        digest = file_digest(state.agent_state.request.source_path)
        final = runner.run(state)
    assert final.workflow_status == "finished" and final.task_status == TaskStatus.COMPLETED
    assert final.stop_reason == StopReason.COVERAGE_TARGET_MET and final.selected_round == 2
    assert len(client.contexts) == 2 and tool.call_count == 2
    assert [call.kwargs["round_number"] for call in tool.call_args_list] == [1, 2]
    feedback = next(item for item in final.messages if item.type == "execution_feedback")
    ctx = AgentContext.model_validate_json((final.run_dir / "node_results/02/test_generation/context.json").read_text(encoding="utf-8"))
    assert ctx.inbox == (feedback,) and feedback.message_id in final.consumed_message_ids
    assert feedback.payload.feedback.protected_tests == ("test_score_pass",)
    assert feedback.model_dump(mode="json") == json.loads(client.contexts[1].user_prompt.split(
        "<WORKFLOW_HANDOFF_DATA>\n")[1].split("\n</WORKFLOW_HANDOFF_DATA>")[0])
    assert "coverage_source_hints" in client.contexts[1].user_prompt
    assert file_digest(state.agent_state.request.source_path) == digest
    assert all(verify_artifact(final.run_dir, ref).is_file() for ref in final.artifacts)
    summary = read_summary(final)
    assert summary["external_requests"] == 0 and summary["selected_all_passed"] is True
    assert file_digest(Path(summary["final_test_file"])) == summary["final_test_sha256"]
    events = [Event.model_validate_json(line) for line in (final.run_dir / "events.jsonl").read_text().splitlines()]
    assert [item.seq for item in events] == list(range(1, final.last_event_seq + 1))
    assert all(item.duration_seconds >= 0 for item in events if item.type == "node_finished")
    assert len([item for item in events if item.type == "route_selected"]) == 2
    with pytest.raises(ValueError):
        runner.run(final)


@pytest.mark.parametrize("fixtures,max_rounds,status,stop,rounds,executions,selected", [
    (("calculation_complete.json",), 3, "completed", "coverage_target_met", 1, 1, 1),
    (("calculation_invalid_syntax.json", "calculation_complete.json"), 3, "completed", "coverage_target_met", 2, 1, 2),
    (("p3/name_error.json", "calculation_complete.json"), 3, "completed", "coverage_target_met", 2, 2, 2),
    (("calculation_invalid_syntax.json", "p4/invalid_round_02.json", "p4/invalid_round_03.json"), 3,
     "failed", "round_limit_reached", 3, 0, None),
    (("calculation_invalid_syntax.json",), 1, "failed", "round_limit_reached", 1, 0, None),
    (("p3/ambiguous_assertion.json",), 3, "needs_review", "ambiguous_specification", 1, 1, 1),
])
def test_calculation_routes(tmp_path, fixtures, max_rounds, status, stop, rounds, executions, selected):
    state, client, runner = prepare(tmp_path, "calculation", fixtures, max_rounds=max_rounds)
    final = runner.run(state)
    assert final.workflow_status == "finished" and final.task_status == status
    assert final.stop_reason == stop and final.selected_round == selected
    assert len(client.contexts) == rounds == len(final.agent_state.history)
    assert sum(item.execution is not None for item in final.agent_state.history) == executions
    read_summary(final)


@pytest.mark.parametrize("fixtures,max_rounds,stop,rounds,selected", [
    (("validation_round_01.json",), 1, "round_limit_reached", 1, 1),
    (("validation_round_01.json", "validation_round_01.json"), 3, "no_progress", 2, 1),
    (("validation_round_01.json", "p4/no_coverage_progress.json"), 3, "no_progress", 2, 2),
    (("validation_round_01.json", "validation_removed_pass.json"), 2, "round_limit_reached", 2, 1),
    (("validation_round_01.json", "validation_weakened_pass.json"), 2, "round_limit_reached", 2, 1),
    (("validation_round_01.json", "p4/later_name_error.json"), 2, "round_limit_reached", 2, 1),
])
def test_stop_and_history_selection(tmp_path, fixtures, max_rounds, stop, rounds, selected):
    state, client, runner = prepare(tmp_path, "validation", fixtures, max_rounds=max_rounds)
    final = runner.run(state)
    assert final.workflow_status == "finished" and final.task_status == "partial"
    assert final.stop_reason == stop and final.selected_round == selected
    assert len(client.contexts) == rounds == len(final.agent_state.history)
    summary = read_summary(final)
    assert summary["selected_all_passed"] is True
    if fixtures[-1] == "p4/later_name_error.json":
        assert summary["latest_all_passed"] is False
        assert final.agent_state.history[-1].execution.kind == "execution_error"
    if selected == 1:
        assert Path(summary["final_test_file"]).read_bytes() == final.agent_state.history[0].test_path.read_bytes()


def test_defect_keeps_spec_supported_failing_assertion(tmp_path):
    state, client, runner = prepare(tmp_path, "discount", ("discount_defect.json",))
    final = runner.run(state)
    assert final.task_status == "needs_review" and final.stop_reason == "suspected_source_defect"
    assert len(client.contexts) == 1
    summary = read_summary(final)
    assert summary["selected_all_passed"] is False
    assert "90" in Path(summary["final_test_file"]).read_text()


def test_timeout_stops_before_another_generation(tmp_path):
    state, client, runner = prepare(tmp_path, "execution/timeout", ("calculation_complete.json",), timeout=1)
    final = runner.run(state)
    assert final.workflow_status == "finished" and final.task_status == "failed"
    assert final.stop_reason == "execution_timeout" and len(client.contexts) == 1
    assert final.agent_state.history[-1].execution.kind == "timeout"
    assert read_summary(final)["final_test_file"] is None


def test_node_exception_blocks_dependency_and_saves_controlled_failure(tmp_path):
    class Broken:
        def run(self, context):
            raise RuntimeError("key=secret-value")
    execution = Mock(spec=TestExecutionAgent)
    state, _, runner = prepare(tmp_path, "calculation", (), generation=Broken(), execution=execution)
    final = runner.run(state)
    assert final.workflow_status == "error" and final.error_code == "node_exception"
    assert final.task_status == "failed" and execution.run.call_count == 0
    assert (final.run_dir / "node_results/01/test_generation/context.json").exists()
    for path in ("state.json", "events.jsonl", "summary.json", "report.md"):
        assert "secret-value" not in (final.run_dir / path).read_text(encoding="utf-8")
    read_summary(final)


def test_later_generation_error_retains_previous_available_tests(tmp_path):
    state, client, runner = prepare(tmp_path, "validation", ("validation_round_01.json",))
    final = runner.run(state)  # Deliberately exhaust the fixture client after first feedback.
    assert final.workflow_status == "error" and final.task_status == "partial"
    assert final.error_code == "generation_internal_error" and final.selected_round == 1
    assert len(client.contexts) == 2 and len(final.agent_state.history) == 1
    assert read_summary(final)["latest_all_passed"] is True


def test_model_error_records_attempts_without_an_outer_retry(tmp_path):
    class FailedClient:
        calls = 0
        def generate_tests(self, context):
            self.calls += 1
            raise LLMError(LLMErrorCode.SERVICE_UNAVAILABLE, "secret-value", retryable=True, attempts=3)
    client = FailedClient()
    state, _, runner = prepare(tmp_path, "calculation", (), generation=TestGenerationAgent(client, client_mode="real"))
    state = state.updated(config=state.config.updated(mode="real"))
    final = runner.run(state)
    assert final.workflow_status == "error" and final.error_code == "llm_service_unavailable"
    assert client.calls == 1 and final.agent_state.api_calls == 3
    summary = read_summary(final)
    assert summary["external_requests"] == 3 and summary["generation_calls"] == 1
    assert "secret-value" not in (final.run_dir / "report.md").read_text(encoding="utf-8")


def test_coverage_export_failure_keeps_tests_without_completion(tmp_path):
    state, client, runner = prepare(tmp_path, "calculation", ("calculation_complete.json",))
    with patch("testgen_agent.tools.coverage_runner.subprocess.run", side_effect=OSError("export fault")):
        final = runner.run(state)
    assert final.workflow_status == "finished" and final.task_status == "partial"
    assert final.stop_reason == "unrecoverable_error" and len(client.contexts) == 1
    summary = read_summary(final)
    assert summary["selected_all_passed"] is True and summary["selected_coverage"]["status"] == "unavailable"


def test_invalid_action_is_rejected_without_execution(tmp_path):
    class Unknown:
        def run(self, context):
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id).model_copy(
                update={"patch": {"next_action": "unknown_action"}})
    execution = Mock(spec=TestExecutionAgent)
    state, _, runner = prepare(tmp_path, "calculation", (), generation=Unknown(), execution=execution)
    with pytest.warns(UserWarning, match="serializer warnings"):
        final = runner.run(state)
    assert final.workflow_status == "error" and final.error_code == "invalid_handoff"
    assert execution.run.call_count == 0


def test_export_failure_is_not_saved_as_success(tmp_path):
    state, _, runner = prepare(tmp_path, "calculation", ("calculation_complete.json",))
    with patch("test_workflow.reporting.atomic_write", side_effect=OSError("disk fault")):
        with pytest.raises(OSError):
            runner.run(state)
    saved = StateStore(state.run_dir).load()
    assert saved.workflow_status == "error" and saved.error_code == "export_error"
    assert saved.task_status == "partial" and saved.stop_reason == "unrecoverable_error"


def test_registered_trailing_node_runs_without_loop_edits(tmp_path):
    class Summary:
        def run(self, context):
            assert context.state.task_status == "completed"
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id)
    state, _, runner = prepare(tmp_path, "calculation", ("calculation_complete.json",))
    runner.registry.register("summary", Summary())
    definition = TESTING_WORKFLOW.updated(nodes=(*TESTING_WORKFLOW.nodes, NodeDefinition(agent_id="summary")))
    final = WorkflowRunner(runner.registry, definition).run(state)
    assert final.task_status == "completed" and final.workflow_status == "finished"
    assert (final.run_dir / "node_results/01/summary/result.json").exists()


def test_cli_real_execution_offline_generation_and_exact_output(tmp_path, capsys):
    output = tmp_path / "中文 带空格运行"
    args = ["run", "--demo", "--source", str(ROOT / "examples/calculation.py"),
            "--spec", str(ROOT / "examples/calculation.md"), "--output", str(output)]
    with patch("test_workflow.cli.load_config", side_effect=AssertionError("offline cannot read credentials")):
        assert main(args) == 0
        assert "task_status=completed" in capsys.readouterr().out
        before = (output / "state.json").read_bytes()
        assert main(args) == 2
        assert (output / "state.json").read_bytes() == before
    assert StateStore(output).load().agent_state.history[0].execution.passed == 3


def test_cli_real_mode_wiring_with_mocked_model_client(tmp_path, capsys):
    client = FixtureClient((FIXTURES / "calculation_complete.json",))
    settings = AppConfig(model="mock-model", api_key="mock-key")
    output = tmp_path / "mocked-real-mode"
    with patch("test_workflow.cli.load_config", return_value=settings) as load, patch(
        "test_workflow.cli.DeepSeekClient", return_value=client) as factory:
        assert main(["run", "--source", str(ROOT / "examples/calculation.py"), "--spec",
                     str(ROOT / "examples/calculation.md"), "--output", str(output), "--env-file", "chosen.env",
                     "--max-rounds", "1", "--timeout", "10"]) == 0
    load.assert_called_once_with(env_file=Path("chosen.env"))
    factory.assert_called_once_with(settings)
    saved = StateStore(output).load()
    assert saved.config.mode == "real" and saved.config.model.model == "mock-model"
    assert saved.config.max_rounds == 1 and saved.config.execution_timeout_seconds == 10
    assert "mock-key" not in (output / "state.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("extra", [
    ["--timeout", "0"], ["--fixture", "missing.json"], ["--env-file", "unused.env"],
])
def test_cli_invalid_settings_create_no_run(tmp_path, extra):
    output = tmp_path / "new"
    assert main(["run", "--demo", "--source", str(ROOT / "examples/calculation.py"),
                 "--spec", str(ROOT / "examples/calculation.md"), "--output", str(output), *extra]) == 2
    assert not output.exists()
