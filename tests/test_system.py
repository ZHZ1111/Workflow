"""P5 system boundaries using production agents and controlled corruptions."""

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from testgen_agent.models import GeneratedTests
from testgen_agent.llm_client import LLMGenerationResult, LLMCallRecord

from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.cli import main
from test_workflow.config import WorkflowConfig
from test_workflow.demo.client import FixtureClient
from test_workflow.definitions import TESTING_WORKFLOW
from test_workflow.events import Event
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import file_digest
from test_workflow.workflow import create_run, WorkflowRunner

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def engine(tmp_path, generator, executor, *, source="calculation"):
    initial = create_run(source=ROOT / f"examples/{source}.py", specification=ROOT / f"examples/{source}.md",
                         output=tmp_path, config=WorkflowConfig(mode="offline"))
    registry = AgentRegistry()
    registry.register("test_generation", generator)
    registry.register("test_execution", executor)
    return initial, WorkflowRunner(registry, TESTING_WORKFLOW)


@pytest.mark.parametrize("fault", ["run_id", "round", "node", "receiver", "duplicate", "digest"])
def test_corrupt_first_generation_cannot_launch_execution(tmp_path, fault):
    client = FixtureClient((FIXTURES / "calculation_complete.json",))
    class Corrupted:
        def run(self, context):
            result = TestGenerationAgent(client, client_mode="offline").run(context)
            assert result.status == "ok"
            if fault == "run_id":
                return result.updated(run_id="foreign-task")
            if fault == "round":
                return result.updated(round=2)
            if fault == "node":
                return result.updated(agent_id="test_execution")
            if fault == "receiver":
                return result.updated(messages=(result.messages[0].updated(receiver="test_generation"),))
            if fault == "duplicate":
                return result.updated(messages=(*result.messages, *result.messages))
            (context.run_dir / result.artifacts[0].path).write_text("{}", encoding="utf-8")
            return result
    executor = Mock(spec=TestExecutionAgent)
    initial, runner = engine(tmp_path, Corrupted(), executor)
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.error_code == "invalid_handoff"
    assert final.task_status == "failed" and executor.run.call_count == 0
    assert len(client.contexts) == 1 and final.agent_state.history == ()
    assert StateStore(final.run_dir).load() == final
    events = [Event.model_validate_json(line) for line in (final.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(item.type == "node_failed" for item in events)
    assert events[-1].type == "workflow_error"


def test_wrong_revision_round_preserves_committed_history(tmp_path):
    client = FixtureClient((FIXTURES / "validation_round_01.json", FIXTURES / "validation_round_02.json"))
    hashes = []
    class Corrupted:
        def run(self, context):
            result = TestGenerationAgent(client, client_mode="offline").run(context)
            assert result.status == "ok"
            if context.round == 1:
                hashes.append(file_digest(result.patch.candidate.test_path))
                return result
            return result.updated(round=1)
    initial, runner = engine(tmp_path, Corrupted(), TestExecutionAgent(), source="validation")
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.task_status == "partial"
    assert final.error_code == "invalid_handoff" and final.selected_round == 1
    assert len(final.agent_state.history) == 1 and len(client.contexts) == 2
    assert file_digest(final.agent_state.history[0].test_path) == hashes[0]
    assert not (final.run_dir / "node_results/02/test_execution").exists()
    feedback = next(item for item in final.messages if item.type == "execution_feedback")
    assert feedback.message_id not in final.consumed_message_ids
    summary = json.loads((final.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["selected_all_passed"] is True and summary["task_status"] != "completed"


def test_changed_subject_between_nodes_is_rejected_before_pytest(tmp_path):
    client = FixtureClient((FIXTURES / "calculation_complete.json",))
    class ChangedSubject:
        def run(self, context):
            result = TestGenerationAgent(client, client_mode="offline").run(context)
            (context.run_dir / "agent_workspace/subject.py").write_text("def different(): return 0\n", encoding="utf-8")
            return result
    initial, runner = engine(tmp_path, ChangedSubject(), TestExecutionAgent())
    before = file_digest(initial.agent_state.request.source_path)
    final = runner.run(initial)
    assert final.workflow_status == "error" and final.error_code == "invalid_handoff"
    assert not (final.run_dir / "node_results/01/test_execution").exists()
    assert file_digest(initial.agent_state.request.source_path) == before


def test_persistent_state_write_failure_exposes_last_saved_snapshot(tmp_path):
    client = FixtureClient((FIXTURES / "calculation_complete.json",))
    executor = Mock(spec=TestExecutionAgent)
    initial, runner = engine(tmp_path, TestGenerationAgent(client, client_mode="offline"), executor)
    save = StateStore.save
    def fail_after_generation(store, state):
        if state.agent_state.history:
            raise OSError("controlled full disk")
        return save(store, state)
    with patch.object(StateStore, "save", fail_after_generation):
        with pytest.raises(OSError):
            runner.run(initial)
    saved = StateStore(initial.run_dir).load()
    assert saved.workflow_status == "running" and saved.finished_at is None
    assert saved.agent_state.history == () and executor.run.call_count == 0
    assert (initial.run_dir / "node_results/01/test_generation/result.json").exists()
    assert not (initial.run_dir / "report.md").exists()


def test_blocking_question_goes_to_review_without_pytest(tmp_path):
    generated = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    class QuestionClient:
        calls = 0
        def generate_tests(self, context):
            self.calls += 1
            return LLMGenerationResult(generated=generated.updated(questions=("Which amount domain should apply?",)),
                provider="deepseek", requested_model="fixture-model", response_model="fixture-model", attempts=1,
                format_repairs=0, latency_seconds=0, calls=(LLMCallRecord(purpose="generation"),))
    client = QuestionClient()
    initial, runner = engine(tmp_path, TestGenerationAgent(client, client_mode="offline"), TestExecutionAgent())
    final = runner.run(initial)
    assert final.workflow_status == "finished" and final.task_status == "needs_review"
    assert final.stop_reason == "ambiguous_specification" and client.calls == 1
    assert final.agent_state.history[-1].execution is None and not final.messages


def test_chinese_and_space_input_paths_use_same_real_workflow(tmp_path):
    inputs = tmp_path / "中文 输入路径"
    inputs.mkdir()
    source, spec = inputs / "计算 源码.py", inputs / "行为 规格.md"
    source.write_bytes((ROOT / "examples/calculation.py").read_bytes())
    spec.write_bytes((ROOT / "examples/calculation.md").read_bytes())
    output = tmp_path / "中文 输出路径"
    before = (file_digest(source), file_digest(spec))
    assert main(["run", "--demo", "--source", str(source), "--spec", str(spec), "--output", str(output),
                 "--fixture", str(FIXTURES / "calculation_complete.json")]) == 0
    state = StateStore(output).load()
    assert state.task_status == "completed" and state.agent_state.history[0].execution.passed == 3
    assert before == (file_digest(source), file_digest(spec))


@pytest.mark.parametrize("fault", ["missing_source", "missing_spec", "blank_spec", "directory_source", "class_source", "bad_utf8_spec"])
def test_invalid_input_cannot_spend_generation_call(tmp_path, fault):
    source, spec = tmp_path / "source.py", tmp_path / "spec.md"
    source.write_bytes((ROOT / "examples/calculation.py").read_bytes())
    spec.write_bytes((ROOT / "examples/calculation.md").read_bytes())
    if fault == "missing_source":
        source.unlink()
    elif fault == "missing_spec":
        spec.unlink()
    elif fault == "blank_spec":
        spec.write_text(" \n", encoding="utf-8")
    elif fault == "directory_source":
        source.unlink()
        source.mkdir()
    elif fault == "class_source":
        source.write_text("class Unsupported: pass\n", encoding="utf-8")
    else:
        spec.write_bytes(b"\xff\xfeinvalid")
    output = tmp_path / "run"
    with patch("test_workflow.demo.client.FixtureClient.generate_tests", side_effect=AssertionError("invalid input spent a call")) as call:
        assert main(["run", "--demo", "--source", str(source), "--spec", str(spec), "--output", str(output),
                     "--fixture", str(FIXTURES / "calculation_complete.json")]) == 2
    assert not output.exists() and call.call_count == 0
