"""E3: real bounded feedback execution with deterministic offline responses."""

import json
from pathlib import Path

import pytest

from testgen_agent import models as m
from testgen_agent.agent import AgentTools, run_agent, select_best_candidate
from testgen_agent.generation import stage_revision_candidate
from testgen_agent.llm_client import LLMError, LLMErrorCode
from testgen_agent.tools import analyze_source, run_tests, run_tests_with_coverage
from test_generation import llm_result, relationship_generated, single_function_generated
from test_failure_analysis import discount_state


class SequenceClient:
    def __init__(self, generated):
        self.responses = iter(generated)
        self.contexts = []

    def generate_tests(self, context):
        self.contexts.append(context)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return llm_result(response)


def candidate(cases, *, code=None):
    first = single_function_generated("choose", code="from subject import choose\ndef test_positive():\n    assert choose(1) == 1\n")
    scenarios, mappings, functions = [], [], []
    for name, value, expected in cases:
        scenarios.append(first.plan.scenarios[0].updated(
            scenario_id=name, inputs=m.ScenarioInput(args=(value,)), expected_behavior=f"returns {expected}"))
        mappings.append(m.TestCaseMapping(test_function=f"test_{name}", scenario_ids=(name,)))
        functions.append(f"def test_{name}():\n    assert choose({value}) == {expected}\n")
    return first.updated(plan=first.plan.updated(scenarios=tuple(scenarios)), mappings=tuple(mappings),
                         code=code if code is not None else "from subject import choose\n" + "\n".join(functions))


POS = ("positive", 1, 1)
ZERO = ("zero", 0, 0)
NEG = ("negative", -1, -1)


@pytest.fixture
def task_request(tmp_path):
    path = tmp_path / "source.py"
    path.write_text("def choose(n):\n    if n > 0:\n        return 1\n    if n == 0:\n        return 0\n    return -1\n", encoding="utf-8")
    return m.TaskInput(source_path=path, output_dir=tmp_path / "run",
                       model=m.ModelInfo(model="offline-model", base_url="https://example.com"))


def run(task_request, responses, **kwargs):
    client = SequenceClient(responses)
    return run_agent(task_request, client=client, oracle_mode=m.OracleMode.SPECIFICATION, **kwargs), client


def test_three_round_real_loop_finishes_and_preserves_all_snapshots(task_request):
    original = task_request.source_path.read_bytes()
    result, client = run(task_request, [candidate([POS]), candidate([POS, ZERO]), candidate([POS, ZERO, NEG])],
                         behavior_specification="positive -> 1; zero -> 0; negative -> -1")
    assert result.decision.status == "completed"
    assert result.state.stop_reason == "coverage_target_met"
    assert result.state.current_round == result.state.api_calls == len(client.contexts) == 3
    assert result.state.best_available_round == 3
    assert result.state.history[-1].coverage.branch_percent == 100
    assert task_request.source_path.read_bytes() == original
    for number in range(1, 4):
        directory = task_request.output_dir / "rounds" / f"{number:02d}"
        assert (directory / "test_subject.py").is_file()
        saved = m.CandidateRound.model_validate_json((directory / "candidate.json").read_text(encoding="utf-8"))
        assert saved == result.state.history[number - 1]
        assert list((directory / "executions").glob("run-*/coverage.json"))
    assert m.AgentState.model_validate_json(result.state_file.read_text(encoding="utf-8")) == result.state
    summary = json.loads(result.summary_file.read_text(encoding="utf-8"))
    assert summary["stop_reason"] == "coverage_target_met"
    assert summary["executed_rounds"] == 3
    assert summary["token_usage"] is None
    assert summary["conclusion"]["level"] == "passed"
    assert summary["metrics"]["passed"] == 3
    assert summary["metrics"]["line_percent"] == 100
    assert summary["scope"]["planned_scenarios"] == 3
    report = result.report_file.read_text(encoding="utf-8")
    assert "最终结论：通过" in report
    assert "## 核心数据" in report
    assert "## 数据结论" in report
    assert "positive" in report and "negative" in report
    assert "test_negative" in result.final_test_file.read_text(encoding="utf-8")
    assert "protected_test_functions" in client.contexts[2].user_prompt
    assert '"round": 1' in client.contexts[2].user_prompt
    assert '"round": 2' in client.contexts[2].user_prompt
    assert '"remaining_rounds_including_requested": 1' in client.contexts[2].user_prompt
    assert "positive -> 1" in client.contexts[2].user_prompt


