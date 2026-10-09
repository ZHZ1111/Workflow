"""Stable agent IDs map to instances; nodes do not create other nodes."""

from pydantic import TypeAdapter

from .agents.base import Agent
from .contracts import StableId


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, Agent] = {}

    def register(self, agent_id: str, agent: Agent) -> None:
        TypeAdapter(StableId).validate_python(agent_id)
        if agent_id in self._agents:
            raise ValueError("agent ID is already registered")
        if not isinstance(agent, Agent) or not callable(agent.run):
            raise TypeError("agent must implement run(context)")
        self._agents[agent_id] = agent

    def get(self, agent_id: str) -> Agent:
        try:
            return self._agents[agent_id]
        except KeyError:
            raise ValueError(f"agent is not registered: {agent_id}") from None
