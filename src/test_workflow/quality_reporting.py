"""Read-only quality provenance used by reporting and strict inspection."""

from .adapters.quality_revision import validate_quality_record
from .contracts import AgentContext
from .quality_client import ReviewResponse
from .quality_state import validate_quality_completion
from .storage import verify_artifact


def quality_details(state, rounds, nodes, messages, *, strict=False):
    assessments = {item.round: item for item in state.quality_assessments}
    errors = []
    for row in rounds:
        number = row["round"]
        quality = assessments.get(number)
        review_nodes = [node for node in nodes if node["node"] == "test_quality_review" and node["round"] == number]
        record = {"status": "disabled", "reason": "quality_review_disabled", "assessment": None,
                  "assessment_path": None, "response_path": None, "telemetry": None, "evidence_verified": None,
                  "feedback": [message for message in messages if message["type"] == "quality_feedback" and message["source_round"] == number]}
        if state.config.quality_review_enabled:
            record.update(status="skipped", reason="base_not_completed")
            if (row["assessment"] or {}).get("task_status") == "completed":
                record.update(status="not_run", reason="awaiting_review" if state.workflow_status in {"pending", "running"} else "review_not_started")
            if review_nodes:
                last = review_nodes[-1]
                record.update(status="error" if last["output_status"] == "error" or not last["committed"] else "not_run",
                              reason=last["error_code"] or "review_not_committed", telemetry=last["review"])
        if quality is not None:
            record.update(status={"approve": "passed", "request_revision": "changes_requested", "request_review": "needs_review"}[quality.decision.verdict],
                          reason=quality.decision.summary, assessment=quality.model_dump(mode="json"),
                          assessment_path=f"quality_reviews/{number:02d}/assessment.json", response_path=f"quality_reviews/{number:02d}/response.json")
            try:
                validate_quality_record(state, quality)
                response_ref = next(ref for ref in state.artifacts if ref.kind == "quality_response" and ref.round == number)
                response = ReviewResponse.model_validate_json(verify_artifact(state.run_dir, response_ref).read_text(encoding="utf-8"))
                committed = [node for node in review_nodes if node["committed"] and node["protocol_matches"]]
                if len(committed) != 1 or committed[0]["quality_assessment"] != record["assessment"] or committed[0]["review"] != response.telemetry.model_dump(mode="json"):
                    raise ValueError("committed quality node differs from its response or assessment")
                record.update(evidence_verified=True, telemetry=response.telemetry.model_dump(mode="json"))
            except (OSError, ValueError, StopIteration, SyntaxError):
                if strict:
                    raise ValueError("quality evidence or its committed node cannot be verified") from None
                errors.append(number)
                record.update(status="error", reason="invalid_quality_evidence", evidence_verified=False)
        row["quality"] = record
    if strict:
        validate_quality_completion(state)
        for message in state.messages:
            if message.type != "quality_feedback":
                continue
            quality = assessments.get(message.round)
            if (quality is None or quality.decision.verdict != "request_revision" or message.payload.artifact not in state.artifacts
                    or message.payload.artifact.path != f"quality_reviews/{message.round:02d}/assessment.json"
                    or message.payload.reason != quality.decision.summary
                    or message.payload.quality_feedback.findings != tuple(item for item in quality.decision.findings if item.blocking)):
                raise ValueError("quality feedback differs from its committed review")
            if message.message_id in state.consumed_message_ids:
                consumers = [node for node in nodes if node["committed"] and message.message_id in node["inbox"]]
                if len(consumers) != 1 or consumers[0]["node"] != "test_generation" or consumers[0]["round"] != message.round + 1:
                    raise ValueError("quality consumption requires one committed next-round generator")
                context = AgentContext.model_validate_json((state.run_dir / consumers[0]["context"]).read_text(encoding="utf-8"))
                if context.inbox != (message,):
                    raise ValueError("quality consumption context differs from its saved feedback")
    latest = state.quality_assessments[-1] if state.quality_assessments else None

    def approved(number):
        if not state.config.quality_review_enabled or number is None:
            return None
        row = next((item for item in rounds if item["round"] == number), None)
        return bool(row and row["quality"]["status"] == "passed" and row["quality"]["evidence_verified"])

    return {"quality_review_enabled": state.config.quality_review_enabled,
            "quality_status": state.quality_status, "quality_stop_reason": state.quality_stop_reason,
            "quality_assessments": [item.model_dump(mode="json") for item in state.quality_assessments],
            "quality_integrity_errors": errors,
            "unresolved_findings": [item.model_dump(mode="json") for item in latest.decision.findings if item.blocking] if latest else [],
            "unresolved_findings_round": latest.round if latest and any(item.blocking for item in latest.decision.findings) else None,
            "selected_quality_approved": approved(state.selected_round),
            "latest_quality_approved": approved(state.agent_state.history[-1].number if state.agent_state.history else None)}