def test_report_exposes_relationship_evidence_and_strategy(tmp_path):
    source_path = Path(__file__).parents[1] / "examples" / "relationships.py"
    source = analyze_source(source_path)
    request = m.TaskInput(
        source_path=source_path,
        output_dir=tmp_path / "relationship-run",
        max_rounds=1,
        model=m.ModelInfo(model="offline-model", base_url="https://example.com"),
    )
    result = run_agent(
        request,
        client=SequenceClient([relationship_generated(source)]),
        oracle_mode=m.OracleMode.SPECIFICATION,
        behavior_specification="checkout returns calculate_total's result",
    )

    summary = json.loads(result.summary_file.read_text(encoding="utf-8"))
    assert summary["scope"]["entry_functions"] == ["checkout"]
    assert summary["scope"]["relationships"][0]["evidence"][0]["call_site_ids"] == [
        source.direct_calls[0].call_site_id
    ]
    assert summary["scope"]["strategy"][-1]["decision"] == "collaboration_test"
    report = result.report_file.read_text(encoding="utf-8")
    assert "## 函数关联与测试策略" in report
    assert "rel-checkout-total" in report
    assert "checkout directly calls calculate_total" in report
    assert "collaboration_test" in report


@pytest.mark.parametrize("limit", [1, 2])
def test_budget_limit_does_not_request_another_candidate(task_request, limit):
    result, client = run(task_request.updated(max_rounds=limit), [candidate([POS]), candidate([POS, ZERO])])
    assert result.state.stop_reason == "round_limit_reached"
    assert result.state.current_round == len(client.contexts) == limit
    assert result.decision.status == "partial"
    assert result.final_test_file.is_file()


def test_invalid_added_candidate_keeps_previous_and_retains_unresolved_error(task_request):
    invalid = candidate([POS, ZERO], code="def invalid(:")
    result, client = run(task_request.updated(max_rounds=2), [candidate([POS]), invalid])
    assert result.state.best_available_round == 1
    assert result.state.history[1].validation_error.startswith("test_syntax")
    assert result.state.history[1].execution is None
    assert result.state.issues[0].kind == "generation_error"
    assert "test_positive" in result.final_test_file.read_text(encoding="utf-8")
    assert not (task_request.output_dir / "rounds/02/test_subject.py").exists()
    assert len(client.contexts) == 2


@pytest.mark.parametrize("changed", [
    candidate([ZERO, NEG]),
    candidate([POS, ZERO], code="from subject import choose\ndef test_positive():\n    assert choose(2) == 1\ndef test_zero():\n    assert choose(0) == 0\n"),
])
def test_host_rejects_deleted_or_changed_passing_test(task_request, changed):
    result, client = run(task_request.updated(max_rounds=2), [candidate([POS]), changed])
    assert result.state.history[1].validation_error.startswith("preserve_tests")
    assert result.state.history[1].execution is None
    assert result.state.best_available_round == 1


def test_duplicate_with_comment_changes_stops_before_second_execution(task_request):
    first = candidate([POS])
    result, client = run(task_request, [first, first.updated(code=first.code + "\n# cosmetic change\n")])
    assert result.state.stop_reason == "no_progress"
    assert len(client.contexts) == 2
    assert sum(item.execution is not None for item in result.state.history) == 1
    assert result.state.best_available_round == 1


def test_new_tests_without_coverage_increase_stop(task_request):
    more_positive = ("another_positive", 2, 1)
    result, client = run(task_request, [candidate([POS]), candidate([POS, more_positive])])
    assert result.state.stop_reason == "no_progress"
    assert len(client.contexts) == 2
    assert result.state.history[1].execution.all_passed


def test_generation_error_repaired_then_extended_with_history(task_request):
    invalid = candidate([POS], code="def invalid(:")
    result, client = run(task_request, [invalid, candidate([POS]), candidate([POS, ZERO, NEG])])
    assert result.state.stop_reason == "coverage_target_met"
    assert result.state.history[0].validation_error
    assert "test_syntax" in client.contexts[2].user_prompt
    assert result.state.issues == ()
    assert result.state.best_available_round == 3


