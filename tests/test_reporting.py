"""P6 reports, committed message consumption and the real summary extension."""

import json
from pathlib import Path
import re
from unittest.mock import patch
from urllib.parse import unquote

import pytest

from test_workflow.cli import main
from test_workflow.contracts import AgentContext, AgentResult
from test_workflow.events import Event
from test_workflow.reporting import inspect_summary
from test_workflow.state import StateStore
from test_workflow.storage import file_digest, verify_artifact

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def run(tmp_path, source, fixtures, *, budget=3):
    output = tmp_path / "P6 中文 运行"
    args = ["run", "--demo", "--with-summary", "--source", str(ROOT / f"examples/{source}.py"),
            "--spec", str(ROOT / f"examples/{source}.md"), "--output", str(output), "--max-rounds", str(budget)]
    for fixture in fixtures:
        args += ["--fixture", str(FIXTURES / fixture)]
    with patch("test_workflow.cli.load_config", side_effect=AssertionError("offline cannot load credentials")):
        code = main(args)
    return code, StateStore(output).load(), json.loads((output / "summary.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("source,fixtures,budget,status,selected", [
    ("calculation", ("calculation_complete.json",), 3, "completed", 1),
    ("validation", ("validation_round_01.json", "validation_round_02.json"), 3, "completed", 2),
    ("validation", ("validation_round_01.json", "p4/later_name_error.json"), 2, "partial", 1),
    ("discount", ("discount_defect.json",), 3, "needs_review", 1),
    ("calculation", ("calculation_invalid_syntax.json",), 1, "failed", None),
])
def test_summary_extension_preserves_conclusion_and_links_real_evidence(tmp_path, source, fixtures, budget, status, selected):
    code, state, summary = run(tmp_path, source, fixtures, budget=budget)
    assert code == (0 if status == "completed" else 1)
    assert state.task_status == status and state.selected_round == selected
    assert summary["agents"]["result_summary"]["invocations"] == 1
    assert summary["agents"]["result_summary"]["committed_results"] == 1
    assert summary["external_requests"] == 0
    assert summary["pytest_processes"] == sum(candidate.execution is not None for candidate in state.agent_state.history)
    ctx = AgentContext.model_validate_json((state.run_dir / f"node_results/{state.current_round:02d}/result_summary/context.json").read_text(encoding="utf-8"))
    assert ctx.state.agent_state == state.agent_state and ctx.state.task_status == state.task_status
    result = AgentResult.model_validate_json((state.run_dir / f"node_results/{state.current_round:02d}/result_summary/result.json").read_text(encoding="utf-8"))
    assert result.patch.candidate is None and result.messages == ()
    assert result.generation is None and result.assessment is None
    data = json.loads(verify_artifact(state.run_dir, result.artifacts[0]).read_text(encoding="utf-8"))
    assert data["phase"] == "business_conclusion_before_export" and data["workflow_status"] == "running"
    assert (data["task_status"], data["stop_reason"], data["selected_round"]) == (summary["task_status"], summary["stop_reason"], selected)
    report = (state.run_dir / "report.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(([^)]+)\)", report):
        assert (state.run_dir / unquote(target)).is_file(), target
    assert "目标：行覆盖 ≥90%" in report and "pytest 启动" in report
    if status == "needs_review":
        assert "suspected_source_defect" in report and summary["selected_execution"]["failed"] == 1
    if fixtures[-1] == "p4/later_name_error.json":
        assert summary["selected_all_passed"] is True and summary["latest_all_passed"] is False
    if selected is None:
        assert summary["final_test_file"] is None and "没有可导出" in report


