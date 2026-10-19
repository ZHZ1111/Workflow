"""P3 offline generation + real single-candidate execution acceptance.

This harness manually invokes nodes, including one revision. It does not
implement P4 scheduling, read credentials or request an external model.
"""

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from test_workflow.agents.test_generation.core.tools import run_tests_with_coverage
from test_workflow.adapters.execution import evaluate
from test_workflow.agents.test_execution import TestExecutionAgent
from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext, AgentResult, ExecutionAssessment, ExecutionFeedback, Message
from test_workflow.definitions import EVALUATION_WORKFLOW
from test_workflow.demo.client import FixtureClient
from test_workflow.events import EventWriter, MessageWriter
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest, verify_artifact
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def invoke(state, agent, runner, node, number=1, inbox=()):
    state = state.updated(workflow_status="running", current_node=node, current_round=number)
    events = EventWriter(state.run_dir, state.run_id)
    if not events.seq:
        events.append("workflow_started")
    events.append("node_started", node=node, round=number)
    state = state.updated(last_event_seq=events.seq)
    StateStore(state.run_dir).save(state)
    ctx = AgentContext(run_id=state.run_id, round=number, agent_id=node, run_dir=state.run_dir,
                       snapshot_json=state.canonical_json(), inbox=inbox)
    result = agent.run(ctx)
    assert result.status == "ok", result.error_code
    state = runner.apply_result(ctx.state, result, tuple(item.message_id for item in inbox))
    for message in result.messages:
        MessageWriter(state.run_dir).append(message)
        events.append("message_sent", node=node, round=number, message_id=message.message_id)
    events.append("node_finished", node=node, round=number)
    state = state.updated(current_node=None, last_event_seq=events.seq)
    StateStore(state.run_dir).save(state)
    directory = state.run_dir / "node_results" / f"{number:02d}" / node
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write(directory / "context.json", ctx.canonical_json(indent=2) + "\n")
    atomic_write(directory / "result.json", result.canonical_json(indent=2) + "\n")
    return state, ctx, result