def test_llm_failure_retains_best_and_counts_failed_requests(task_request):
    error = LLMError(LLMErrorCode.TIMEOUT, "safe timeout", retryable=True, attempts=3)
    result, client = run(task_request, [candidate([POS]), error])
    assert result.state.stop_reason == "unrecoverable_error"
    assert result.state.api_calls == 4
    assert result.state.current_round == 1
    assert result.state.best_available_round == 1
    assert result.decision.status == "partial"
    assert result.final_test_file.is_file()


def test_first_llm_failure_saves_failed_summary_without_fake_candidate(task_request):
    error = LLMError(LLMErrorCode.PERMISSION, "safe permission", retryable=False, attempts=1)
    result, client = run(task_request, [error])
    assert result.decision.status == "failed"
    assert result.state.history == ()
    assert result.final_test_file is None
    assert result.state.api_calls == 1
    assert result.summary_file.is_file()
    assert "未完成" in result.report_file.read_text(encoding="utf-8")


def test_real_defect_stops_once_and_keeps_failing_candidate(task_request):
    state = discount_state()
    task_request = state.request.updated(output_dir=task_request.output_dir)
    result, client = run(task_request, [state.history[0].generated], behavior_specification=state.behavior_specification)
    assert result.state.stop_reason == "suspected_source_defect"
    assert result.decision.status == "needs_review"
    assert result.state.best_available_round == 1
    assert "== 90" in result.final_test_file.read_text(encoding="utf-8")
    assert len(client.contexts) == 1


def test_questions_after_passing_round_keep_best_and_stop(task_request):
    first = candidate([POS])
    unclear = first.updated(code=None, mappings=(), questions=("Which zero behavior is intended?",))
    result, client = run(task_request, [first, unclear])
    assert result.decision.status == "needs_review"
    assert result.state.stop_reason == "ambiguous_specification"
    assert result.state.best_available_round == 1
    assert len(client.contexts) == 2


def test_existing_run_is_rejected_before_llm_calls(task_request):
    task_request.output_dir.mkdir()
    client = SequenceClient([])
    with pytest.raises(FileExistsError):
        run_agent(task_request, client=client, oracle_mode=m.OracleMode.SPECIFICATION)
    assert client.contexts == []


@pytest.mark.parametrize("number", [True, 0, 4, "2"])
def test_tools_reject_invalid_round_selectors(tmp_path, number):
    with pytest.raises((TypeError, ValueError)):
        run_tests_with_coverage(tmp_path, round_number=number)


