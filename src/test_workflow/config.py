"""Public workflow settings. Credentials stay in model-client configuration."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt
from testgen_agent.models import ContractModel, ModelInfo, RoundNumber


class WorkflowConfig(ContractModel):
    mode: Literal["demo", "offline", "real"] = "demo"
    max_rounds: RoundNumber = 3
    execution_timeout_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 60
    model: ModelInfo | None = None
    quality_review_enabled: StrictBool = False