def main():
    output = ROOT / "outputs/p3"
    output.mkdir(parents=True, exist_ok=True)
    exports = ROOT / "examples/contracts/p3"
    exports.mkdir(parents=True, exist_ok=True)
    samples = {}
    processes = []
    popen = subprocess.Popen
    def record(*args, **kwargs):
        process = popen(*args, **kwargs)
        if "pytest" in args[0]:
            processes.append(process)
        return process

    for name, source, fixtures, kind, recommendation in (
        ("passed", "calculation", ("calculation_complete.json",), "passed", "finish"),
        ("low_coverage_revision", "validation", ("validation_round_01.json", "validation_round_02.json"), "passed", "finish"),
        ("invalid_syntax", "calculation", ("calculation_invalid_syntax.json",), None, "repair_tests"),
        ("no_tests_rejected", "calculation", ("p3/no_tests_rejected.json",), None, "repair_tests"),
        ("ambiguous_assertion", "calculation", ("p3/ambiguous_assertion.json",), "assertion_failed", "request_review"),
        ("name_error", "calculation", ("p3/name_error.json",), "execution_error", "repair_tests"),
        ("import_error", "calculation", ("p3/import_error.json",), "collection_error", "repair_tests"),
        ("collection_error", "calculation", ("p3/collection_error.json",), "collection_error", "request_review"),
        ("suspected_defect", "discount", ("discount_defect.json",), "assertion_failed", "request_review"),
        ("timeout", "execution/timeout", ("calculation_complete.json",), "timeout", "stop_error"),
        ("coverage_export_failure", "calculation", ("calculation_complete.json",), "passed", "stop_error"),
    ):
        state = create_run(source=ROOT / f"examples/{source}.py", specification=ROOT / f"examples/{source}.md",
                           output=output, config=WorkflowConfig(execution_timeout_seconds=2 if name == "timeout" else 30))
        digest = file_digest(state.agent_state.request.source_path)
        client = FixtureClient(tuple(FIXTURES / value for value in fixtures))
        generator, executor = TestGenerationAgent(client, client_mode="offline"), TestExecutionAgent()
        registry = AgentRegistry()
        registry.register("test_generation", generator)
        registry.register("test_execution", executor)
        runner = WorkflowRunner(registry, EVALUATION_WORKFLOW)
        rounds = []
        feedback = ()
        for number in range(1, len(fixtures) + 1):
            with patch("subprocess.Popen", side_effect=AssertionError("generation cannot execute pytest")):
                state, _, gr = invoke(state, generator, runner, "test_generation", number, feedback)
            fault = patch("test_workflow.agents.test_generation.core.tools.coverage_runner.subprocess.run", side_effect=OSError("controlled export fault")) if name == "coverage_export_failure" else nullcontext()
            with fault, patch("test_workflow.agents.test_generation.core.tools.test_runner.subprocess.Popen", side_effect=record):
                state, ctx, result = invoke(state, executor, runner, "test_execution", number, gr.messages)
            assessment = result.assessment
            rounds.append({"round": number, "tool_calls": assessment.tool_calls, "pytest_started": assessment.pytest_started,
                           "execution_kind": assessment.feedback.execution.kind if assessment.feedback.execution else None,
                           "passed": assessment.feedback.execution.passed if assessment.feedback.execution else None,
                           "failed": assessment.feedback.execution.failed if assessment.feedback.execution else None,
                           "coverage_status": assessment.feedback.coverage.status if assessment.feedback.coverage else None,
                           "line_percent": assessment.feedback.coverage.line_percent if assessment.feedback.coverage else None,
                           "branch_percent": assessment.feedback.coverage.branch_percent if assessment.feedback.coverage else None,
                           "recommendation": assessment.recommendation, "task_status_advice": assessment.task_status,
                           "protected_tests": list(assessment.feedback.protected_tests),
                           "issue_kinds": [issue.kind for issue in assessment.feedback.issues],
                           "assessment_path": f"evaluations/{number:02d}/assessment.json"})
            feedback = result.messages
            if number < len(fixtures):
                assert assessment.recommendation == "extend_tests" and feedback
        assert rounds[-1]["execution_kind"] == kind and assessment.recommendation == recommendation
        assert file_digest(state.agent_state.request.source_path) == digest
        assert state.task_status is None and StateStore(state.run_dir).load() == state
        for reference in result.artifacts:
            verify_artifact(state.run_dir, reference)
        samples[name] = {"run_dir": state.run_dir.relative_to(ROOT).as_posix(), "rounds": rounds,
                         "external_model_requests": 0, "source_unchanged": True,
                         "fault_injection": "coverage_export_OSError" if name == "coverage_export_failure" else None,
                         "pytest_processes": sum(item["pytest_started"] for item in rounds)}
        for suffix, value in (("context", ctx), ("result", result), ("assessment", assessment)):
            atomic_write(exports / f"{name}_{suffix}.json", value.canonical_json(indent=2) + "\n")
        if result.messages:
            atomic_write(exports / f"{name}_message.json", result.messages[0].canonical_json(indent=2) + "\n")
        if name == "passed":
            passed_context = ctx
        print(f"{name}: {kind or 'not_started'} -> {recommendation}", flush=True)

    # Real manual backend observation of an empty test file. Production rejects
    # this candidate first; no gate is weakened for this diagnostic case.
    workspace = create_run(source=ROOT / "examples/calculation.py", specification=ROOT / "examples/calculation.md", output=output).run_dir / "manual_no_tests"
    (workspace / "rounds/01").mkdir(parents=True)
    (workspace / "subject.py").write_bytes((ROOT / "examples/calculation.py").read_bytes())
    (workspace / "rounds/01/test_subject.py").write_text("# deliberately empty\n", encoding="utf-8")
    with patch("test_workflow.agents.test_generation.core.tools.test_runner.subprocess.Popen", side_effect=record):
        observed = run_tests_with_coverage(workspace)
    candidate = passed_context.state.agent_state.history[-1].updated(execution=observed.execution, coverage=observed.coverage)
    manual = evaluate(passed_context, candidate, None, (), tool_calls=1, observation_mode="injected")
    assert observed.execution.kind == "no_tests" and manual.recommendation == "repair_tests"
    atomic_write(workspace / "assessment.json", manual.canonical_json(indent=2) + "\n")
    samples["manual_no_tests_backend"] = {"workspace": workspace.relative_to(ROOT).as_posix(), "execution_kind": "no_tests",
                                          "collected": observed.execution.collected, "recommendation": manual.recommendation,
                                          "observation_mode": "injected", "pytest_processes": 1}
    assert all(process.poll() is not None for process in processes)
    for model in (AgentContext, AgentResult, ExecutionAssessment, ExecutionFeedback, Message):
        atomic_write(exports / f"{model.__name__}.schema.json", json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
    summary = {"phase": "P3", "generation_mode": "offline", "generation_subprocess_calls": 0,
               "date": datetime.now(timezone(timedelta(hours=8))).isoformat(), "external_model_requests": 0,
               "pytest_processes": len(processes), "all_pytest_processes_terminated": True,
               "automatic_feedback_scheduler": False, "samples": samples}
    atomic_write(ROOT / "evaluation/p3_results.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"P3 acceptance: {len(samples)} cases; {len(processes)} real pytest processes; all terminated.")


if __name__ == "__main__":
    main()