def test_round_two_tools_execute_only_round_two(tmp_path):
    (tmp_path / "subject.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    for number, code in [(1, "def test_old():\n    assert False\n"), (2, "from subject import f\ndef test_new():\n    assert f() == 1\n")]:
        directory = tmp_path / "rounds" / f"{number:02d}"
        directory.mkdir(parents=True)
        (directory / "test_subject.py").write_text(code, encoding="utf-8")
    assert run_tests(tmp_path, round_number=2).all_passed
    result = run_tests_with_coverage(tmp_path, round_number=2)
    assert result.execution.all_passed
    assert "02" in result.artifacts.parts


def test_added_defect_evidence_is_kept_even_when_earlier_passing_candidate_selected(task_request):
    bad = ("negative", -1, 99)
    result, client = run(task_request, [candidate([POS]), candidate([POS, bad])],
                         behavior_specification="| 输入 | 预期 |\n|---|---|\n|-1|99|")
    assert result.decision.status == "needs_review"
    assert result.state.stop_reason == "suspected_source_defect"
    assert result.state.best_available_round == 1
    assert result.state.history[1].execution.failed == 1
    assert any(issue.kind == "suspected_source_defect" and issue.round_number == 2 for issue in result.state.issues)
    assert "== 99" in (task_request.output_dir / "rounds/02/test_subject.py").read_text(encoding="utf-8")
    assert "== 99" not in result.final_test_file.read_text(encoding="utf-8")
    assert len(client.contexts) == 2


def test_changed_passing_scenario_metadata_is_rejected(task_request):
    first = candidate([POS])
    second = candidate([POS, ZERO])
    scenario = second.plan.scenarios[0].updated(expected_behavior="different expectation")
    second = second.updated(plan=second.plan.updated(scenarios=(scenario, second.plan.scenarios[1])))
    result, client = run(task_request.updated(max_rounds=2), [first, second])
    assert result.state.history[1].validation_error.startswith("preserve_tests")
    assert result.state.best_available_round == 1


@pytest.mark.parametrize("new_support", ["EXPECTED = 2\n", "EXPECTED = 1\nEXPECTED = 2\n"])
def test_constant_rebinding_cannot_weaken_unchanged_assertion(task_request, new_support):
    first = candidate([POS], code="from subject import choose\nEXPECTED = 1\ndef test_positive():\n    assert choose(1) == EXPECTED\n")
    second = candidate([POS, ZERO], code="from subject import choose\n" + new_support +
                       "def test_positive():\n    assert choose(1) == EXPECTED\ndef test_zero():\n    assert choose(0) == 0\n")
    result, client = run(task_request.updated(max_rounds=2), [first, second])
    assert result.state.history[1].validation_error.startswith("preserve_tests")
    assert result.state.history[1].execution is None


def test_rejected_relationship_plan_can_be_kept_in_state_and_repaired(task_request):
    first = candidate([POS])
    wrong = first.updated(plan=first.plan.updated(target_functions=("choose", "imaginary"), strategy=(
        *first.plan.strategy, m.StrategyDecision(subject_kind="function", function_name="imaginary",
                                               decision="not_selected", reason="invented target"))))
    result, client = run(task_request, [wrong, first, candidate([POS, ZERO, NEG])])
    assert result.state.history[0].validation_error.startswith("target_functions")
    assert result.state.history[0].generated.plan.target_functions == ("choose", "imaginary")
    assert result.decision.status == "completed"
    assert result.state.issues == ()


def test_revision_staging_refuses_overwriting_earlier_rounds(task_request):
    result, client = run(task_request.updated(max_rounds=2), [candidate([POS]), candidate([POS, ZERO])])
    original = (task_request.output_dir / "rounds/02/candidate.json").read_bytes()
    with pytest.raises(FileExistsError):
        stage_revision_candidate(source=result.state.source_info, llm_result=llm_result(candidate([POS, ZERO])),
                                 workspace=task_request.output_dir, history=result.state.history[:1])
    assert (task_request.output_dir / "rounds/02/candidate.json").read_bytes() == original


def test_source_snapshot_changes_stop_without_overwriting_original_input(task_request):
    original = task_request.source_path.read_bytes()
    client = SequenceClient([candidate([POS]), candidate([POS, ZERO])])
    def mutate_before_staging(**kwargs):
        (kwargs["workspace"] / "subject.py").write_text("def choose(n):\n    return 999\n", encoding="utf-8")
        return stage_revision_candidate(**kwargs)
    result = run_agent(task_request, client=client, oracle_mode=m.OracleMode.SPECIFICATION,
                       tools=AgentTools(stage_revision=mutate_before_staging))
    assert result.state.stop_reason == "unrecoverable_error"
    assert task_request.source_path.read_bytes() == original
    assert result.state.best_available_round == 1
    assert len(client.contexts) == 2
    assert (task_request.output_dir / "subject.py").read_bytes() == original
    assert "999" in (task_request.output_dir / "subject_at_stop.py").read_text(encoding="utf-8")


def test_actual_usage_is_summed_and_missing_usage_stays_unknown(task_request):
    class UsageClient(SequenceClient):
        def generate_tests(self, context):
            result = super().generate_tests(context)
            return result.updated(attempts=4, total_usage=m.TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
    client = UsageClient([candidate([POS]), candidate([POS, ZERO]), candidate([POS, ZERO, NEG])])
    result = run_agent(task_request, client=client, oracle_mode=m.OracleMode.SPECIFICATION)
    assert result.state.api_calls == 12
    assert len(client.contexts) == 3
    assert result.state.token_usage.total_tokens == 45


def test_input_error_has_explicit_failed_stop_and_no_model_request(task_request):
    task_request = task_request.updated(source_path=task_request.source_path.with_name("missing.py"))
    result, client = run(task_request, [])
    assert result.decision.status == "failed"
    assert result.state.stop_reason == "unrecoverable_error"
    assert result.state.current_round == 0
    assert not client.contexts


def test_timeout_ends_loop_without_second_model_request(task_request):
    from testgen_agent.tools.coverage_runner import CoverageRunResult
    def timeout(workspace, timeout):
        execution = m.ExecutionResult(kind="timeout", exit_code=-1, timed_out=True)
        coverage = m.CoverageResult(target_file=workspace / "subject.py", status="unavailable", error="timeout")
        return CoverageRunResult(execution=execution, coverage=coverage, artifacts=workspace, source_hints={})
    result, client = run(task_request, [candidate([POS])], tools=AgentTools(execute=timeout))
    assert result.state.stop_reason == "execution_timeout"
    assert result.decision.status == "failed"
    assert result.final_test_file is None
    assert len(client.contexts) == 1


def test_usage_unknown_in_any_round_prevents_invented_total(task_request):
    class MixedUsageClient(SequenceClient):
        def generate_tests(self, context):
            result = super().generate_tests(context)
            return result.updated(total_usage=m.TokenUsage(total_tokens=15)) if len(self.contexts) == 1 else result
    client = MixedUsageClient([candidate([POS]), candidate([POS, ZERO, NEG])])
    result = run_agent(task_request, client=client, oracle_mode=m.OracleMode.SPECIFICATION)
    assert result.decision.status == "completed"
    assert result.state.token_usage is None
    assert json.loads(result.summary_file.read_text(encoding="utf-8"))["token_usage"] is None


def test_oracle_mode_changes_are_rejected_before_execution(task_request):
    generated = candidate([POS])
    scenario = generated.plan.scenarios[0].updated(expected_basis=m.ExpectedBasis(
        kind="implementation_assumption", detail="unapproved characterization"))
    generated = generated.updated(plan=generated.plan.updated(oracle_mode="characterization", scenarios=(scenario,)))
    result, client = run(task_request.updated(max_rounds=1), [generated])
    assert result.state.history[0].validation_error.startswith("oracle_mode")
    assert result.state.history[0].execution is None
    assert not (task_request.output_dir / "rounds/01/test_subject.py").exists()


def test_task_memory_is_not_shared_between_runs(task_request):
    first, first_client = run(task_request.updated(max_rounds=1), [candidate([POS])], behavior_specification="FIRST TASK ONLY")
    second_request = task_request.updated(output_dir=task_request.output_dir.with_name("run-two"), max_rounds=1)
    second, second_client = run(second_request, [candidate([POS])], behavior_specification="SECOND TASK ONLY")
    assert "FIRST TASK ONLY" not in second_client.contexts[0].user_prompt
    assert second.state.api_calls == 1
    assert second.state.current_round == 1


def test_best_candidate_prefers_completion_over_one_stronger_metric(task_request):
    from test_agent import passed
    generated = candidate([POS])
    first = m.CandidateRound(number=1, generated=generated, test_path=Path("rounds/01/test_subject.py"), execution=passed(1),
                             coverage=m.CoverageResult(target_file=task_request.source_path, status="available",
                                                       covered_lines=8, total_lines=10, missing_lines=(5, 6),
                                                       covered_branches=2, total_branches=2))
    second = first.updated(number=2, coverage=m.CoverageResult(target_file=task_request.source_path, status="available",
                            covered_lines=9, total_lines=10, missing_lines=(6,), covered_branches=4, total_branches=5,
                            missing_branches=(m.BranchArc(start=4, end=6),)))
    assert select_best_candidate((first, second)) == 2


def test_passing_tests_in_runtime_error_round_are_also_protected(task_request):
    first = candidate([POS, ZERO], code="from subject import choose\ndef test_positive():\n    assert choose(1) == 1\ndef test_zero():\n    assert choose(unknown_name) == 0\n")
    result, client = run(task_request.updated(max_rounds=2), [first, candidate([ZERO])])
    assert result.state.history[0].execution.passed == 1
    assert result.state.history[0].execution.errors == 1
    assert result.state.history[1].validation_error.startswith("preserve_tests")
    assert result.state.history[1].execution is None
