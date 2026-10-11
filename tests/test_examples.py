import json
from pathlib import Path, PureWindowsPath

import pytest

from test_workflow.contracts import AgentContext, AgentResult, ExecutionAssessment, Message
from test_workflow.state import WorkflowState

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/contracts/p1"


def load_example(path, model, tmp_path):
    """Rebase recorded machine paths while retaining all contract relationships."""
    def portable(value):
        if isinstance(value, dict):
            return {
                key: json.dumps(portable(json.loads(item)), ensure_ascii=False)
                if key == "snapshot_json" else portable(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [portable(item) for item in value]
        if isinstance(value, str):
            recorded = PureWindowsPath(value)
            if recorded.is_absolute() and recorded.drive.endswith(":"):
                return str(tmp_path / recorded.drive[:-1] / Path(*recorded.parts[1:]))
        return value

    value = portable(json.loads(path.read_text(encoding="utf-8")))
    return model.model_validate_json(json.dumps(value, ensure_ascii=False))


@pytest.mark.parametrize("name,model", [
    ("generation_context", AgentContext), ("generation_result", AgentResult),
    ("execution_context", AgentContext), ("execution_result", AgentResult),
    ("message", Message), ("state", WorkflowState),
])
def test_checked_in_contract_examples_validate(name, model, tmp_path):
    value = load_example(EXAMPLES / f"{name}.json", model, tmp_path)
    assert model.model_validate_json(value.canonical_json()) == value


@pytest.mark.parametrize("sample", ["valid_initial", "invalid_syntax", "valid_revision",
                                    "removed_passing_test", "weakened_passing_test"])
@pytest.mark.parametrize("suffix,model", [("context", AgentContext), ("result", AgentResult)])
def test_p2_contract_examples_validate(sample, suffix, model, tmp_path):
    path = EXAMPLES.parent / "p2" / f"{sample}_{suffix}.json"
    value = load_example(path, model, tmp_path)
    assert model.model_validate_json(value.canonical_json()) == value


@pytest.mark.parametrize("sample", ["passed", "low_coverage_revision", "invalid_syntax", "no_tests_rejected",
                                    "ambiguous_assertion", "name_error", "import_error", "collection_error",
                                    "suspected_defect", "timeout", "coverage_export_failure"])
@pytest.mark.parametrize("suffix,model", [("context", AgentContext), ("result", AgentResult), ("assessment", ExecutionAssessment)])
def test_p3_contract_examples_validate(sample, suffix, model, tmp_path):
    path = EXAMPLES.parent / "p3" / f"{sample}_{suffix}.json"
    value = load_example(path, model, tmp_path)
    assert model.model_validate_json(value.canonical_json()) == value
