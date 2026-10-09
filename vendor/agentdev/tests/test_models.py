"""B1 contracts: reject false success, mixed provenance and inconsistent history."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from testgen_agent import models as m
from testgen_agent.config import load_config


@pytest.fixture
def request_info(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = load_config(environ={"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "model-test-secret"})
    return m.TaskInput(source_path=Path("examples/calculation.py"), model=m.ModelInfo.from_config(config))


@pytest.fixture
def source_info(request_info):
    return m.SourceInfo(
        source_path=request_info.source_path,
        source_code='def calculate_total(unit_price, quantity):\n    """计算总价。"""\n    return unit_price * quantity\n',
        functions=(m.FunctionInfo(
            name="calculate_total", signature="(unit_price, quantity)",
            start_line=1, end_line=3, docstring="计算总价。",
        ),),
    )


@pytest.fixture
def generated():
    plan = m.TestPlan(
        target_functions=("calculate_total",), oracle_mode="specification",
        scenarios=(m.TestScenario(
            scenario_id="normal-1", target_function="calculate_total", category="normal",
            inputs=m.ScenarioInput(args=(12, 3)), expected_behavior="返回整数 36",
            expected_basis=m.ExpectedBasis(kind="specification", detail="单价乘以数量"),
        ),),
    )
    return m.GeneratedTests(
        plan=plan,
        code="from subject import calculate_total\n\ndef test_total():\n    assert calculate_total(12, 3) == 36\n",
        mappings=(m.TestCaseMapping(test_function="test_total", scenario_ids=("normal-1",)),),
    )


@pytest.fixture
def execution():
    return m.ExecutionResult(
        kind="passed", exit_code=0, collected=1, executed=1, passed=1, failed=0,
        errors=0, skipped=0, xfailed=0, xpassed=0, stdout="测试通过\n", duration_seconds=0.25,
    )


@pytest.fixture
def coverage():
    return m.CoverageResult(
        target_file=Path("subject.py"), status="available", covered_lines=2, total_lines=2,
        covered_branches=0, total_branches=0,
    )


@pytest.fixture
def candidate(generated, execution, coverage):
    return m.CandidateRound(
        number=1, generated=generated, test_path=Path("rounds/01/test_subject.py"),
        execution=execution, coverage=coverage,
    )


def make_report(request_info, candidate, **changes):
    values = dict(
        run_id="synthetic-contract-test", request=request_info, oracle_mode="specification",
        status="completed", stop_reason="coverage_target_met", rounds=(candidate,),
        selected_round=1, final_test_file=Path("outputs/test_subject.py"),
        trace_file=Path("outputs/trace.jsonl"), api_calls=1,
    )
    return m.FinalReport(**{**values, **changes})


def test_seven_contracts_have_json_schemas_and_canonical_round_trips(request_info, source_info, generated, candidate):
    state = m.AgentState(
        run_id="synthetic-contract-test", request=request_info, oracle_mode="specification",
        source_info=source_info, behavior_specification="总价为单价乘以数量",
        current_round=1, history=(candidate,), best_available_round=1,
        next_action="finish", stop_reason="coverage_target_met",
    )
    values = [source_info, generated.plan, generated, candidate.execution, candidate.coverage,
              state, make_report(request_info, candidate)]
    for value in values:
        assert type(value).model_validate_json(value.canonical_json()) == value
        for mode in ("validation", "serialization"):
            schema = type(value).model_json_schema(mode=mode)
            assert schema["additionalProperties"] is False
    report = values[-1].model_dump(mode="json")
    assert report["metrics"]["line_percent"] == 100
    assert report["metrics"]["branch_percent"] is None
    assert "model-test-secret" not in json.dumps(report)
    assert "api_key" not in m.ModelInfo.model_fields
    assert "计算总价" in source_info.canonical_json()


@pytest.mark.parametrize("key", ["execution", "coverage", "passed", "line_percent", "output_path", "status", "api_calls"])
def test_llm_payload_rejects_host_paths_and_measured_values(generated, key):
    with pytest.raises(ValidationError):
        m.GeneratedTests.model_validate({**generated.model_dump(round_trip=True), key: 100})


def test_nested_llm_payload_also_rejects_tool_metrics(generated):
    data = generated.model_dump(round_trip=True)
    data["plan"]["scenarios"][0]["inputs"]["passed"] = 99
    with pytest.raises(ValidationError):
        m.GeneratedTests.model_validate(data)


@pytest.mark.parametrize("changes", [
    {"mappings": ()},
    {"mappings": ({"test_function": "test_total", "scenario_ids": ("unknown",)},)},
    {"mappings": ({"test_function": "check_total", "scenario_ids": ("normal-1",)},)},
    {"code": " \n"},
    {"code": None, "mappings": (), "questions": ()},
])
def test_invalid_generated_payloads_are_rejected(generated, changes):
    with pytest.raises(ValidationError):
        generated.updated(**changes)


def test_ambiguity_can_be_returned_without_fabricating_code(generated):
    response = generated.updated(
        plan=generated.plan.updated(scenarios=()), code=None, mappings=(), questions=("请确认零数量的约定。",),
    )
    assert response.code is None and response.questions


def test_duplicate_or_unknown_plan_references_are_rejected(generated):
    scenario = generated.plan.scenarios[0]
    for changes in (
        {"target_functions": ("calculate_total", "calculate_total")},
        {"scenarios": (scenario, scenario)},
        {"scenarios": (scenario.updated(target_function="unknown"),)},
        {"oracle_mode": "characterization"},
    ):
        with pytest.raises(ValidationError):
            generated.plan.updated(**changes)


@pytest.mark.parametrize("value", [-1, True, "1", 1.0])
def test_counters_are_nonnegative_strict_integers(execution, value):
    with pytest.raises(ValidationError):
        execution.updated(passed=value)


@pytest.mark.parametrize("changes", [
    {"exit_code": 1}, {"failed": 1}, {"executed": 2}, {"collected": 2},
    {"timed_out": True}, {"duration_seconds": float("nan")},
    {"runnable": True}, {"all_passed": True},
    {"kind": "launch_error", "exit_code": None},
])
def test_execution_counts_codes_and_derived_flags_cannot_contradict(execution, changes):
    with pytest.raises(ValidationError):
        execution.updated(**changes)


def test_assertion_failure_is_runnable_and_preserves_failure(execution):
    result = execution.updated(kind="assertion_failed", exit_code=1, passed=0, failed=1)
    assert result.runnable and not result.all_passed


@pytest.mark.parametrize("kind,extra", [
    ("no_tests", {"exit_code": 5, "collected": 0, "executed": 0}),
    ("timeout", {"exit_code": None, "timed_out": True}),
    ("collection_error", {"exit_code": 2, "errors": 1}),
    ("launch_error", {"exit_code": None}),
])
def test_unavailable_executions_never_become_all_passed(kind, extra):
    result = m.ExecutionResult(kind=kind, **extra)
    assert not result.runnable and not result.all_passed
    assert result.passed is None


@pytest.mark.parametrize("outcome", ["skipped", "xfailed", "xpassed"])
def test_only_bypassed_tests_are_not_available(execution, outcome):
    result = execution.updated(passed=0, executed=0 if outcome == "skipped" else 1, **{outcome: 1})
    assert not result.runnable and not result.all_passed


def test_mixed_skip_run_is_partial_not_all_passed(execution):
    result = execution.updated(collected=2, skipped=1)
    assert result.runnable and not result.all_passed


@pytest.mark.parametrize("changes", [
    {"covered_lines": 3}, {"total_lines": 3}, {"covered_branches": 1},
    {"line_percent": 100}, {"status": "unavailable", "error": "导出失败"},
    {"missing_lines": (9, 9)},
])
def test_coverage_counters_missing_data_and_percentages_are_consistent(coverage, changes):
    with pytest.raises(ValidationError):
        coverage.updated(**changes)


def test_zero_denominators_and_missing_coverage_are_explicit():
    empty = m.CoverageResult(
        target_file="subject.py", status="available", covered_lines=0, total_lines=0,
        covered_branches=0, total_branches=0,
    )
    unavailable = m.CoverageResult(target_file="subject.py", status="unavailable", error="工具导出失败")
    for result in (empty, unavailable):
        assert result.line_percent is result.branch_percent is None
        assert not result.meets_target


def test_missing_lines_are_source_locations_and_branch_exits_can_be_negative():
    result = m.CoverageResult(
        target_file="subject.py", status="available", covered_lines=1, total_lines=2,
        covered_branches=1, total_branches=2, missing_lines=(100,),
        missing_branches=(m.BranchArc(start=99, end=-3),),
    )
    assert result.line_percent == result.branch_percent == 50
    with pytest.raises(ValidationError):
        m.BranchArc(start=0, end=-3)


def test_source_metadata_rejects_duplicate_and_invalid_locations(source_info):
    with pytest.raises(ValidationError):
        source_info.updated(functions=source_info.functions * 2)
    with pytest.raises(ValidationError):
        source_info.updated(functions=(source_info.functions[0].updated(end_line=4),))
    with pytest.raises(ValidationError):
        source_info.functions[0].updated(start_line=3, end_line=1)


def test_previous_available_candidate_survives_invalid_next_round(request_info, source_info, candidate):
    invalid = m.CandidateRound(
        number=2, generated=candidate.generated.updated(code="def test_total(:\n"),
        test_path="rounds/02/test_subject.py", validation_error="测试语法错误",
    )
    issue = m.Issue(kind="generation_error", summary="追加候选无效", round_number=2)
    state = m.AgentState(
        run_id="synthetic-contract-test", request=request_info, oracle_mode="specification",
        source_info=source_info, current_round=2, history=(candidate, invalid),
        best_available_round=1, next_action="finish", stop_reason="round_limit_reached", issues=(issue,),
    )
    report = make_report(
        request_info, candidate, status="partial", stop_reason=state.stop_reason,
        rounds=state.history, unresolved_issues=state.issues,
    )
    assert state.best_available_round == report.selected_round == 1
    assert report.rounds[1].validation_error == "测试语法错误"
    assert report.metrics.executed_rounds == 1
    assert report.metrics.all_passed
    with pytest.raises(ValidationError):
        state.updated(best_available_round=2)


def test_state_rejects_round_budget_mode_and_source_reference_mismatches(request_info, source_info, candidate):
    state = m.AgentState(
        run_id="synthetic-contract-test", request=request_info, oracle_mode="specification",
        source_info=source_info, current_round=1, history=(candidate,), best_available_round=1,
    )
    for changes in (
        {"current_round": 2}, {"history": (candidate.updated(number=2),)},
        {"best_available_round": 2}, {"oracle_mode": "characterization"},
        {"source_info": source_info.updated(source_path="different.py")},
        {"stop_reason": "round_limit_reached", "next_action": "generate_tests"},
        {"request": request_info.updated(max_rounds=1), "history": (candidate, candidate.updated(number=2)), "current_round": 2},
    ):
        with pytest.raises(ValidationError):
            state.updated(**changes)


@pytest.mark.parametrize("changes", [
    {"trace_file": None}, {"selected_round": None}, {"final_test_file": None},
    {"status": "failed"}, {"status": "needs_review"},
    {"stop_reason": "round_limit_reached"},
    {"unresolved_issues": ({"kind": "environment_error", "summary": "报告写入失败"},)},
])
def test_report_cannot_claim_completion_with_missing_or_conflicting_evidence(request_info, candidate, changes):
    with pytest.raises(ValidationError):
        make_report(request_info, candidate, **changes)


def test_passing_tests_without_coverage_do_not_complete(request_info, candidate):
    with pytest.raises(ValidationError):
        make_report(request_info, candidate.updated(coverage=None))
    report = make_report(
        request_info, candidate.updated(coverage=None), status="partial", stop_reason="round_limit_reached",
    )
    assert report.metrics.all_passed and report.metrics.line_percent is None


def test_specification_failure_requires_review_and_remains_in_report(request_info, candidate):
    failing = candidate.updated(execution=candidate.execution.updated(
        kind="assertion_failed", exit_code=1, passed=0, failed=1,
    ))
    issue = m.Issue(
        kind="suspected_source_defect", summary="规格断言失败", round_number=1,
        scenario_id="normal-1", evidence=("规格预期 36，工具实际返回 37（合成测试数据）",),
    )
    report = make_report(
        request_info, failing, status="needs_review", stop_reason="suspected_source_defect",
        unresolved_issues=(issue,),
    )
    assert report.rounds[0].execution.failed == 1
    assert not report.metrics.all_passed
    with pytest.raises(ValidationError):
        report.updated(status="partial")


def test_review_before_execution_and_failure_without_candidates(request_info):
    review = m.FinalReport(
        run_id="synthetic-contract-test", request=request_info, oracle_mode="specification",
        status="needs_review", stop_reason="ambiguous_specification",
        unresolved_issues=(m.Issue(kind="ambiguous_specification", summary="规格与 docstring 冲突"),),
    )
    failed = m.FinalReport(
        run_id="synthetic-contract-test", request=request_info, oracle_mode="specification",
        status="failed", stop_reason="unrecoverable_error",
    )
    assert review.final_test_file is failed.final_test_file is None
    assert not review.metrics.runnable and not failed.metrics.runnable


def test_host_usage_and_model_info_reject_invented_values(request_info):
    with pytest.raises(ValidationError):
        m.TokenUsage(prompt_tokens=12, completion_tokens=1, total_tokens=100)
    with pytest.raises(ValidationError):
        request_info.model.updated(api_key="model-test-secret")
    assert m.TokenUsage().total_tokens is None
