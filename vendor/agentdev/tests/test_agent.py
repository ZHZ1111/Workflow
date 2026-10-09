"""E1 action gates, real local execution and replaceable tool interfaces."""

from pathlib import Path

import pytest

from testgen_agent import models as m
from testgen_agent.agent import (
    AgentTools, apply_decision, build_action_context, choose_action, run_initial_step,
)
from testgen_agent.tools import analyze_source
from test_generation import SyntheticClient, llm_result, relationship_generated, single_function_generated


@pytest.fixture
def source():
    return analyze_source(Path(__file__).parents[1] / "examples" / "relationships.py")


def passed(count=2):
    return m.ExecutionResult(kind="passed", exit_code=0, collected=count, executed=count,
                             passed=count, failed=0, errors=0, skipped=0, xfailed=0, xpassed=0)


def full_coverage(source):
    return m.CoverageResult(target_file=source.source_path, status="available",
                            covered_lines=1, total_lines=1, covered_branches=0, total_branches=0)


def state_for(source, *, generated=None, execution=None, coverage=None, validation_error=None,
              max_rounds=3, test_path=Path("rounds/01/test_subject.py")):
    candidate = m.CandidateRound(number=1, generated=generated or relationship_generated(source),
                                test_path=test_path, validation_error=validation_error,
                                execution=execution, coverage=coverage)
    return m.AgentState(
        schema_version="1.1", run_id="offline", oracle_mode="specification", source_info=source,
        request=m.TaskInput(source_path=source.source_path, max_rounds=max_rounds,
                            model=m.ModelInfo(model="offline-model", base_url="https://example.com")),
        history=(candidate,), current_round=1,
    )


def test_initial_states_and_context(source):
    state = state_for(source)
    data = state.model_dump(round_trip=True)
    data.update(history=(), current_round=0, source_info=None)
    initial = m.AgentState.model_validate(data)
    assert choose_action(initial).action == m.AgentAction.ANALYZE_SOURCE
    data["source_info"] = source
    analyzed = m.AgentState.model_validate(data)
    assert choose_action(analyzed).action == m.AgentAction.GENERATE_TESTS
    assert "controller_decision" in build_action_context(analyzed).user_prompt
    assert choose_action(state).action == m.AgentAction.RUN_TESTS


@pytest.mark.parametrize("kind,code,action,stop", [
    ("collection_error", 2, "request_review", "ambiguous_specification"),
    ("no_tests", 5, "repair_generated_test", None),
    ("timeout", None, "finish", "execution_timeout"),
    ("launch_error", None, "finish", "unrecoverable_error"),
    ("result_error", 0, "finish", "unrecoverable_error"),
    ("internal_error", 3, "finish", "unrecoverable_error"),
])
def test_execution_gates(source, kind, code, action, stop):
    execution = m.ExecutionResult(kind=kind, exit_code=code, timed_out=kind == "timeout")
    state = state_for(source, execution=execution)
    decision = choose_action(state)
    assert decision.action == action
    assert decision.stop_reason == stop
    applied = apply_decision(state, decision)
    assert applied.next_action == action
    if stop:
        with pytest.raises(ValueError, match="does not permit"):
            build_action_context(applied)


def test_assertion_failure_keeps_code_and_requests_review(source):
    execution = m.ExecutionResult(kind="assertion_failed", exit_code=1, collected=2, executed=2,
                                 passed=1, failed=1, errors=0, skipped=0, xfailed=0, xpassed=0)
    state = state_for(source, execution=execution, coverage=full_coverage(source))
    before = state.history[0].generated.code
    decision = choose_action(state)
    assert decision.action == "request_review"
    assert apply_decision(state, decision).history[0].generated.code == before
    with pytest.raises(ValueError):
        build_action_context(state)


def test_validation_repair_has_previous_code_and_error(source):
    state = state_for(source, validation_error="test_syntax: invalid syntax")
    assert choose_action(state).action == "repair_generated_test"
    context = build_action_context(state)
    assert "test_syntax" in context.user_prompt
    assert "previous_generated_tests" in context.user_prompt
    assert "test_collaboration_checkout" in context.user_prompt


def test_coverage_gaps_link_dependency_and_entry(source):
    line = source.functions[0].end_line
    coverage = m.CoverageResult(target_file=source.source_path, status="available",
                               covered_lines=0, total_lines=1, missing_lines=(line,),
                               covered_branches=0, total_branches=0)
    state = state_for(source, execution=passed(), coverage=coverage)
    decision = choose_action(state)
    assert decision.action == "add_missing_cases"
    assert decision.feedback["affected_functions"] == ["calculate_total"]
    assert decision.feedback["caller_chain_functions"] == ["calculate_total", "checkout"]
    assert decision.feedback["direct_call_evidence"][0]["caller"] == "checkout"
    context = build_action_context(state)
    assert "observed_execution" in context.user_prompt
    assert "caller_chain_functions" in context.user_prompt
    assert "prior_strategy" in context.user_prompt


def test_full_coverage_cannot_hide_missing_collaboration(source):
    generated = relationship_generated(source)
    data = generated.model_dump(round_trip=True)
    data["plan"]["scenarios"] = data["plan"]["scenarios"][:1]
    data["mappings"] = data["mappings"][:1]
    data["code"] = "from subject import calculate_total\ndef test_unit_total_zero():\n    assert calculate_total(0, 5) == 0\n"
    generated = m.GeneratedTests.model_validate(data)
    state = state_for(source, generated=generated, execution=passed(1), coverage=full_coverage(source))
    decision = choose_action(state)
    assert decision.action == "add_missing_cases"
    assert decision.feedback["unverified_relationship_ids"] == ["rel-checkout-total"]


