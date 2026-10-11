from pathlib import Path
from unittest.mock import patch

from testgen_agent.config import AppConfig

from test_workflow.cli import main
from test_workflow.service import fixture_client
from test_workflow.state import StateStore

ROOT = Path(__file__).resolve().parents[1]


def test_demo_and_inspect(tmp_path, capsys):
    assert main(["demo", "--source", str(ROOT / "examples/calculation.py"),
                 "--spec", str(ROOT / "examples/calculation.md"), "--output", str(tmp_path)]) == 0
    assert "mode=demo workflow_status=finished task_status=unmeasured" in capsys.readouterr().out
    runs = list(tmp_path.glob("run-*"))
    assert len(runs) == 1
    assert main(["inspect", str(runs[0])]) == 0
    assert '"workflow_status": "finished"' in capsys.readouterr().out


def test_invalid_inputs_create_no_task(tmp_path):
    blank = tmp_path / "blank.md"
    blank.write_text("  \n", encoding="utf-8")
    output = tmp_path / "runs"
    assert main(["demo", "--source", str(ROOT / "examples/calculation.py"),
                 "--spec", str(blank), "--output", str(output)]) == 2
    assert not output.exists()
    unsupported = tmp_path / "class.py"
    unsupported.write_text("class Example:\n    pass\n", encoding="utf-8")
    assert main(["demo", "--source", str(unsupported),
                 "--spec", str(ROOT / "examples/calculation.md"), "--output", str(output)]) == 2
    assert not output.exists()


def test_missing_and_invalid_saved_state(tmp_path):
    assert main(["inspect", str(tmp_path)]) == 2
    (tmp_path / "state.json").write_text("{partial", encoding="utf-8")
    assert main(["inspect", str(tmp_path)]) == 2


def test_real_cli_uses_environment_round_and_execution_defaults(tmp_path):
    config = AppConfig(model="test-model", api_key="test-key", max_rounds=1, execution_timeout_seconds=12)
    output = tmp_path / "run"
    with patch("test_workflow.cli.load_config", return_value=config), patch(
            "test_workflow.cli.DeepSeekClient", return_value=fixture_client("calculation")):
        assert main(["run", "--source", str(ROOT / "examples/calculation.py"),
                     "--spec", str(ROOT / "examples/calculation.md"), "--output", str(output)]) == 0
    state = StateStore(output).load()
    assert state.config.max_rounds == 1 and state.config.execution_timeout_seconds == 12
