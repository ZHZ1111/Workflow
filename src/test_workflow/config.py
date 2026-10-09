"""Public workflow settings. Credentials belong only to future model adapters."""

from typing import Annotated, Literal

from pydantic import Field, StrictInt
from testgen_agent.models import ContractModel, ModelInfo, RoundNumber


class WorkflowConfig(ContractModel):
    mode: Literal["demo", "real"] = "demo"
    max_rounds: RoundNumber = 3
    execution_timeout_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 30
    model: ModelInfo | None = None
