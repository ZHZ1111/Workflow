"""Shared application entry point for CLI and the local Web workbench."""

from pathlib import Path

from testgen_agent.models import GeneratedTests

from .agents.test_execution import TestExecutionAgent
from .agents.test_generation import TestGenerationAgent
from .config import WorkflowConfig
from .definitions import SUMMARY_WORKFLOW, TESTING_WORKFLOW
from .demo.client import FixtureClient
from .nodes.result_summary import ResultSummaryNode
from .registry import AgentRegistry
from .state import WorkflowState
from .workflow import WorkflowRunner, create_run

DATA_ROOT = Path(__file__).parent / "demo" / "data"
DEMO_FIXTURES = {
    "calculation": ("calculation_complete.json",),
    "validation": ("validation_round_01.json", "validation_round_02.json"),
    "discount": ("discount_defect.json",),
}


def sample_inputs() -> dict:
    """Bundled CLI/acceptance inputs; the Web form never preloads samples."""
    return {name: {"source": (DATA_ROOT / f"{name}.py").read_text(encoding="utf-8"),
                   "spec": (DATA_ROOT / f"{name}.md").read_text(encoding="utf-8")}
            for name in DEMO_FIXTURES}


def fixture_client(sample: str, fixtures: tuple[Path, ...] = ()) -> FixtureClient:
    paths = fixtures or tuple(DATA_ROOT / name for name in DEMO_FIXTURES.get(sample, ()))
    if not paths:
        raise ValueError("this source requires an explicit offline fixture")
    for path in paths:
        GeneratedTests.model_validate_json(path.read_text(encoding="utf-8"))
    return FixtureClient(paths)


def run_workflow(*, source: Path, specification: Path, output: Path,
                 config: WorkflowConfig, client, exact_output: bool = True,
                 with_summary: bool = False) -> WorkflowState:
    registry = AgentRegistry()
    registry.register("test_generation", TestGenerationAgent(client, client_mode=config.mode))
    registry.register("test_execution", TestExecutionAgent())
    if with_summary:
        registry.register("result_summary", ResultSummaryNode())
    state = create_run(source=source, specification=specification, output=output,
                       config=config, exact_output=exact_output)
    return WorkflowRunner(registry, SUMMARY_WORKFLOW if with_summary else TESTING_WORKFLOW).run(state)
