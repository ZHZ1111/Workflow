"""Reproduce P1 tests, fake-node handoff and versioned contract examples offline."""

from pathlib import Path
import json
import subprocess
import sys
import xml.etree.ElementTree as ET

from test_workflow.contracts import AgentContext, AgentResult, Message
from test_workflow.definitions import DEMO_WORKFLOW
from test_workflow.demo.agents import FakeExecutionAgent, FakeGenerationAgent
from test_workflow.registry import AgentRegistry
from test_workflow.state import StateStore, WorkflowState
from test_workflow.storage import atomic_write, file_digest, verify_artifact
from test_workflow.workflow import WorkflowRunner, create_run

ROOT = Path(__file__).resolve().parents[1]


class CapturingAgent:
    def __init__(self, inner):
        self.inner = inner
        self.context = None
        self.result = None

    def run(self, context):
        self.context = context
        self.result = self.inner.run(context)
        return self.result


def main() -> int:
    evaluation = ROOT / "evaluation"
    evaluation.mkdir(exist_ok=True)
    initial = create_run(source=ROOT / "examples/calculation.py",
                         specification=ROOT / "examples/calculation.md", output=ROOT / "outputs/p1")
    source_hash = file_digest(initial.agent_state.request.source_path)
    generation = CapturingAgent(FakeGenerationAgent())
    execution = CapturingAgent(FakeExecutionAgent())
    registry = AgentRegistry()
    registry.register("test_generation", generation)
    registry.register("test_execution", execution)
    final = WorkflowRunner(registry, DEMO_WORKFLOW).run(initial)
    assert final.workflow_status == "finished", final.error_code
    assert StateStore(final.run_dir).load() == final
    assert execution.context.inbox == generation.result.messages
    assert source_hash == file_digest(initial.agent_state.request.source_path)
    assert not (final.run_dir / "agent_workspace").exists()
    for artifact in final.artifacts:
        verify_artifact(final.run_dir, artifact)

    examples = ROOT / "examples/contracts/p1"
    examples.mkdir(parents=True, exist_ok=True)
    examples_by_name = {
        "generation_context": generation.context, "generation_result": generation.result,
        "execution_context": execution.context, "execution_result": execution.result,
        "message": final.messages[0], "state": final,
    }
    for name, value in examples_by_name.items():
        atomic_write(examples / f"{name}.json", value.canonical_json(indent=2) + "\n")
        assert type(value).model_validate_json(value.canonical_json()) == value
    schemas = examples / "schemas"
    schemas.mkdir(exist_ok=True)
    for model in (AgentContext, AgentResult, Message, WorkflowState):
        atomic_write(schemas / f"{model.__name__}.json",
                     json.dumps(model.model_json_schema(mode="validation"), ensure_ascii=False, indent=2) + "\n")

    test = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q",
                           "--basetemp", str(final.run_dir / "pytest-temp"),
                           "--junitxml", str(evaluation / "p1_tests.xml")],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    atomic_write(evaluation / "p1_tests.txt", test.stdout + test.stderr)
    suites = ET.parse(evaluation / "p1_tests.xml").getroot().findall("testsuite")
    tests = {name: sum(int(suite.attrib.get(name, 0)) for suite in suites)
             for name in ("tests", "failures", "errors", "skipped")}
    dependency = subprocess.run([sys.executable, "-m", "pip", "check"], cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
    evidence = {
        "phase": "P1", "mode": "demo", "date": "2026-10-09", "python": sys.version.split()[0],
        "run_id": final.run_id, "run_dir": str(final.run_dir.relative_to(ROOT)).replace("\\", "/"),
        "workflow_status": final.workflow_status, "task_status": final.task_status,
        "model_calls": 0, "candidate_pytest_executions": 0,
        "agents": [node.agent_id for node in DEMO_WORKFLOW.nodes],
        "messages": len(final.messages), "consumed_message_ids": list(final.consumed_message_ids),
        "last_event_seq": final.last_event_seq, "state_reloaded": True,
        "inner_workspace_absent": True, "source_unchanged": True,
        "tests": tests, "tests_exit_code": test.returncode,
        "pip_check_exit_code": dependency.returncode, "pip_check": dependency.stdout.strip(),
        "contract_examples": list(examples_by_name),
    }
    atomic_write(evaluation / "p1_results.json", json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(test.stdout.strip())
    print(f"P1 evidence: {evaluation / 'p1_results.json'}")
    print(f"Demo state: {final.run_dir / 'state.json'}")
    return 0 if test.returncode == dependency.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
