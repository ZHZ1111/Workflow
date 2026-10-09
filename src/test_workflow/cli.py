"""P1 command-line demo and read-only state inspection."""

import argparse
from pathlib import Path

from .demo.agents import FakeExecutionAgent, FakeGenerationAgent
from .definitions import DEMO_WORKFLOW
from .registry import AgentRegistry
from .state import StateStore
from .workflow import WorkflowRunner, create_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Testing Workflow — P1 offline node handoff")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Run two fake agents without model calls or pytest")
    demo.add_argument("--source", type=Path, required=True)
    demo.add_argument("--spec", type=Path, required=True)
    demo.add_argument("--output", type=Path, default=Path("outputs/p1"))
    demo.add_argument("--fixture", type=Path, help="GeneratedTests JSON compatible with the source; default: calculation")
    inspect = commands.add_parser("inspect", help="Validate and print persisted state; does not resume")
    inspect.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            state = StateStore(args.run_dir).load()
            print(state.canonical_json(indent=2))
            return 0
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
        print("Unable to read or validate inputs/state. Check paths and P1 input contracts.")
        return 2