def test_full_coverage_cannot_hide_ineffective_assertion(source):
    generated = relationship_generated(source)
    data = generated.model_dump(round_trip=True)
    data["code"] = generated.code.replace("assert checkout(12, 3) == 36", "checkout(12, 3)\n    assert True")
    state = state_for(source, generated=m.GeneratedTests.model_validate(data),
                      execution=passed(), coverage=full_coverage(source))
    assert choose_action(state).action == "repair_generated_test"


def test_finish_and_stopped_state_are_stable(source):
    state = state_for(source, execution=passed(), coverage=full_coverage(source))
    decision = choose_action(state)
    assert decision.stop_reason == "coverage_target_met"
    stopped = apply_decision(state, decision)
    assert choose_action(stopped).stop_reason == decision.stop_reason
    with pytest.raises(ValueError):
        build_action_context(stopped)


@pytest.mark.parametrize("error", [None, "unavailable"])
def test_unknown_coverage_does_not_complete(source, error):
    coverage = None if error is None else m.CoverageResult(target_file=source.source_path,
                                                          status="unavailable", error=error)
    decision = choose_action(state_for(source, execution=passed(), coverage=coverage))
    assert decision.stop_reason == "unrecoverable_error"


def test_round_budget_stops_repair(source):
    state = state_for(source, validation_error="test_syntax", max_rounds=1)
    assert choose_action(state).stop_reason == "round_limit_reached"
    with pytest.raises(ValueError):
        build_action_context(state)


def test_model_questions_do_not_execute_or_repeat(source, tmp_path):
    generated = relationship_generated(source)
    data = generated.model_dump(round_trip=True)
    data.update(code=None, mappings=(), questions=("Which expected total is correct?",))
    client = SyntheticClient(llm_result(m.GeneratedTests.model_validate(data)))
    def forbidden(*args, **kwargs):
        pytest.fail("unresolved candidate must not execute")
    request = state_for(source).request.model_copy(update={"output_dir": tmp_path / "question"})
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION,
                              tools=AgentTools(execute=forbidden))
    assert result.decision.action == "request_review"
    assert len(client.contexts) == 1


def test_controller_real_tools_execute_once_and_preserve_source(source, tmp_path):
    original = source.source_path.read_bytes()
    client = SyntheticClient(llm_result(relationship_generated(source)))
    request = state_for(source).request.model_copy(update={"output_dir": tmp_path / "real"})
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION)
    assert result.state.history[0].execution.all_passed
    assert result.state.history[0].coverage.status == "available"
    assert result.state.best_available_round == 1
    assert len(client.contexts) == result.state.api_calls == 1
    assert [step.action for step in result.steps[:3]] == ["analyze_source", "generate_tests", "run_tests"]
    assert source.source_path.read_bytes() == original
    assert m.AgentState.model_validate_json(result.state.canonical_json()) == result.state


def test_real_branch_gap_changes_next_action(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("def classify(n):\n    if n > 0:\n        return 1\n    return 0\n", encoding="utf-8")
    generated = single_function_generated("classify", code="from subject import classify\ndef test_behavior():\n    assert classify(1) == 1\n")
    client = SyntheticClient(llm_result(generated))
    request = m.TaskInput(source_path=path, output_dir=tmp_path / "gap",
                          model=m.ModelInfo(model="offline-model", base_url="https://example.com"))
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION)
    assert result.state.history[0].execution.all_passed
    assert result.state.history[0].coverage.branch_percent == 50
    assert result.decision.action == "add_missing_cases"
    assert result.decision.feedback["affected_functions"] == ["classify"]
    assert len(client.contexts) == 1


def test_invalid_candidate_never_reaches_execution(source, tmp_path):
    generated = relationship_generated(source)
    data = generated.model_dump(round_trip=True)
    data["code"] = "def broken(:"
    client = SyntheticClient(llm_result(m.GeneratedTests.model_validate(data)))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid candidate must not execute")
    request = state_for(source).request.model_copy(update={"output_dir": tmp_path / "invalid"})
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION,
                              tools=AgentTools(execute=forbidden))
    assert result.decision.action == "repair_generated_test"
    assert result.state.history[0].validation_error
    assert not result.artifacts.ready_for_execution
    assert len(client.contexts) == 1


def test_spec_path_is_read_and_tool_interfaces_are_replaceable(source, tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("明确的离线规格", encoding="utf-8-sig")
    events = []
    def analyze(path):
        events.append("analyze")
        assert path == source.source_path
        return source
    from testgen_agent.generation import generate_initial_candidate
    def generate(**kwargs):
        events.append("generate")
        assert kwargs["behavior_specification"] == "明确的离线规格"
        return generate_initial_candidate(**kwargs)
    from testgen_agent.tools import run_tests_with_coverage
    def execute(workspace, timeout):
        events.append("execute")
        return run_tests_with_coverage(workspace, timeout)
    client = SyntheticClient(llm_result(relationship_generated(source)))
    request = state_for(source).request.model_copy(update={"output_dir": tmp_path / "spec-run", "spec_path": spec})
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION,
                              tools=AgentTools(analyze=analyze, generate=generate, execute=execute))
    assert events == ["analyze", "generate", "execute"]
    assert result.state.behavior_specification == "明确的离线规格"
