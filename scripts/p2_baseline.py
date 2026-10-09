"""P2 single-node acceptance harness. Execution occurs only outside the node.

This manually supplies measured evidence to revisions; it is not P4 feedback routing.
No .env, real account keys or external service requests are used.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from testgen_agent.agent import choose_action
from testgen_agent.generation import preserved_tests
from testgen_agent.models import AgentAction
from testgen_agent.tools import run_tests_with_coverage

from test_workflow.agents.test_generation import TestGenerationAgent
from test_workflow.contracts import AgentContext, AgentResult, Message, MessagePayload
from test_workflow.definitions import DEMO_WORKFLOW
from test_workflow.demo.agents import FakeExecutionAgent
from test_workflow.demo.client import FixtureClient
from test_workflow.events import EventWriter, MessageWriter
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def invoke(state, agent, number=1, inbox=()):
    state = state.updated(workflow_status="running", current_node="test_generation", current_round=number)
    events = EventWriter(state.run_dir, state.run_id)
    if not events.seq:
        events.append("workflow_started")
    events.append("node_started", node="test_generation", round=number)
    state = state.updated(last_event_seq=events.seq)
    StateStore(state.run_dir).save(state)
    context = AgentContext(run_id=state.run_id, round=number, agent_id="test_generation", run_dir=state.run_dir,
                           snapshot_json=state.canonical_json(), inbox=inbox)
    with patch("subprocess.run", side_effect=AssertionError("generation must not launch subprocesses")), \
         patch("subprocess.Popen", side_effect=AssertionError("generation must not launch subprocesses")):
        result = agent.run(context)
    assert result.status == "ok", result.error_code
    registry = AgentRegistry()
    registry.register("test_generation", agent)
    registry.register("test_execution", FakeExecutionAgent())
    state = WorkflowRunner(registry, DEMO_WORKFLOW).apply_result(state, result, tuple(item.message_id for item in inbox))
    for message in result.messages:
        MessageWriter(state.run_dir).append(message)
        events.append("message_sent", node="test_generation", round=number, message_id=message.message_id)
    events.append("node_finished", node="test_generation", round=number)
    state = state.updated(current_node=None, last_event_seq=events.seq)
    StateStore(state.run_dir).save(state)
    directory = state.run_dir / "node_results" / f"{number:02d}"
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write(directory / "context.json", context.canonical_json(indent=2) + "\n")
    atomic_write(directory / "result.json", result.canonical_json(indent=2) + "\n")
    return state, context, result


def add_measured_feedback(state, first):
    # This is a harness tool call, deliberately outside TestGenerationAgent.run().
    measured = run_tests_with_coverage(state.agent_state.request.output_dir, round_number=1)
    candidate = first.patch.candidate.updated(execution=measured.execution, coverage=measured.coverage)
    business = state.agent_state.updated(history=(candidate,))
    decision = choose_action(business)
    assert decision.action == AgentAction.ADD_MISSING_CASES
    business = business.updated(next_action=decision.action)
    feedback = Message(message_id="feedback-01", run_id=state.run_id, round=1,
                       sender="test_execution", receiver="test_generation", type="execution_feedback",
                       payload=MessagePayload(candidate_id=f"{state.run_id}:1", artifact=first.messages[0].payload.artifact,
                                              reason="Harness measured missing coverage; preserve passing tests.",
                                              recommended_action=decision.action))
    state = state.updated(agent_state=business, messages=(*state.messages, feedback))
    MessageWriter(state.run_dir).append(feedback)
    StateStore(state.run_dir).save(state)
    return state, feedback, measured


def main():
    samples = {}
    exports = {}
    for name, source, fixtures in (
        ("valid_initial", "calculation", ("calculation_complete.json",)),
        ("invalid_syntax", "calculation", ("calculation_invalid_syntax.json",)),
        ("valid_revision", "validation", ("validation_round_01.json", "validation_round_02.json")),
        ("removed_passing_test", "validation", ("validation_round_01.json", "validation_removed_pass.json")),
        ("weakened_passing_test", "validation", ("validation_round_01.json", "validation_weakened_pass.json")),
    ):
        state = create_run(source=ROOT / f"examples/{source}.py", specification=ROOT / f"examples/{source}.md",
                           output=ROOT / "outputs/p2")
        source_hash = file_digest(state.agent_state.request.source_path)
        client = FixtureClient(tuple(FIXTURES / fixture for fixture in fixtures))
        agent = TestGenerationAgent(client, client_mode="offline")
        state, context, first = invoke(state, agent)
        result = first
        measured = None
        protected = []
        if len(fixtures) == 2:
            state, feedback, measured = add_measured_feedback(state, first)
            protected = sorted(preserved_tests(state.agent_state.history))
            state, context, result = invoke(state, agent, 2, (feedback,))
        assert result.patch.candidate.execution is None
        error = result.patch.candidate.validation_error
        if name == "invalid_syntax":
            assert error.startswith("test_syntax") and not result.messages
        elif name in {"removed_passing_test", "weakened_passing_test"}:
            assert error.startswith("preserve_tests") and not result.messages
        else:
            assert error is None and result.messages
        assert StateStore(state.run_dir).load() == state
        assert file_digest(state.agent_state.request.source_path) == source_hash
        samples[name] = {
            "run_dir": state.run_dir.relative_to(ROOT).as_posix(), "candidate_round": result.round,
            "validation_error": error, "ready_messages": len(result.messages),
            "client_calls": len(client.contexts), "external_model_requests": 0,
            "generation_subprocess_calls": 0, "harness_pytest_executions": int(measured is not None),
            "protected_tests": protected, "source_unchanged": True,
            "prior_execution_kind": measured.execution.kind if measured else None,
            "prior_line_percent": measured.coverage.line_percent if measured else None,
        }
        exports[f"{name}_context"] = context
        exports[f"{name}_result"] = result

    examples = ROOT / "examples/contracts/p2"
    examples.mkdir(parents=True, exist_ok=True)
    for name, value in exports.items():
        atomic_write(examples / f"{name}.json", value.canonical_json(indent=2) + "\n")
        assert type(value).model_validate_json(value.canonical_json()) == value
    schemas = examples / "schemas"
    schemas.mkdir(exist_ok=True)
    for model in (AgentContext, AgentResult):
        atomic_write(schemas / f"{model.__name__}.json", json.dumps(model.model_json_schema(mode="validation"),
                     ensure_ascii=False, indent=2) + "\n")
    evidence = {"phase": "P2", "mode": "offline", "date": datetime.now(timezone(timedelta(hours=8))).date().isoformat(),
                "external_model_requests": 0, "generation_subprocess_calls": 0,
                "harness_pytest_executions": 3, "samples": samples,
                "note": "Harness supplies measurements manually; execution Agent and P4 routing are pending."}
    atomic_write(ROOT / "evaluation/p2_results.json", json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print("P2 single-node acceptance: 5 cases passed; generation subprocess calls=0; external requests=0")
    print(f"Evidence: {ROOT / 'evaluation/p2_results.json'}")


if __name__ == "__main__":
    main()
