"""P1 defines a fixed sequential flow; feedback routing arrives in P4."""

from typing import Annotated

from pydantic import Field, model_validator
from testgen_agent.models import ContractModel

from .contracts import StableId


class NodeDefinition(ContractModel):
    agent_id: StableId
    receives_from: StableId | None = None


class WorkflowDefinition(ContractModel):
    name: StableId
    nodes: Annotated[tuple[NodeDefinition, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def ordered_dependencies(self):
        preceding = set()
        for node in self.nodes:
            if node.agent_id in preceding:
                raise ValueError("sequential node IDs must be unique")
            if node.receives_from is not None and node.receives_from not in preceding:
                raise ValueError("a dependency must precede its consumer")
            preceding.add(node.agent_id)
        return self


DEMO_WORKFLOW = WorkflowDefinition(name="p1_handoff", nodes=(
    NodeDefinition(agent_id="test_generation"),
    NodeDefinition(agent_id="test_execution", receives_from="test_generation"),
))
