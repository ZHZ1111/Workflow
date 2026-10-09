from pathlib import Path

import pytest

from test_workflow.contracts import AgentContext, AgentResult, Message
from test_workflow.state import WorkflowState

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/contracts/p1"


@pytest.mark.parametrize("name,model", [
    ("generation_context", AgentContext), ("generation_result", AgentResult),
    ("execution_context", AgentContext), ("execution_result", AgentResult),
    ("message", Message), ("state", WorkflowState),
])
def test_checked_in_contract_examples_validate(name, model):
    value = model.model_validate_json((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    assert model.model_validate_json(value.canonical_json()) == value


@pytest.mark.parametrize("sample", ["valid_initial", "invalid_syntax", "valid_revision",
                                    "removed_passing_test", "weakened_passing_test"])
@pytest.mark.parametrize("suffix,model", [("context", AgentContext), ("result", AgentResult)])
def test_p2_contract_examples_validate(sample, suffix, model):
    path = EXAMPLES.parent / "p2" / f"{sample}_{suffix}.json"
    value = model.model_validate_json(path.read_text(encoding="utf-8"))
    assert model.model_validate_json(value.canonical_json()) == value
