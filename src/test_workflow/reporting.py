"""Minimal P4 final selection and evidence export; rich reporting belongs to P6."""

import json
import shutil

from .state import WorkflowState
from .storage import atomic_write, file_digest, verify_artifact
from .contracts import AgentResult


def export_result(state: WorkflowState) -> None:
    selected = (state.agent_state.history[state.selected_round - 1]
                if state.selected_round is not None else None)
    final = None
    if selected is not None:
        reference = next(item for item in state.artifacts
                         if item.kind == "tests" and item.round == selected.number)
        source = verify_artifact(state.run_dir, reference)
        final = state.run_dir / "final" / "test_subject.py"
        final.parent.mkdir(exist_ok=False)
        shutil.copyfile(source, final)
        subject = next(item for item in state.artifacts if item.kind == "subject")
        shutil.copyfile(verify_artifact(state.run_dir, subject), final.parent / "subject.py")
    results = tuple(AgentResult.model_validate_json(path.read_text(encoding="utf-8"))
                    for path in sorted((state.run_dir / "node_results").glob("*/*/result.json")))
    generation = tuple(result.generation for result in results if result.generation is not None)
    observation_modes = sorted({result.assessment.observation_mode for result in results if result.assessment})
    requests_known = all(item.external_requests is not None for item in generation)
    reason = ("Passing candidates first, then target/available coverage, branch and line coverage, and passed count; ties keep the earlier round."
              if selected else "No executed runnable candidate is available.")
    latest = state.agent_state.history[-1] if state.agent_state.history else None
    summary = {
        "run_id": state.run_id, "mode": state.config.mode,
        "generation_mode": "offline" if state.config.mode == "offline" else "real",
        "workflow_status": state.workflow_status, "task_status": state.task_status,
        "stop_reason": state.stop_reason, "error_code": state.error_code,
        "rounds": len(state.agent_state.history), "selected_round": state.selected_round,
        "selection_reason": reason,
        "selected_all_passed": selected.execution.all_passed if selected else None,
        "latest_all_passed": latest.execution.all_passed if latest and latest.execution else None,
        "selected_execution": selected.execution.model_dump(mode="json") if selected else None,
        "selected_coverage": selected.coverage.model_dump(mode="json") if selected and selected.coverage else None,
        "final_test_file": str(final) if final else None,
        "final_test_sha256": file_digest(final) if final else None,
        "generation_calls": sum(item.client_calls for item in generation),
        "external_requests": state.agent_state.api_calls if requests_known else None,
        "known_external_requests": state.agent_state.api_calls,
        "execution_observation_modes": observation_modes,
        "report_file": str(state.run_dir / "report.md"),
        "state_file": str(state.run_dir / "state.json"),
    }
    atomic_write(state.run_dir / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    rows = ["# Workflow result", "", f"- Run: `{state.run_id}`",
            f"- Generation: `{summary['generation_mode']}`; execution observations: `{', '.join(observation_modes) or 'none'}`.",
            f"- Generation calls: `{summary['generation_calls']}`; external requests: `{summary['external_requests']}`.",
            f"- Workflow: `{state.workflow_status}`; task: `{state.task_status}`",
            f"- Stop: `{state.stop_reason}`; error: `{state.error_code}`",
            f"- Selected round: `{state.selected_round}`", f"- Selection: {reason}",
            f"- Selected all passed: `{summary['selected_all_passed']}`; latest all passed: `{summary['latest_all_passed']}`",
            "", "An earlier selected candidate does not imply the latest candidate passed.", "",
            "| Round | Validation | Execution | Passed | Failed | Line % | Branch % |",
            "|---|---|---|---|---|---|---|"]
    for candidate in state.agent_state.history:
        execution, coverage = candidate.execution, candidate.coverage
        rows.append(f"| {candidate.number} | {'rejected' if candidate.validation_error else 'accepted'} | "
                    f"{execution.kind if execution else 'not executed'} | {execution.passed if execution else '-'} | "
                    f"{execution.failed if execution else '-'} | {coverage.line_percent if coverage else '-'} | "
                    f"{coverage.branch_percent if coverage else '-'} |")
    rows += ["", "## Evidence", "", "- [State](state.json)", "- [Summary](summary.json)",
             "- [Events](events.jsonl)", "- Node contexts/results: `node_results/NN/<node>/`",
             "- Assessments: `evaluations/NN/assessment.json`", "- Original round evidence remains in `agent_workspace/rounds/`." ]
    if final:
        rows.append("- [Selected tests](final/test_subject.py) and [source copy](final/subject.py)")
    if state.agent_state.issues:
        rows += ["", "## Recorded issues", ""]
        rows.extend(f"- Round {issue.round_number}: `{issue.kind}` — {issue.summary}"
                    for issue in state.agent_state.issues)
    atomic_write(state.run_dir / "report.md", "\n".join(rows) + "\n")
