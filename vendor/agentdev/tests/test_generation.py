"""C2 validates and stages model output without claiming synthetic output is a real LLM run."""

import json
from pathlib import Path

import pytest

from testgen_agent import models as m
from testgen_agent.generation import (
    CandidateValidationError,
    generate_initial_candidate,
    stage_initial_candidate,
    validate_generated_tests,
)
from testgen_agent.llm_client import LLMCallRecord, LLMGenerationResult
from testgen_agent.tools import analyze_source, run_tests


class SyntheticClient:
    """Offline adapter fixture; never presented as an external model result."""

    def __init__(self, result: LLMGenerationResult) -> None:
        self.result = result
        self.contexts = []

    def generate_tests(self, context):
        self.contexts.append(context)
        return self.result


def llm_result(generated: m.GeneratedTests) -> LLMGenerationResult:
    return LLMGenerationResult(
        generated=generated,
        provider="deepseek",
        requested_model="offline-synthetic-model",
        response_model="offline-synthetic-model",
        attempts=1,
        format_repairs=0,
        latency_seconds=0.01,
        calls=(LLMCallRecord(purpose="generation", response_id="offline-fixture"),),
    )


def relationship_generated(source: m.SourceInfo) -> m.GeneratedTests:
    call_id = source.direct_calls[0].call_site_id
    relationship = m.RelationshipAnalysis(
        relation_id="rel-checkout-total",
        kind="direct_call",
        involved_functions=("checkout", "calculate_total"),
        evidence=(m.RelationshipEvidence(
            kind="source",
            detail="checkout directly calls calculate_total",
            line=11,
            call_site_ids=(call_id,),
        ),),
        confidence="confirmed",
    )
    plan = m.TestPlan(
        schema_version="1.1",
        target_functions=("calculate_total", "checkout"),
        oracle_mode="specification",
        entry_functions=("checkout",),
        relationships=(relationship,),
        strategy=(
            m.StrategyDecision(
                subject_kind="function", function_name="calculate_total",
                decision="unit_test", reason="multiplication has a focused boundary",
            ),
            m.StrategyDecision(
                subject_kind="function", function_name="checkout",
                decision="unit_test", reason="entry behavior can be checked directly",
            ),
            m.StrategyDecision(
                subject_kind="relationship", relationship_id="rel-checkout-total",
                decision="collaboration_test", reason="real entry and dependency produce one result",
            ),
        ),
        scenarios=(
            m.TestScenario(
                scenario_id="unit-total-zero",
                target_function="calculate_total",
                category="boundary",
                inputs=m.ScenarioInput(args=(0, 5)),
                expected_behavior="returns zero",
                expected_basis=m.ExpectedBasis(kind="specification", detail="zero price boundary"),
                test_kind="unit",
                involved_functions=("calculate_total",),
            ),
            m.TestScenario(
                scenario_id="collaboration-checkout",
                target_function="checkout",
                category="normal",
                inputs=m.ScenarioInput(args=(12, 3)),
                expected_behavior="returns 36 through the real calculation",
                expected_basis=m.ExpectedBasis(kind="specification", detail="checkout workflow"),
                test_kind="collaboration",
                involved_functions=("checkout", "calculate_total"),
                relationship_ids=("rel-checkout-total",),
            ),
        ),
    )
    return m.GeneratedTests(
        plan=plan,
        code=(
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero():\n"
            "    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n"
            "    assert checkout(12, 3) == 36\n"
        ),
        mappings=(
            m.TestCaseMapping(
                test_function="test_unit_total_zero", scenario_ids=("unit-total-zero",),
            ),
            m.TestCaseMapping(
                test_function="test_collaboration_checkout",
                scenario_ids=("collaboration-checkout",),
            ),
        ),
    )


def single_function_generated(
    function_name: str,
    *,
    code: str,
    category: str = "normal",
) -> m.GeneratedTests:
    scenario_id = f"{function_name}-{category}"
    return m.GeneratedTests(
        plan=m.TestPlan(
            schema_version="1.1",
            target_functions=(function_name,),
            oracle_mode="specification",
            entry_functions=(function_name,),
            strategy=(m.StrategyDecision(
                subject_kind="function", function_name=function_name,
                decision="unit_test", reason="the function has independently specified behavior",
            ),),
            scenarios=(m.TestScenario(
                scenario_id=scenario_id,
                target_function=function_name,
                category=category,
                inputs=m.ScenarioInput(),
                expected_behavior="matches the public specification",
                expected_basis=m.ExpectedBasis(kind="specification", detail="example specification"),
                test_kind="unit",
                involved_functions=(function_name,),
            ),),
        ),
        code=code,
        mappings=(m.TestCaseMapping(test_function="test_behavior", scenario_ids=(scenario_id,)),),
    )


