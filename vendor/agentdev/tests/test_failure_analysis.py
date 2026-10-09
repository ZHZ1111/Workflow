"""E2 failure causes, oracle grounding and retained defect evidence."""

from pathlib import Path

import pytest

from testgen_agent import models as m
from testgen_agent.agent import AgentTools, apply_decision, build_action_context, choose_action, run_initial_step
from testgen_agent.tools import analyze_source, run_tests_with_coverage
from test_agent import full_coverage, passed, state_for
from test_generation import SyntheticClient, llm_result, relationship_generated, single_function_generated


@pytest.fixture
def source():
    return analyze_source(Path(__file__).parents[1] / "examples" / "relationships.py")


def failure_execution(kind="assertion_failed", *, exception="AssertionError", origin="test_subject.py", module=None, line=4):
    return m.ExecutionResult(
        kind=kind, exit_code=1, collected=1, executed=1, passed=0,
        failed=1 if kind == "assertion_failed" else 0,
        errors=1 if kind == "execution_error" else 0, skipped=0, xfailed=0, xpassed=0,
        failures=(m.FailureObservation(nodeid="rounds/01/test_subject.py::test_behavior", phase="call",
                                       exception_type=exception, path=origin, line=line, missing_module=module),),
    )


def discount_state(*, spec=None, expected=90, line=4, args=(100,)):
    root = Path(__file__).parents[1]
    source = analyze_source(root / "examples" / "discount.py")
    generated = single_function_generated("calculate_discount", code=(
        "from subject import calculate_discount\n\n"
        "def test_behavior():\n"
        f"    assert calculate_discount(100) == {expected}\n"
    ))
    scenario = generated.plan.scenarios[0].updated(inputs=m.ScenarioInput(args=args))
    generated = generated.updated(plan=generated.plan.updated(scenarios=(scenario,)))
    state = m.AgentState(
        schema_version="1.1", run_id="discount-offline", oracle_mode="specification", source_info=source,
        behavior_specification=spec if spec is not None else (root / "examples" / "discount.md").read_text(encoding="utf-8"),
        request=m.TaskInput(source_path=source.source_path,
                            model=m.ModelInfo(model="offline-model", base_url="https://example.com")),
        history=(m.CandidateRound(number=1, generated=generated, test_path=Path("rounds/01/test_subject.py"),
                                 execution=failure_execution(line=line)),), current_round=1,
    )
    return state


def test_literal_spec_failure_reports_suspected_defect_and_retains_evidence():
    state = discount_state()
    decision = choose_action(state)
    assert decision.action == "request_review"
    assert decision.status == "needs_review"
    assert decision.stop_reason == "suspected_source_defect"
    assert decision.issues[0].scenario_id == "calculate_discount-normal"
    assert "90" in decision.issues[0].evidence[-1]
    applied = apply_decision(state, decision)
    assert applied.history == state.history
    assert applied.issues == decision.issues
    assert apply_decision(applied, decision).issues == applied.issues
    assert m.AgentState.model_validate_json(applied.canonical_json()) == applied
    with pytest.raises(ValueError, match="does not permit"):
        build_action_context(applied)


@pytest.mark.parametrize("spec,expected,args,line", [
    ("满100打折，具体取整方式不确定。", 90, (100,), 4),
    ("| 输入 | 预期 |\n|---|---|\n|100|90|\n|100|100|", 90, (100,), 4),
    ("| 输入 | 预期 |\n|---|---|\n|100|100|", 90, (100,), 4),
    ("| 输入 | 预期 |\n|---|---|\n|100|90|", 91, (100,), 4),
    ("| 输入 | 预期 |\n|---|---|\n|100|90|", 90, (99,), 4),
    ("| 输入 | 预期 |\n|---|---|\n|100|90|", 90, (100,), 3),
    ("| 输入 | 预期 |\n|---|---|\n|True|90|", 90, (100,), 4),
])
def test_insufficient_conflicting_or_mismapped_oracles_require_review(spec, expected, args, line):
    decision = choose_action(discount_state(spec=spec, expected=expected, args=args, line=line))
    assert decision.stop_reason == "ambiguous_specification"
    assert decision.status == "needs_review"
    assert not any(issue.kind == "suspected_source_defect" for issue in decision.issues)


