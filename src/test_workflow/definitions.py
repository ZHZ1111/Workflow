"""Sequential nodes and explicit permitted feedback edges."""

from typing import Annotated

from pydantic import Field, model_validator
from testgen_agent.models import ContractModel

from .contracts import StableId


class NodeDefinition(ContractModel):
    agent_id: StableId
    receives_from: StableId | None = None


class FeedbackRoute(ContractModel):
    sender: StableId
    receiver: StableId


class WorkflowDefinition(ContractModel):
    name: StableId
    nodes: Annotated[tuple[NodeDefinition, ...], Field(min_length=1)]
    feedback_routes: tuple[FeedbackRoute, ...] = ()

    @model_validator(mode="after")
    def ordered_dependencies(self):
        preceding = set()
        for node in self.nodes:
            if node.agent_id in preceding:
                raise ValueError("sequential node IDs must be unique")
            if node.receives_from is not None and node.receives_from not in preceding:
                raise ValueError("a dependency must precede its consumer")
            preceding.add(node.agent_id)
        pairs = {(route.sender, route.receiver) for route in self.feedback_routes}
        if len(pairs) != len(self.feedback_routes) or any(
            route.sender not in preceding or route.receiver not in preceding or route.sender == route.receiver
            for route in self.feedback_routes
        ):
            raise ValueError("feedback routes must uniquely connect registered distinct nodes")
        return self


DEMO_WORKFLOW = WorkflowDefinition(name="p1_handoff", nodes=(
    NodeDefinition(agent_id="test_generation"),
    NodeDefinition(agent_id="test_execution", receives_from="test_generation"),
))

EVALUATION_WORKFLOW = DEMO_WORKFLOW.updated(name="p3_evaluation", feedback_routes=(
    FeedbackRoute(sender="test_execution", receiver="test_generation"),
))

TESTING_WORKFLOW = EVALUATION_WORKFLOW.updated(name="testing_workflow")

SUMMARY_WORKFLOW = TESTING_WORKFLOW.updated(name="testing_with_summary", nodes=(
    *TESTING_WORKFLOW.nodes, NodeDefinition(agent_id="result_summary"),
))

QUALITY_WORKFLOW = TESTING_WORKFLOW.updated(name="quality_workflow", nodes=(
    *TESTING_WORKFLOW.nodes, NodeDefinition(agent_id="test_quality_review", receives_from="test_execution"),
), feedback_routes=(
    *TESTING_WORKFLOW.feedback_routes, FeedbackRoute(sender="test_quality_review", receiver="test_generation"),
))

QUALITY_SUMMARY_WORKFLOW = QUALITY_WORKFLOW.updated(name="quality_with_summary", nodes=(
    *QUALITY_WORKFLOW.nodes, NodeDefinition(agent_id="result_summary"),
))