def test_feedback_consumption_event_identifies_source_and_target_round(tmp_path):
    _, state, summary = run(tmp_path, "validation", ("validation_round_01.json", "validation_round_02.json"))
    events = [Event.model_validate_json(line) for line in (state.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event.seq for event in events] == list(range(1, state.last_event_seq + 1))
    feedback = next(message for message in state.messages if message.type == "execution_feedback")
    sent = next(event for event in events if event.type == "message_sent" and event.message_id == feedback.message_id)
    consumed = next(event for event in events if event.type == "message_consumed" and event.message_id == feedback.message_id)
    assert sent.round == 1 and consumed.round == 2 and consumed.node == "test_generation"
    assert sent.candidate_id == consumed.candidate_id == f"{state.run_id}:1"
    assert sent.artifact_path == consumed.artifact_path == "evaluations/01/assessment.json"
    assert sent.seq < consumed.seq
    recorded = next(item for item in summary["messages"] if item["message_id"] == feedback.message_id)
    assert recorded["source_round"] == 1 and recorded["consumer_round"] == 2 and recorded["consumed"] is True
    for event in events:
        if event.type == "node_output_saved":
            assert (state.run_dir / event.result_path).is_file()
    stop = next(event for event in events if event.type == "workflow_stopped")
    tail = next(event for event in events if event.type == "node_started" and event.node == "result_summary")
    assert stop.seq < tail.seq and events[-1].type == "workflow_finished"


@pytest.mark.parametrize("damage", ["changed_final", "missing_final", "changed_artifact", "missing_artifact"])
def test_inspect_summary_is_read_only_and_detects_changed_evidence(tmp_path, capsys, damage):
    code, state, _ = run(tmp_path, "calculation", ("calculation_complete.json",))
    assert code == 0 and state.task_status == "completed"
    capsys.readouterr()
    before = {path.relative_to(state.run_dir): (file_digest(path), path.stat().st_mtime_ns)
              for path in state.run_dir.rglob("*") if path.is_file()}
    with patch("test_workflow.cli.DeepSeekClient", side_effect=AssertionError("inspection cannot use model")), patch(
            "test_workflow.cli.WorkflowRunner.run", side_effect=AssertionError("inspection cannot replay")):
        assert main(["inspect", str(state.run_dir), "--summary"]) == 0
    viewed = json.loads(capsys.readouterr().out)
    assert viewed["last_event_seq"] == state.last_event_seq
    after = {path.relative_to(state.run_dir): (file_digest(path), path.stat().st_mtime_ns)
             for path in state.run_dir.rglob("*") if path.is_file()}
    assert before == after
    target = state.run_dir / "final/test_subject.py" if damage.endswith("final") else state.agent_state.history[0].test_path
    if damage.startswith("missing"):
        target.unlink()
    else:
        target.write_text("changed", encoding="utf-8")
    assert main(["inspect", str(state.run_dir), "--summary"]) == 2
    with pytest.raises((ValueError, OSError)):
        inspect_summary(state)


def test_unconsumed_feedback_is_not_reported_as_used(tmp_path):
    # A later client error leaves previous feedback sent, but not committed as consumed.
    code, state, summary = run(tmp_path, "validation", ("validation_round_01.json",))
    assert code == 1 and state.workflow_status == "error"
    feedback = next(item for item in summary["messages"] if item["type"] == "execution_feedback")
    assert feedback["consumed"] is False and feedback["consumer_round"] is None
    assert "result_summary" not in summary["agents"]
    assert summary["agents"]["test_generation"] == {"role": summary["agents"]["test_generation"]["role"],
                                                     "invocations": 2, "committed_results": 1}


def test_coverage_unavailable_report_keeps_true_test_outcomes(tmp_path):
    with patch("testgen_agent.tools.coverage_runner.subprocess.run", side_effect=OSError("export fault")):
        _, state, summary = run(tmp_path, "calculation", ("calculation_complete.json",))
    assert state.task_status == "partial" and summary["selected_all_passed"] is True
    assert summary["selected_coverage"]["status"] == "unavailable"
    assert summary["selected_coverage"]["line_percent"] is None
    report = (state.run_dir / "report.md").read_text(encoding="utf-8")
    assert "unavailable" in report and summary["selected_coverage"]["error"] in report