@pytest.fixture
def relationship_source():
    return analyze_source(Path(__file__).parents[1] / "examples" / "relationships.py")


@pytest.fixture
def relationship_candidate(relationship_source):
    return relationship_generated(relationship_source)


def test_generate_once_stages_fixed_paths_and_round_trip_snapshots(
    tmp_path, relationship_source, relationship_candidate,
):
    original = relationship_source.source_path.read_bytes()
    client = SyntheticClient(llm_result(relationship_candidate))

    artifacts = generate_initial_candidate(
        source=relationship_source,
        behavior_specification="checkout returns calculate_total's result",
        oracle_mode=m.OracleMode.SPECIFICATION,
        workspace=tmp_path / "run-one",
        client=client,
    )

    assert len(client.contexts) == 1
    assert artifacts.ready_for_execution
    assert artifacts.subject_path.read_bytes() == original
    assert relationship_source.source_path.read_bytes() == original
    assert artifacts.test_path == tmp_path / "run-one" / "rounds" / "01" / "test_subject.py"
    assert artifacts.test_path.read_text(encoding="utf-8") == relationship_candidate.code
    assert m.TestPlan.model_validate_json(artifacts.plan_path.read_text(encoding="utf-8")) == relationship_candidate.plan
    assert m.CandidateRound.model_validate_json(
        artifacts.candidate_path.read_text(encoding="utf-8")
    ) == artifacts.candidate
    assert LLMGenerationResult.model_validate_json(
        artifacts.llm_result_path.read_text(encoding="utf-8")
    ) == artifacts.llm_result
    prompt_payload = json.loads(
        client.contexts[0].user_prompt.split("TASK_DATA_JSON_START\n", 1)[1]
        .rsplit("\nTASK_DATA_JSON_END", 1)[0]
    )
    assert prompt_payload["source_graph_hints"]["candidate_entry_functions"] == ["checkout"]


def test_staged_synthetic_simple_candidate_executes_with_real_pytest(
    tmp_path, relationship_source, relationship_candidate,
):
    artifacts = stage_initial_candidate(
        source=relationship_source,
        llm_result=llm_result(relationship_candidate),
        workspace=tmp_path / "execution-check",
    )
    execution = run_tests(artifacts.workspace, timeout=20)

    assert execution.all_passed, execution.stdout + execution.stderr
    assert execution.collected == execution.executed == execution.passed == 2


def test_independent_functions_keep_separate_unit_strategy(tmp_path):
    source = analyze_source(Path(__file__).parents[1] / "examples" / "independent.py")
    plan = m.TestPlan(
        schema_version="1.1",
        target_functions=("rectangle_area", "is_even"),
        oracle_mode="specification",
        entry_functions=("rectangle_area", "is_even"),
        strategy=(
            m.StrategyDecision(
                subject_kind="function", function_name="rectangle_area",
                decision="unit_test", reason="independent area behavior",
            ),
            m.StrategyDecision(
                subject_kind="function", function_name="is_even",
                decision="unit_test", reason="independent parity behavior",
            ),
        ),
        scenarios=(
            m.TestScenario(
                scenario_id="area-zero", target_function="rectangle_area", category="boundary",
                inputs=m.ScenarioInput(args=(0, 5)), expected_behavior="returns zero",
                expected_basis=m.ExpectedBasis(kind="specification", detail="zero width"),
                involved_functions=("rectangle_area",),
            ),
            m.TestScenario(
                scenario_id="even-four", target_function="is_even", category="normal",
                inputs=m.ScenarioInput(args=(4,)), expected_behavior="returns true",
                expected_basis=m.ExpectedBasis(kind="specification", detail="four is even"),
                involved_functions=("is_even",),
            ),
        ),
    )
    generated = m.GeneratedTests(
        plan=plan,
        code=(
            "from subject import rectangle_area, is_even\n\n"
            "def test_area_zero():\n    assert rectangle_area(0, 5) == 0\n\n"
            "def test_even_four():\n    assert is_even(4) is True\n"
        ),
        mappings=(
            m.TestCaseMapping(test_function="test_area_zero", scenario_ids=("area-zero",)),
            m.TestCaseMapping(test_function="test_even_four", scenario_ids=("even-four",)),
        ),
    )

    validate_generated_tests(generated, source)

    assert source.direct_calls == ()
    assert plan.relationships == ()
    assert {item.decision for item in plan.strategy} == {"unit_test"}
    assert {item.test_kind for item in plan.scenarios} == {"unit"}


