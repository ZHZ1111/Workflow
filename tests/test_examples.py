from pathlib import Path

import pytest

from test_workflow.contracts import AgentContext, AgentResult, ExecutionAssessment, Message
from test_workflow.state import WorkflowState

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/contracts/p1"


@pytest.mark.parametrize("name,model", [
    ("generation_context", AgentContext), ("generation_result", AgentResult),
    ("execution_context", AgentContext), ("execution_result", AgentResult),
    ("message", Message), ("state", WorkflowState),
])
def test_checked_in_contract_examples_validate(name, model, contract_example):
    value = contract_example(EXAMPLES / f"{name}.json", model)
    assert model.model_validate_json(value.canonical_json()) == value


@pytest.mark.parametrize("sample", ["valid_initial", "invalid_syntax", "valid_revision",
                                    "removed_passing_test", "weakened_passing_test"])
@pytest.mark.parametrize("suffix,model", [("context", AgentContext), ("result", AgentResult)])
def test_p2_contract_examples_validate(sample, suffix, model, contract_example):
    path = EXAMPLES.parent / "p2" / f"{sample}_{suffix}.json"
    value = contract_example(path, model)
    assert model.model_validate_json(value.canonical_json()) == value


@pytest.mark.parametrize("sample", ["passed", "low_coverage_revision", "invalid_syntax", "no_tests_rejected",
                                    "ambiguous_assertion", "name_error", "import_error", "collection_error",
                                    "suspected_defect", "timeout", "coverage_export_failure"])
@pytest.mark.parametrize("suffix,model", [("context", AgentContext), ("result", AgentResult), ("assessment", ExecutionAssessment)])
def test_p3_contract_examples_validate(sample, suffix, model, contract_example):
    path = EXAMPLES.parent / "p3" / f"{sample}_{suffix}.json"
    value = contract_example(path, model)
    assert model.model_validate_json(value.canonical_json()) == value