def test_generated_oracle_label_alone_does_not_prove_bug():
    state = discount_state(spec="No explicit input/expected oracle.")
    assert state.history[0].generated.plan.scenarios[0].expected_basis.kind == "specification"
    assert choose_action(state).stop_reason == "ambiguous_specification"


def test_exact_prose_quote_supports_suspected_defect_without_a_table():
    spec = "At exactly 100, return 90."
    state = discount_state(spec=spec)
    candidate = state.history[0]
    scenario = candidate.generated.plan.scenarios[0].updated(
        expected_basis=m.ExpectedBasis(kind="specification", detail=spec))
    generated = candidate.generated.updated(plan=candidate.generated.plan.updated(scenarios=(scenario,)))
    state = state.updated(history=(candidate.updated(generated=generated),))
    decision = choose_action(state)
    assert decision.stop_reason == "suspected_source_defect"
    assert decision.issues[0].evidence[-1] == f"规格原文：{spec}"


@pytest.mark.parametrize("exception,origin,module,action,issue", [
    ("NameError", "test_subject.py", None, "repair_generated_test", "generation_error"),
    ("ImportError", "test_subject.py", None, "repair_generated_test", "generation_error"),
    ("NameError", "subject.py", None, "request_review", "ambiguous_specification"),
    ("ValueError", "subject.py", None, "request_review", "ambiguous_specification"),
    ("ModuleNotFoundError", "subject.py", "external_lib", "finish", "environment_error"),
    ("ModuleNotFoundError", "test_subject.py", "subject", "repair_generated_test", "generation_error"),
])
def test_observed_failure_origin_selects_action(source, exception, origin, module, action, issue):
    execution = failure_execution("execution_error", exception=exception, origin=origin, module=module)
    state = state_for(source, execution=execution)
    decision = choose_action(state)
    assert decision.action == action
    assert decision.issues[0].kind == issue
    if action == "repair_generated_test":
        assert "generation_error" in build_action_context(state).user_prompt
    else:
        with pytest.raises(ValueError):
            build_action_context(state)


def test_unsupported_dependency_fails_without_execution_or_install(source, tmp_path):
    generated = relationship_generated(source)
    generated = generated.updated(code="import not_installed_lib\n" + generated.code)
    client = SyntheticClient(llm_result(generated))
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported dependency must not execute")
    request = state_for(source).request.updated(output_dir=tmp_path / "dependency")
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION,
                              tools=AgentTools(execute=forbidden))
    assert result.decision.status == "failed"
    assert result.state.issues[0].kind == "environment_error"
    assert len(client.contexts) == 1


def test_same_invalid_candidate_stops_instead_of_repairing_again(source):
    state = state_for(source, validation_error="test_syntax: invalid")
    state = state.updated(history=(*state.history, state.history[0].updated(number=2)), current_round=2)
    assert choose_action(state).stop_reason == "no_progress"
    with pytest.raises(ValueError):
        build_action_context(state)


def test_same_passing_candidate_with_coverage_gap_stops(source):
    coverage = m.CoverageResult(target_file=source.source_path, status="available",
                               covered_lines=0, total_lines=1, covered_branches=0, total_branches=0,
                               missing_lines=(source.functions[0].end_line,))
    state = state_for(source, execution=passed(), coverage=coverage)
    state = state.updated(history=(*state.history, state.history[0].updated(number=2)), current_round=2)
    decision = choose_action(state)
    assert decision.stop_reason == "no_progress"
    assert decision.status == "partial"


def test_changed_or_improved_candidate_is_not_rejected_as_repetition(source):
    state = state_for(source, execution=passed(), coverage=full_coverage(source))
    state = state.updated(history=(*state.history, state.history[0].updated(number=2)), current_round=2)
    assert choose_action(state).stop_reason == "coverage_target_met"


def test_explicit_specification_has_priority_over_conflicting_docstring():
    state = discount_state()
    function = state.source_info.functions[0].updated(docstring="| 输入 | 预期 |\n|---|---|\n|100|100|")
    state = state.updated(source_info=state.source_info.updated(functions=(function,)))
    assert choose_action(state).stop_reason == "suspected_source_defect"


