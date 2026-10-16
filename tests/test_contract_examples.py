"""Cross-platform regression checks for historical contract fixture loading."""

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from pydantic import ValidationError

from contract_examples import rebase_recorded_paths
from test_workflow.contracts import AgentContext
from test_workflow.state import WorkflowState

ROOT = Path(__file__).resolve().parents[1] / "examples/contracts"


@pytest.mark.parametrize("root", [PurePosixPath("/tmp/contracts"), PureWindowsPath("C:/Temp/contracts")])
def test_drive_paths_and_nested_snapshot_share_one_native_root(root):
    run_dir = r"E:\recorded\run-1"
    workspace = run_dir + r"\agent_workspace"
    recorded = {
        "run_dir": run_dir,
        "snapshot_json": json.dumps({"run_dir": run_dir, "output_dir": workspace}),
        "references": [{"path": "evidence/01/tests.py"}, {"source_path": "F:/source/subject.py"}],
        "source_code": "def example():\n    return 1\n",
    }
    rebased = rebase_recorded_paths(recorded, root)
    snapshot = json.loads(rebased["snapshot_json"])
    assert rebased["run_dir"] == snapshot["run_dir"] == str(root / "E/recorded/run-1")
    assert snapshot["output_dir"] == str(root / "E/recorded/run-1/agent_workspace")
    assert rebased["references"][1]["source_path"] == str(root / "F/source/subject.py")
    assert rebased["references"][0] == recorded["references"][0]
    assert rebased["source_code"] == recorded["source_code"]
    assert recorded["run_dir"] == run_dir  # Rebasing does not alter the saved fixture.


@pytest.mark.parametrize("relative", ["p3/passed_context.json", "p4/revision_context.json"])
def test_shared_loader_preserves_context_and_workspace_relationships(relative, contract_example, tmp_path):
    context = contract_example(ROOT / relative, AgentContext)
    state = context.state
    assert context.run_dir.is_absolute() and context.run_dir.is_relative_to(tmp_path)
    assert state.run_dir == context.run_dir
    assert state.agent_state.request.output_dir == state.run_dir / "agent_workspace"
    assert (context.run_id, context.round, context.agent_id) == (state.run_id, state.current_round, state.current_node)


def test_shared_loader_does_not_accept_relative_run_directory(contract_example, tmp_path):
    state = contract_example(ROOT / "p1/state.json", WorkflowState)
    invalid = json.loads(state.canonical_json())
    invalid["run_dir"] = "relative/run-1"
    path = tmp_path / "invalid-state.json"
    path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValidationError, match="run directory must be absolute"):
        contract_example(path, WorkflowState)