@pytest.mark.parametrize(
    ("filename", "function_name", "code", "category"),
    [
        (
            "calculation.py", "calculate_total",
            "from subject import calculate_total\n\ndef test_behavior():\n    assert calculate_total(12, 3) == 36\n",
            "normal",
        ),
        (
            "validation.py", "classify_score",
            "import pytest\nfrom subject import classify_score\n\n"
            "def test_behavior():\n    with pytest.raises(TypeError):\n        classify_score(True)\n",
            "exception",
        ),
        (
            "discount.py", "calculate_discount",
            "from subject import calculate_discount\n\ndef test_behavior():\n    assert calculate_discount(99) == 99\n",
            "boundary",
        ),
    ],
)
def test_original_examples_remain_valid_c2_inputs(filename, function_name, code, category):
    source = analyze_source(Path(__file__).parents[1] / "examples" / filename)
    generated = single_function_generated(function_name, code=code, category=category)

    validate_generated_tests(generated, source)


def test_precise_no_code_response_is_saved_without_executable_file(
    tmp_path, relationship_source, relationship_candidate,
):
    generated = relationship_candidate.updated(
        plan=relationship_candidate.plan.updated(scenarios=()),
        code=None,
        mappings=(),
        questions=("请确认 checkout 的业务入口语义。",),
    )

    artifacts = stage_initial_candidate(
        source=relationship_source,
        llm_result=llm_result(generated),
        workspace=tmp_path / "question",
    )

    assert not artifacts.ready_for_execution
    assert artifacts.candidate.validation_error is None
    assert artifacts.test_path is None
    assert artifacts.plan_path.is_file() and artifacts.candidate_path.is_file()


def test_invalid_syntax_is_preserved_as_rejected_snapshot_not_test_file(
    tmp_path, relationship_source, relationship_candidate,
):
    generated = relationship_candidate.updated(
        code="from subject import calculate_total\n\ndef test_unit_total_zero(:\n",
    )

    artifacts = stage_initial_candidate(
        source=relationship_source,
        llm_result=llm_result(generated),
        workspace=tmp_path / "invalid",
    )

    assert not artifacts.ready_for_execution
    assert artifacts.test_path is None
    assert artifacts.candidate.validation_error.startswith("test_syntax")
    assert not (artifacts.workspace / "rounds/01/test_subject.py").exists()
    saved = LLMGenerationResult.model_validate_json(
        artifacts.llm_result_path.read_text(encoding="utf-8")
    )
    assert saved.generated.code == generated.code


@pytest.mark.parametrize(
    ("code", "error_code"),
    [
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_wrong_name():\n    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n",
            "test_mapping",
        ),
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero():\n    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n\n"
            "def test_unplanned():\n    assert checkout(1, 1) == 1\n",
            "test_mapping",
        ),
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero():\n    calculate_total(0, 5)\n    assert 1 == 1\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n",
            "missing_assertion",
        ),
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero():\n    assert True\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n",
            "unconditional_assertion",
        ),
    ],
)
def test_rejects_missing_extra_or_ineffective_tests(
    relationship_source, relationship_candidate, code, error_code,
):
    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(code=code), relationship_source)

    assert captured.value.code == error_code


@pytest.mark.parametrize(
    ("replacement", "error_code"),
    [
        ("from examples.relationships import calculate_total, checkout", "external_dependency"),
        ("from subject import calculate_total as total, checkout", "target_import"),
        ("import subject", "target_import"),
        ("import requests", "external_dependency"),
        ("import pathlib", "unsafe_dependency"),
        ("from unittest.mock import patch", "mocking"),
    ],
)
def test_rejects_imports_outside_fixed_execution_contract(
    relationship_source, relationship_candidate, replacement, error_code,
):
    code = relationship_candidate.code.replace(
        "from subject import calculate_total, checkout", replacement,
    )

    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(code=code), relationship_source)

    assert captured.value.code == error_code


