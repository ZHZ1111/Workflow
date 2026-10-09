from pathlib import Path

from test_workflow.cli import main

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
