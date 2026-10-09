"""E4 persisted event order, diagnostics, real feedback and secret-free metadata."""

import json

import httpx
import pytest

from testgen_agent.agent import AgentTools, run_agent
from testgen_agent.config import load_config
from testgen_agent.generation import stage_initial_candidate
from testgen_agent.llm_client import DeepSeekClient, LLMError, LLMErrorCode, RetryPolicy
from testgen_agent.models import OracleMode
from testgen_agent.prompts import build_generation_context
from testgen_agent.tracing import TraceArtifactError, TraceEvent, TraceWriter
from test_agent_loop import task_request, candidate, POS, ZERO, NEG, run, SequenceClient
from test_failure_analysis import discount_state
from test_llm_client import completion


def events(result):
    return [TraceEvent.model_validate_json(line) for line in result.trace_file.read_text(encoding="utf-8").splitlines()]


def test_real_feedback_trace_captures_actions_results_and_linked_artifacts(task_request):
    result, client = run(task_request, [candidate([POS]), candidate([POS, ZERO]), candidate([POS, ZERO, NEG])])
    trace = events(result)
    assert trace[0].phase == "run_started"
    assert trace[-1].phase == "run_finished"
    assert [item.sequence for item in trace] == list(range(1, len(trace) + 1))
    assert {item.run_id for item in trace} == {result.state.run_id}
    assert all(item.timestamp.utcoffset().total_seconds() == 0 for item in trace)
    assert [item.timestamp for item in trace] == sorted(item.timestamp for item in trace)
    execution = [item for item in trace if item.tool == "run_tests_with_coverage" and item.phase == "tool_finished"]
    assert [item.round_number for item in execution] == [1, 2, 3]
    assert execution[0].result_summary["branch_percent"] < execution[-1].result_summary["branch_percent"]
    assert execution[-1].result_summary["branch_percent"] == 100
    decisions = [item for item in trace if item.phase == "decision"]
    assert any(item.action == "add_missing_cases" and "覆盖" in item.reason for item in decisions)
    assert trace[-1].action == "finish"
    assert trace[-1].result_summary["stop_reason"] == "coverage_target_met"
    for item in execution:
        assert (task_request.output_dir / item.result_summary["artifacts"] / "coverage.json").is_file()
    assert json.loads(result.summary_file.read_text(encoding="utf-8"))["trace_file"] == str(result.trace_file)
    metadata = json.loads(result.metadata_file.read_text(encoding="utf-8"))
    assert metadata["run_id"] == result.state.run_id
    assert metadata["request"]["max_rounds"] == 3
    assert metadata["adapter_config"]["adapter_type"] == "SequenceClient"


def test_defect_failure_trace_explains_stop_and_preserves_raw_test(task_request):
    state = discount_state()
    task_request = state.request.updated(output_dir=task_request.output_dir)
    result, client = run(task_request, [state.history[0].generated], behavior_specification=state.behavior_specification)
    trace = events(result)
    assert trace[-1].result_summary["status"] == "needs_review"
    assert trace[-1].result_summary["stop_reason"] == "suspected_source_defect"
    assert "规格" in trace[-1].reason
    assert len([item for item in trace if item.tool == "llm.generate_tests" and item.phase == "tool_started"]) == 1
    assert "== 90" in result.final_test_file.read_text(encoding="utf-8")


def test_initial_api_failure_leaves_started_failed_and_terminal_events(task_request):
    error = LLMError(LLMErrorCode.NETWORK, "safe network failure", retryable=True, attempts=3)
    result, client = run(task_request, [error])
    trace = events(result)
    model_events = [item for item in trace if item.tool == "llm.generate_tests"]
    assert [item.phase for item in model_events] == ["tool_started", "tool_failed"]
    assert model_events[-1].round_number == 1
    assert model_events[-1].result_summary["attempts"] == 3
    assert trace[-1].state["api_calls"] == 3
    assert trace[-1].result_summary["token_usage"] is None
    assert trace[-1].result_summary["status"] == "failed"
    assert trace[-1].result_summary["stop_reason"] == "unrecoverable_error"


def test_stage_failure_after_model_response_keeps_actual_attempt_count(task_request):
    def failed_stage(**kwargs):
        kwargs["client"].generate_tests(build_generation_context(
            kwargs["source"], kwargs["behavior_specification"], kwargs["oracle_mode"]))
        raise OSError("failed host stage")
    result, client = run(task_request, [candidate([POS])], tools=AgentTools(generate=failed_stage))
    trace = events(result)
    assert result.state.api_calls == 1
    assert any(item.phase == "tool_finished" and item.tool == "llm.generate_tests" for item in trace)
    assert any(item.phase == "tool_failed" and item.tool == "generate_initial_candidate" for item in trace)
    assert trace[-1].result_summary["api_calls"] == 1
    assert result.state.current_round == 0


def test_trace_is_present_before_model_call_and_does_not_contain_prompts(task_request):
    class InspectClient(SequenceClient):
        def generate_tests(self, context):
            path = task_request.output_dir / "trace.jsonl"
            trace = [TraceEvent.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines()]
            assert trace[-1].tool == "llm.generate_tests"
            assert trace[-1].phase == "tool_started"
            return super().generate_tests(context)
    client = InspectClient([candidate([POS, ZERO, NEG])])
    result = run_agent(task_request, client=client, oracle_mode=OracleMode.SPECIFICATION,
                       behavior_specification="UNIQUE SPEC BODY NOT A TRACE FIELD")
    assert "UNIQUE SPEC BODY NOT A TRACE FIELD" not in result.trace_file.read_text(encoding="utf-8")
    assert "def choose" not in result.trace_file.read_text(encoding="utf-8")