@pytest.mark.parametrize(
    ("injected", "error_code"),
    [
        ("    pytest.skip('later')\n", "forbidden_call"),
        ("    monkeypatch.setattr(checkout, '__name__', 'fake')\n", "forbidden_call"),
        ("    with patch('subject.calculate_total'):\n        checkout(12, 3)\n", "forbidden_call"),
        ("    try:\n        checkout(12, 3)\n    except Exception:\n        pass\n", "broad_exception"),
    ],
)
def test_rejects_bypasses_mocks_and_broad_exception_handlers(
    relationship_source, relationship_candidate, injected, error_code,
):
    code = relationship_candidate.code.replace(
        "def test_collaboration_checkout():\n",
        "def test_collaboration_checkout():\n" + injected,
    )

    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(code=code), relationship_source)

    assert captured.value.code == error_code


def test_rejects_skip_marker_without_call_syntax(relationship_source, relationship_candidate):
    code = relationship_candidate.code.replace(
        "def test_collaboration_checkout():",
        "@pytest.mark.skip\ndef test_collaboration_checkout():",
    ).replace(
        "from subject import calculate_total, checkout",
        "import pytest\nfrom subject import calculate_total, checkout",
    )

    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(code=code), relationship_source)

    assert captured.value.code == "test_decorator"


def test_rejects_plan_that_omits_analyzed_function(relationship_source, relationship_candidate):
    scenario = relationship_candidate.plan.scenarios[0]
    plan = m.TestPlan(
        schema_version="1.1",
        target_functions=("calculate_total",),
        oracle_mode="specification",
        entry_functions=("calculate_total",),
        strategy=(relationship_candidate.plan.strategy[0],),
        scenarios=(scenario,),
    )
    generated = m.GeneratedTests(
        plan=plan,
        code="from subject import calculate_total\n\ndef test_unit_total_zero():\n    assert calculate_total(0, 5) == 0\n",
        mappings=(relationship_candidate.mappings[0],),
    )

    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(generated, relationship_source)

    assert captured.value.code == "target_functions"


def test_rejects_relationship_not_supported_by_analyzer(relationship_source, relationship_candidate):
    relation = relationship_candidate.plan.relationships[0]
    evidence = relation.evidence[0].updated(call_site_ids=("invented-call",))
    changed_relation = relation.updated(evidence=(evidence,))
    plan = relationship_candidate.plan.updated(relationships=(changed_relation,))

    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(plan=plan), relationship_source)

    assert captured.value.code == "source_evidence"


def test_rejects_nested_test_body_that_hides_oracle(relationship_source, relationship_candidate):
    code = (
        "from subject import calculate_total, checkout\n\n"
        "def test_unit_total_zero():\n"
        "    def hidden():\n        assert calculate_total(0, 5) == 0\n"
        "    hidden()\n\n"
        "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n"
    )

    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(code=code), relationship_source)

    assert captured.value.code == "nested_test_function"


@pytest.mark.parametrize(
    "code",
    [
        (
            "from subject import calculate_total, checkout\n\n"
            "def checkout(value):\n    return value\n\n"
            "def test_unit_total_zero():\n    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n    assert checkout(36) == 36\n"
        ),
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero(calculate_total):\n    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n"
        ),
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero():\n"
            "    calculate_total = lambda *args: 0\n"
            "    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n    assert checkout(12, 3) == 36\n"
        ),
        (
            "from subject import calculate_total, checkout\n\n"
            "def test_unit_total_zero():\n    assert calculate_total(0, 5) == 0\n\n"
            "def test_collaboration_checkout():\n"
            "    assert [checkout(12, 3) for checkout in [lambda *args: 36]] == [36]\n"
        ),
    ],
)
def test_rejects_any_rebinding_of_imported_target(
    relationship_source, relationship_candidate, code,
):
    with pytest.raises(CandidateValidationError) as captured:
        validate_generated_tests(relationship_candidate.updated(code=code), relationship_source)

    assert captured.value.code == "target_shadowing"


def test_rejects_existing_workspace_without_overwriting(
    tmp_path, relationship_source, relationship_candidate,
):
    workspace = tmp_path / "existing"
    workspace.mkdir()
    marker = workspace / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        stage_initial_candidate(
            source=relationship_source,
            llm_result=llm_result(relationship_candidate),
            workspace=workspace,
        )

    assert marker.read_text(encoding="utf-8") == "user data"
