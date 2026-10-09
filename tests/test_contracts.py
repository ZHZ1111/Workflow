import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from test_workflow.config import WorkflowConfig
from test_workflow.contracts import AgentContext, ArtifactRef, Message, MessagePayload, StatePatch
from test_workflow.demo.agents import FakeGenerationAgent
from test_workflow.state import WorkflowState


def context(initial):
    state = initial.updated(workflow_status="running", current_node="test_generation", current_round=1)
    return AgentContext(run_id=state.run_id, round=1, agent_id="test_generation",
                        run_dir=state.run_dir, snapshot_json=state.canonical_json())


@pytest.mark.parametrize("path", ["../test.py", "/test.py", "C:/test.py", "C:test.py",
                                     "a\\test.py", "a/../test.py", "a//test.py", "./test.py", "a/file:stream"])
def test_artifacts_reject_nonrelative_paths(path):
    with pytest.raises(ValidationError):
        ArtifactRef(path=path, kind="tests", sha256="a" * 64, round=1, candidate_id="run-1:1")


def test_message_roundtrip_and_identity_validation(initial):
    result = FakeGenerationAgent().run(context(initial))
    message = result.messages[0]
    assert Message.model_validate_json(message.canonical_json()) == message
    assert json.loads(message.canonical_json())["payload"]["candidate_id"] == f"{initial.run_id}:1"
    with pytest.raises(ValidationError):
        message.updated(round=2)
    with pytest.raises(ValidationError):
        message.updated(run_id="another-run")
    with pytest.raises(ValidationError):
        MessagePayload(candidate_id="wrong", artifact=message.payload.artifact, reason="handoff")


@pytest.mark.parametrize("field,value", [
    ("run_id", "foreign"), ("history", []), ("run_dir", "other"),
    ("max_rounds", 99), ("request", {}), ("current_round", 2),
])
def test_nodes_cannot_patch_owned_metadata(field, value):
    with pytest.raises(ValidationError):
        StatePatch.model_validate({field: value})


def test_snapshot_is_deeply_isolated_even_with_vendor_dictionaries(initial):
    ctx = context(initial)
    result = FakeGenerationAgent().run(ctx)
    business = initial.agent_state.updated(history=(result.patch.candidate,), current_round=1)
    state = initial.updated(workflow_status="running", current_node="test_execution",
                            current_round=1, agent_state=business)
    ctx = AgentContext(run_id=state.run_id, round=1, agent_id="test_execution",
                       run_dir=state.run_dir, snapshot_json=state.canonical_json())
    local = ctx.state
    local.agent_state.history[0].generated.plan.scenarios[0].inputs.kwargs["probe"] = -999
    assert "probe" not in ctx.state.agent_state.history[0].generated.plan.scenarios[0].inputs.kwargs
    assert "probe" not in state.agent_state.history[0].generated.plan.scenarios[0].inputs.kwargs
    with pytest.raises(ValidationError):
        ctx.round = 2


def test_public_config_rejects_credentials_and_extra_fields():
    for value in ({"api_key": "secret"}, {"headers": {"Authorization": "secret"}},
                  {"max_rounds": 4}, {"max_rounds": True}, {"execution_timeout_seconds": 0}):
        with pytest.raises(ValidationError):
            WorkflowConfig.model_validate(value)


def test_state_roundtrip_and_metadata_constraints(initial):
    assert WorkflowState.model_validate_json(initial.canonical_json()) == initial
    with pytest.raises(ValidationError):
        initial.updated(run_id="different-run")
    with pytest.raises(ValidationError):
        initial.updated(current_round=4)
    with pytest.raises(ValidationError):
        initial.updated(task_status="completed")
    with pytest.raises(ValidationError):
        initial.updated(workflow_status="finished")
    with pytest.raises(ValidationError):
        initial.updated(run_dir=Path("relative"))