def test_deepseek_mock_trace_records_config_repairs_and_usage_without_key(task_request):
    key = "e4-private-api-key-never-log"
    env_file = task_request.source_path.with_name("empty.env")
    env_file.write_text("", encoding="utf-8")
    config = load_config(environ={"LLM_MODEL": "e4-model", "DEEPSEEK_API_KEY": key}, env_file=env_file)
    payload = candidate([POS, ZERO, NEG]).canonical_json()
    responses = iter([completion("not JSON"), completion(payload, usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})])
    transport = httpx.MockTransport(lambda request: next(responses))
    client = DeepSeekClient(config, transport=transport, retry_policy=RetryPolicy(max_transient_retries=0))
    result = run_agent(task_request, client=client, oracle_mode=OracleMode.SPECIFICATION)
    trace = events(result)
    event = next(item for item in trace if item.tool == "llm.generate_tests" and item.phase == "tool_finished")
    assert event.result_summary["attempts"] == 2
    assert event.result_summary["format_repairs"] == 1
    assert event.result_summary["response_model"] == "test-model"
    assert event.result_summary["calls"][1]["purpose"] == "format_repair"
    assert result.state.api_calls == 2
    assert result.state.token_usage is None  # first repair attempt's usage is unknown
    metadata_text = result.metadata_file.read_text(encoding="utf-8")
    trace_text = result.trace_file.read_text(encoding="utf-8")
    assert key not in metadata_text + trace_text
    assert "Authorization" not in metadata_text + trace_text
    assert json.loads(metadata_text)["adapter_config"]["max_http_attempts"] == 3


def test_run_ids_are_independent_and_existing_run_trace_is_never_overwritten(task_request):
    first, client = run(task_request.updated(max_rounds=1), [candidate([POS])])
    other_request = task_request.updated(output_dir=task_request.output_dir.with_name("other"), max_rounds=1)
    second, _ = run(other_request, [candidate([POS])])
    assert first.state.run_id != second.state.run_id
    before = first.trace_file.read_bytes()
    with pytest.raises(FileExistsError):
        run(task_request, [])
    assert first.trace_file.read_bytes() == before


def test_trace_write_failure_propagates_without_claiming_success(task_request, monkeypatch):
    original = TraceWriter.record
    def fail_append(self, state, phase, *args, **kwargs):
        if phase == "tool_started":
            raise TraceArtifactError("disk unavailable")
        return original(self, state, phase, *args, **kwargs)
    monkeypatch.setattr(TraceWriter, "record", fail_append)
    client = SequenceClient([candidate([POS])])
    with pytest.raises(TraceArtifactError):
        run_agent(task_request, client=client, oracle_mode=OracleMode.SPECIFICATION)
    assert not client.contexts
    assert not (task_request.output_dir / "summary.json").exists()


def test_prepared_workspace_cannot_bypass_candidate_overwrite_protection(task_request):
    result, _ = run(task_request.updated(max_rounds=1), [candidate([POS])])
    before = (task_request.output_dir / "rounds/01/candidate.json").read_bytes()
    from test_generation import llm_result
    from testgen_agent.tools import analyze_source
    with pytest.raises(FileExistsError):
        stage_initial_candidate(source=analyze_source(task_request.source_path), llm_result=llm_result(candidate([POS])),
                                workspace=task_request.output_dir, prepared_run_id=result.state.run_id)
    assert (task_request.output_dir / "rounds/01/candidate.json").read_bytes() == before


def test_specification_read_failure_is_traced_without_model_calls(task_request):
    task_request = task_request.updated(spec_path=task_request.source_path.with_name("missing.md"))
    result, client = run(task_request, [])
    trace = events(result)
    assert any(item.tool == "read_specification" and item.phase == "tool_failed" for item in trace)
    assert trace[-1].result_summary["status"] == "failed"
    assert not client.contexts


def test_export_failure_records_failed_tool_and_never_finishes_run(task_request, monkeypatch):
    import testgen_agent.agent as agent
    def fail_export(*args):
        raise OSError("export disk unavailable")
    monkeypatch.setattr(agent, "_export_results", fail_export)
    with pytest.raises(OSError):
        run(task_request, [candidate([POS, ZERO, NEG])])
    trace = [TraceEvent.model_validate_json(line) for line in (task_request.output_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert trace[-1].phase == "tool_failed"
    assert trace[-1].tool == "export_results"
    assert not any(item.phase == "run_finished" for item in trace)


def test_trace_writer_wraps_actual_append_io_error(task_request, monkeypatch):
    from pathlib import Path
    original = Path.open
    def broken_open(path, mode="r", *args, **kwargs):
        if path.name == "trace.jsonl" and mode == "a":
            raise OSError("append denied")
        return original(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", broken_open)
    client = SequenceClient([])
    with pytest.raises(TraceArtifactError, match="append"):
        run_agent(task_request, client=client, oracle_mode=OracleMode.SPECIFICATION)
    assert not client.contexts


def test_relative_output_directory_is_supported(task_request, monkeypatch):
    from pathlib import Path
    monkeypatch.chdir(task_request.source_path.parent)
    result, client = run(task_request.updated(output_dir=Path("relative-run"), max_rounds=1), [candidate([POS])])
    assert events(result)[-1].result_summary["stop_reason"] == "round_limit_reached"
    assert result.trace_file.is_file()
