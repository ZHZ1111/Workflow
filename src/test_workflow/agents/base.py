from typing import Protocol, runtime_checkable

from ..contracts import AgentContext, AgentResult


@runtime_checkable
class Agent(Protocol):
    def run(self, context: AgentContext) -> AgentResult:
        """Return proposals; only the runner commits shared state."""
        ...
