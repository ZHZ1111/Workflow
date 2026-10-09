import json

import pytest

from test_workflow.contracts import AgentResult
from test_workflow.definitions import DEMO_WORKFLOW, NodeDefinition, WorkflowDefinition
from test_workflow.demo.agents import DemoObservation, FakeExecutionAgent, FakeGenerationAgent
from test_workflow.events import Event
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest, verify_artifact
from test_workflow.workflow import WorkflowRunner


def runner(generation=None, execution=None, definition=DEMO_WORKFLOW):
    registry = AgentRegistry()
    registry.register("test_generation", generation or FakeGenerationAgent())
    registry.register("test_execution", execution or FakeExecutionAgent())
    return WorkflowRunner(registry, definition)


class RecordingExecution(FakeExecutionAgent):
    def __init__(self):
        self.calls = 0
        self.context = None

    def run(self, context):
        self.calls += 1
        self.context = context
        return super().run(context)


def test_two_nodes_consume_actual_output_and_persist(initial):
    source_before = file_digest(initial.agent_state.request.source_path)
    execution = RecordingExecution()
    final = runner(execution=execution).run(initial)
    assert final.workflow_status == "finished"
    assert final.task_status is None
    assert final.agent_state.api_calls == 0
    assert final.agent_state.history[0].execution is None
    assert final.agent_state.history[0].coverage is None
    assert execution.calls == 1
    assert execution.context.inbox == final.messages
    assert final.consumed_message_ids == (final.messages[0].message_id,)
    assert StateStore(final.run_dir).load() == final
    assert not (final.run_dir / "agent_workspace").exists()
    assert file_digest(initial.agent_state.request.source_path) == source_before
    observation_ref = next(item for item in final.artifacts if item.kind == "demo_observation")
    observation = DemoObservation.model_validate_json(verify_artifact(final.run_dir, observation_ref).read_text(encoding="utf-8"))
    assert not observation.pytest_executed and not observation.model_called
    assert observation.candidate_sha256 == final.messages[0].payload.artifact.sha256
    messages = [json.loads(line) for line in (final.run_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert messages == [json.loads(item.canonical_json()) for item in final.messages]
    events = [Event.model_validate_json(line) for line in (final.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [event.seq for event in events] == list(range(1, final.last_event_seq + 1))
    assert [event.node for event in events if event.type == "node_finished"] == ["test_generation", "test_execution"]


class AlteredGeneration(FakeGenerationAgent):
    def __init__(self, mutate):
        super().__init__()
        self.mutate = mutate

    def run(self, context):
        return self.mutate(super().run(context))


@pytest.mark.parametrize("mutate", [
    lambda result: result.updated(run_id="foreign-run"),
    lambda result: result.updated(round=2),
    lambda result: result.updated(agent_id="another-node"),
    lambda result: result.updated(messages=(result.messages[0].updated(receiver="unknown"),)),
    lambda result: result.updated(messages=(result.messages[0].updated(sender="another-node"),)),
    lambda result: result.updated(messages=(result.messages[0], result.messages[0])),
    lambda result: result.updated(artifacts=()),
    lambda result: result.model_copy(update={"round": 99}),
    lambda result: {"run_id": result.run_id},
])
def test_bad_handoff_is_rejected_before_consumer_and_saved(initial, mutate):
    execution = RecordingExecution()
    final = runner(AlteredGeneration(mutate), execution).run(initial)
    assert final.workflow_status == "error"
    assert final.error_code == "invalid_handoff"
    assert execution.calls == 0
    assert final.messages == () and final.agent_state.history == ()
    assert StateStore(final.run_dir).load() == final


def test_node_exception_preserves_generator_evidence_without_secrets(initial):
    class Failing:
        def run(self, context):
            raise RuntimeError("Authorization: Bearer secret-value API_KEY=secret-value")
    final = runner(execution=Failing()).run(initial)
    assert final.workflow_status == "error"
    assert final.error_code == "node_exception"
    assert len(final.agent_state.history) == 1
    assert len(final.messages) == 1
    assert final.consumed_message_ids == ()
    assert StateStore(final.run_dir).load() == final
    for name in ("state.json", "events.jsonl", "messages.jsonl"):
        assert "secret-value" not in (final.run_dir / name).read_text(encoding="utf-8")


def test_content_tampering_stops_handoff(initial):
    class Tampering(FakeGenerationAgent):
        def run(self, context):
            result = super().run(context)
            (context.run_dir / result.artifacts[0].path).write_text("{}", encoding="utf-8")
            return result
    execution = RecordingExecution()
    final = runner(Tampering(), execution).run(initial)
    assert final.workflow_status == "error" and execution.calls == 0


def test_rehashed_candidate_file_cannot_disagree_with_state(initial):
    class Mismatched(FakeGenerationAgent):
        def run(self, context):
            result = super().run(context)
            reference = result.artifacts[0]
            path = context.run_dir / reference.path
            candidate = result.patch.candidate.updated(test_path=None)
            atomic_write(path, candidate.canonical_json())
            altered = reference.updated(sha256=file_digest(path))
            message = result.messages[0]
            return result.updated(artifacts=(altered, result.artifacts[1]), messages=(
                message.updated(payload=message.payload.updated(artifact=altered)),))
    execution = RecordingExecution()
    final = runner(Mismatched(), execution).run(initial)
    assert final.workflow_status == "error" and execution.calls == 0


def test_required_handoff_cannot_be_skipped(initial):
    class Silent:
        def run(self, context):
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id)
    execution = RecordingExecution()
    final = runner(Silent(), execution).run(initial)
    assert final.workflow_status == "error" and execution.calls == 0


def test_registry_and_definition_reject_invalid_wiring():
    registry = AgentRegistry()
    registry.register("test_generation", FakeGenerationAgent())
    with pytest.raises(ValueError):
        registry.register("test_generation", FakeGenerationAgent())
    with pytest.raises(TypeError):
        registry.register("bad", object())
    with pytest.raises(ValueError):
        WorkflowRunner(registry, DEMO_WORKFLOW)
    with pytest.raises(ValueError):
        WorkflowDefinition(name="bad", nodes=(NodeDefinition(agent_id="test_execution", receives_from="test_generation"),))


def test_new_node_only_requires_registration_and_definition(initial):
    class Summary:
        def __init__(self):
            self.called = False
        def run(self, context):
            self.called = True
            assert any(item.kind == "demo_observation" for item in context.state.artifacts)
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id)
    node = Summary()
    registry = AgentRegistry()
    registry.register("test_generation", FakeGenerationAgent())
    registry.register("test_execution", FakeExecutionAgent())
    registry.register("summary", node)
    definition = DEMO_WORKFLOW.updated(nodes=(*DEMO_WORKFLOW.nodes, NodeDefinition(agent_id="summary")))
    final = WorkflowRunner(registry, definition).run(initial)
    assert final.workflow_status == "finished" and node.called
    with pytest.raises(ValueError):
        WorkflowRunner(registry, definition).run(final)
