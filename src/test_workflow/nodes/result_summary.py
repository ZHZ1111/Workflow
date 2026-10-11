"""A small, model-free extension observing the committed business conclusion."""

import json

from ..contracts import AgentResult, ArtifactRef
from ..storage import atomic_write, file_digest


class ResultSummaryNode:
    def run(self, context):
        state = context.state
        if state.task_status is None or state.stop_reason is None:
            raise ValueError("summary node requires a committed testing conclusion")
        selected = state.agent_state.history[state.selected_round - 1] if state.selected_round else None
        directory = context.run_dir / "extensions" / f"{context.round:02d}"
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / "result_summary.json"
        data = {
            "schema_version": "1.0", "run_id": state.run_id,
            "phase": "business_conclusion_before_export", "workflow_status": state.workflow_status,
            "task_status": state.task_status, "stop_reason": state.stop_reason,
            "rounds": len(state.agent_state.history), "selected_round": state.selected_round,
            "selected_all_passed": selected.execution.all_passed if selected else None,
            "state_event_seq": state.last_event_seq,
            "state_file": "state.json", "events_file": "events.jsonl",
        }
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        artifact = ArtifactRef(path=path.relative_to(context.run_dir).as_posix(), kind="report",
                               sha256=file_digest(path), round=context.round,
                               candidate_id=f"{context.run_id}:{context.round}")
        return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                           artifacts=(artifact,))
