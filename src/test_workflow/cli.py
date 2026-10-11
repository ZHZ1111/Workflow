"""Testing workflow CLI, P1 handoff demonstration and read-only inspection."""

import argparse
import json
from pathlib import Path

from testgen_agent.config import load_config
from testgen_agent.llm_client import DeepSeekClient
from testgen_agent.models import GeneratedTests, ModelInfo, TaskStatus

from .agents.test_generation import TestGenerationAgent
from .agents.test_execution import TestExecutionAgent
from .config import WorkflowConfig
from .demo.client import FixtureClient
from .demo.agents import FakeExecutionAgent, FakeGenerationAgent
from .definitions import DEMO_WORKFLOW, TESTING_WORKFLOW, SUMMARY_WORKFLOW
from .nodes.result_summary import ResultSummaryNode
from .reporting import inspect_summary
from .registry import AgentRegistry
from .state import StateStore
from .workflow import WorkflowRunner, create_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-agent testing workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run two fake agents without model calls or pytest")
    demo.add_argument("--source", type=Path, required=True)
    demo.add_argument("--spec", type=Path, required=True)
    demo.add_argument("--output", type=Path, default=Path("outputs/p1"))
    demo.add_argument("--fixture", type=Path, help="GeneratedTests JSON compatible with the source; default: calculation")
    run = commands.add_parser("run", help="Generate, execute, evaluate and revise up to three rounds")
    run.add_argument("--source", type=Path, required=True)
    run.add_argument("--spec", type=Path, required=True)
    run.add_argument("--output", type=Path, help="New exact run directory; existing paths are rejected")
    run.add_argument("--max-rounds", type=int, default=3, choices=range(1, 4))
    run.add_argument("--timeout", type=int, default=30, help="Per-round pytest timeout, 1..300 seconds")
    run.add_argument("--demo", action="store_true", help="Offline fixture generation with real pytest/coverage")
    run.add_argument("--fixture", type=Path, action="append", help="Offline JSON response, repeat in round order")
    run.add_argument("--env-file", type=Path, help="Explicit model settings file for real mode")
    run.add_argument("--with-summary", action="store_true", help="Register a model-free summary extension before export")
    inspect = commands.add_parser("inspect", help="Validate and print persisted state; does not resume")
    inspect.add_argument("run_dir", type=Path)
    inspect.add_argument("--summary", action="store_true", help="Verify saved evidence and print a read-only summary")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            state = StateStore(args.run_dir).load()
            print(json.dumps(inspect_summary(state), ensure_ascii=False, indent=2) if args.summary else state.canonical_json(indent=2))
            return 0
        if args.command == "run":
            if args.fixture and not args.demo:
                raise ValueError("fixtures require offline demo mode")
            if args.demo and args.env_file:
                raise ValueError("offline mode does not use environment files")
            config = WorkflowConfig(mode="offline" if args.demo else "real", max_rounds=args.max_rounds,
                                    execution_timeout_seconds=args.timeout)
            if args.demo:
                data = Path(__file__).parent / "demo" / "data"
                defaults = {
                    "calculation": ("calculation_complete.json",),
                    "validation": ("validation_round_01.json", "validation_round_02.json"),
                    "discount": ("discount_defect.json",),
                }
                names = defaults.get(args.source.stem)
                fixtures = tuple(args.fixture or (tuple(data / name for name in names) if names else ()))
                if not fixtures:
                    raise ValueError("this source requires an explicit offline fixture")
                for path in fixtures:
                    GeneratedTests.model_validate_json(path.read_text(encoding="utf-8"))
                client = FixtureClient(fixtures)
            else:
                model_config = load_config(env_file=args.env_file)
                config = config.updated(model=ModelInfo.from_config(model_config))
                client = DeepSeekClient(model_config)
            registry = AgentRegistry()
            registry.register("test_generation", TestGenerationAgent(client, client_mode="offline" if args.demo else "real"))
            registry.register("test_execution", TestExecutionAgent())
            if args.with_summary:
                registry.register("result_summary", ResultSummaryNode())
            state = create_run(source=args.source, specification=args.spec,
                               output=args.output if args.output is not None else Path("outputs/workflow"),
                               config=config, exact_output=args.output is not None)
            state = WorkflowRunner(registry, SUMMARY_WORKFLOW if args.with_summary else TESTING_WORKFLOW).run(state)
            print(f"run_id={state.run_id} mode={state.config.mode} workflow_status={state.workflow_status} task_status={state.task_status}")
            print(f"stop_reason={state.stop_reason} selected_round={state.selected_round}")
            print(f"report={state.run_dir / 'report.md'}")
            print(f"state={state.run_dir / 'state.json'}")
            if state.error_code:
                print(f"error_code={state.error_code}")
            return 0 if state.workflow_status == "finished" and state.task_status == TaskStatus.COMPLETED else 1
        registry = AgentRegistry()
        registry.register("test_generation", FakeGenerationAgent(args.fixture))
        registry.register("test_execution", FakeExecutionAgent())
        state = create_run(source=args.source, specification=args.spec, output=args.output)
        state = WorkflowRunner(registry, DEMO_WORKFLOW).run(state)
        print(f"mode=demo workflow_status={state.workflow_status} task_status=unmeasured")
        print(f"state={state.run_dir / 'state.json'}")
        if state.error_code:
            print(f"error_code={state.error_code}")
        return 0 if state.workflow_status == "finished" else 1
    except (OSError, ValueError, TypeError):
        # Do not echo exception text or input configuration into logs/console.
        print("Unable to read or validate inputs/configuration/state, or persist output. Check paths and workflow contracts.")
        return 2