def test_characterization_failure_never_claims_specification_bug():
    state = discount_state()
    candidate = state.history[0]
    scenario = candidate.generated.plan.scenarios[0].updated(
        expected_basis=m.ExpectedBasis(kind="implementation_assumption", detail="observed implementation"))
    generated = candidate.generated.updated(plan=candidate.generated.plan.updated(
        oracle_mode="characterization", scenarios=(scenario,)))
    state = state.updated(oracle_mode="characterization", history=(candidate.updated(generated=generated),))
    assert choose_action(state).stop_reason == "ambiguous_specification"


def test_old_execution_snapshots_without_failure_details_remain_readable():
    execution = m.ExecutionResult.model_validate({
        "kind": "assertion_failed", "exit_code": 1, "collected": 1, "executed": 1,
        "passed": 0, "failed": 1, "errors": 0, "skipped": 0, "xfailed": 0, "xpassed": 0,
    })
    assert execution.failures == ()


def test_failure_details_cannot_be_attached_to_success():
    with pytest.raises(ValueError, match="cannot contain failure observations"):
        passed(1).updated(failures=failure_execution().failures)


def test_changed_candidate_with_same_coverage_stops_without_progress(source):
    coverage = m.CoverageResult(target_file=source.source_path, status="available",
                               covered_lines=0, total_lines=1, covered_branches=0, total_branches=0,
                               missing_lines=(source.functions[0].end_line,))
    state = state_for(source, execution=passed(), coverage=coverage)
    previous = state.history[0]
    generated = previous.generated.updated(code=previous.generated.code + "\n# different candidate\n")
    state = state.updated(history=(previous, previous.updated(number=2, generated=generated)), current_round=2)
    assert choose_action(state).stop_reason == "no_progress"


def test_changed_exception_is_not_identical_measured_outcome(source):
    previous = failure_execution("execution_error", exception="NameError")
    current = failure_execution("execution_error", exception="ImportError")
    state = state_for(source, execution=previous)
    state = state.updated(history=(state.history[0], state.history[0].updated(number=2, execution=current)), current_round=2)
    assert choose_action(state).action == "repair_generated_test"


def test_real_discount_failure_preserves_expected_90_and_source(tmp_path):
    state = discount_state()
    source_bytes = state.request.source_path.read_bytes()
    client = SyntheticClient(llm_result(state.history[0].generated))
    request = state.request.updated(output_dir=tmp_path / "discount")
    result = run_initial_step(request, client=client, oracle_mode=m.OracleMode.SPECIFICATION,
                              behavior_specification=state.behavior_specification)
    assert result.state.history[0].execution.kind == "assertion_failed"
    failure = result.state.history[0].execution.failures[0]
    assert failure.exception_type == "AssertionError"
    assert failure.line == 4
    assert result.decision.stop_reason == "suspected_source_defect"
    assert "== 90" in result.artifacts.test_path.read_text(encoding="utf-8")
    assert state.request.source_path.read_bytes() == source_bytes
    assert len(client.contexts) == 1


@pytest.mark.parametrize("code,exception,module", [
    ("from absent_e2_library import value\n", "ModuleNotFoundError", "absent_e2_library"),
    ("from subject import missing_function\n", "ImportError", None),
    ("from subject import f\ndef test_name():\n    f(unknown_name)\n", "NameError", None),
])
def test_real_runner_retains_structured_collection_and_call_failures(tmp_path, code, exception, module):
    (tmp_path / "subject.py").write_text("def f(n):\n    return n\n", encoding="utf-8")
    directory = tmp_path / "rounds" / "01"
    directory.mkdir(parents=True)
    (directory / "test_subject.py").write_text(code, encoding="utf-8")
    run = run_tests_with_coverage(tmp_path)
    assert run.execution.failures[0].exception_type == exception
    assert run.execution.failures[0].missing_module == module
    assert Path(run.execution.failures[0].path).name == "test_subject.py"
    assert m.ExecutionResult.model_validate_json(run.execution.canonical_json()) == run.execution
