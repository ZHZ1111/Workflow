"""P4 acceptance through the public CLI: offline generation, real execution."""

import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import xml.etree.ElementTree as ET

from test_workflow.contracts import AgentContext, AgentResult, ExecutionAssessment, Message
from test_workflow.events import Event
from test_workflow.state import StateStore, WorkflowState
from test_workflow.storage import atomic_write, file_digest, verify_artifact

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def invoke(command):
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")


def run_checks(root):
    checks = {}
    for name, targets in (
        ("tests", ["tests"]),
        ("vendor_tests", ["tests/agentdev/test_agent.py", "tests/agentdev/test_generation.py"]),
    ):
        xml_path = ROOT / f"evaluation/p4_{name}.xml"
        command = [sys.executable, "-m", "pytest", *targets, "-q", "--basetemp", str(root / name), "--junitxml", str(xml_path)]
        result = invoke(command)
        atomic_write(ROOT / f"evaluation/p4_{name}.txt", result.stdout + result.stderr)
        suites = ET.parse(xml_path).getroot().findall("testsuite")
        checks[name] = {field: sum(int(suite.attrib.get(field, 0)) for suite in suites)
                        for field in ("tests", "failures", "errors", "skipped")}
        checks[name]["exit_code"] = result.returncode
        print(result.stdout.strip(), flush=True)
    return checks


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--skip-tests", action="store_true", help="Only reproduce the independent CLI cases")
    mode.add_argument("--tests-only", action="store_true", help="Refresh checks for the existing CLI evidence")
    args = parser.parse_args()
    root = ROOT / "outputs/p4" / f"acceptance-{uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=False)
    if args.tests_only:
        path = ROOT / "evaluation/p4_results.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["checks"] = run_checks(root)
        atomic_write(path, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        return 0 if all(item["exit_code"] == 0 for item in evidence["checks"].values()) else 1
    exports = ROOT / "examples/contracts/p4"
    exports.mkdir(parents=True, exist_ok=True)
    samples = {}
    cases = (
        ("passed", "calculation", ("calculation_complete.json",), 3, "completed", "coverage_target_met", 1, 1),
        ("feedback", "validation", ("validation_round_01.json", "validation_round_02.json"), 3, "completed", "coverage_target_met", 2, 2),
        ("syntax_repaired", "calculation", ("calculation_invalid_syntax.json", "calculation_complete.json"), 3, "completed", "coverage_target_met", 2, 2),
        ("name_repaired", "calculation", ("p3/name_error.json", "calculation_complete.json"), 3, "completed", "coverage_target_met", 2, 2),
        ("three_round_limit", "calculation", ("calculation_invalid_syntax.json", "p4/invalid_round_02.json", "p4/invalid_round_03.json"), 3, "failed", "round_limit_reached", 3, None),
        ("partial_limit", "validation", ("validation_round_01.json",), 1, "partial", "round_limit_reached", 1, 1),
        ("duplicate", "validation", ("validation_round_01.json", "validation_round_01.json"), 3, "partial", "no_progress", 2, 1),
        ("no_coverage_progress", "validation", ("validation_round_01.json", "p4/no_coverage_progress.json"), 3, "partial", "no_progress", 2, 2),
        ("removed_passing_test", "validation", ("validation_round_01.json", "validation_removed_pass.json"), 2, "partial", "round_limit_reached", 2, 1),
        ("weakened_passing_test", "validation", ("validation_round_01.json", "validation_weakened_pass.json"), 2, "partial", "round_limit_reached", 2, 1),
        ("later_execution_error", "validation", ("validation_round_01.json", "p4/later_name_error.json"), 2, "partial", "round_limit_reached", 2, 1),
        ("suspected_defect", "discount", ("discount_defect.json",), 3, "needs_review", "suspected_source_defect", 1, 1),
        ("ambiguous_assertion", "calculation", ("p3/ambiguous_assertion.json",), 3, "needs_review", "ambiguous_specification", 1, 1),
        ("timeout", "execution/timeout", ("calculation_complete.json",), 3, "failed", "execution_timeout", 1, None),
    )
    for name, source, fixtures, budget, status, stop, rounds, selected in cases:
        directory = root / name
        source_path, spec_path = ROOT / f"examples/{source}.py", ROOT / f"examples/{source}.md"
        before = (file_digest(source_path), file_digest(spec_path))
        command = [sys.executable, "-m", "test_workflow", "run", "--demo", "--source", str(source_path),
                   "--spec", str(spec_path), "--output", str(directory), "--max-rounds", str(budget),
                   "--timeout", "1" if name == "timeout" else "30"]
        for fixture in fixtures:
            command += ["--fixture", str(FIXTURES / fixture)]
        result = invoke(command)
        atomic_write(root / f"{name}_cli.txt", result.stdout + result.stderr)
        assert result.returncode == (0 if status == "completed" else 1), (name, result.stdout)
        state = StateStore(directory).load()
        assert (state.workflow_status, state.task_status, state.stop_reason,
                len(state.agent_state.history), state.selected_round) == ("finished", status, stop, rounds, selected), name
        assert before == (file_digest(source_path), file_digest(spec_path)), name
        assert state.agent_state.api_calls == 0
        for reference in state.artifacts:
            verify_artifact(directory, reference)
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        if selected:
            assert file_digest(Path(summary["final_test_file"])) == file_digest(state.agent_state.history[selected - 1].test_path)
        contexts, assessments = [], []
        for number in range(1, rounds + 1):
            contexts.append(AgentContext.model_validate_json((directory / f"node_results/{number:02d}/test_generation/context.json").read_text(encoding="utf-8")))
            assessments.append(ExecutionAssessment.model_validate_json((directory / f"evaluations/{number:02d}/assessment.json").read_text(encoding="utf-8")))
            if number > 1:
                feedback = contexts[-1].inbox[0]
                assert feedback.round == number - 1 and feedback.type == "execution_feedback"
                assert feedback.payload.feedback == assessments[-2].feedback
                assert feedback.message_id in state.consumed_message_ids
        samples[name] = {
            "run_id": state.run_id, "run_dir": directory.relative_to(ROOT).as_posix(),
            "cli_exit_code": result.returncode, "workflow_status": state.workflow_status,
            "task_status": state.task_status, "stop_reason": state.stop_reason,
            "selected_round": selected, "selection_reason": summary["selection_reason"],
            "selected_all_passed": summary["selected_all_passed"], "latest_all_passed": summary["latest_all_passed"],
            "generation_calls": summary["generation_calls"], "external_requests": summary["external_requests"],
            "pytest_processes": sum(item.pytest_started for item in assessments),
            "source_unchanged": True, "spec_unchanged": True,
            "rounds": [{"round": item.number, "validation_error": item.validation_error,
                        "execution_kind": item.execution.kind if item.execution else None,
                        "passed": item.execution.passed if item.execution else None,
                        "failed": item.execution.failed if item.execution else None,
                        "line_percent": item.coverage.line_percent if item.coverage else None,
                        "branch_percent": item.coverage.branch_percent if item.coverage else None,
                        "action": assessment.action, "stop_reason": assessment.stop_reason}
                       for item, assessment in zip(state.agent_state.history, assessments)],
            "report": (directory / "report.md").relative_to(ROOT).as_posix(),
        }
        if name in {"passed", "feedback", "later_execution_error"}:
            atomic_write(exports / f"{name}_state.json", state.canonical_json(indent=2) + "\n")
        if name == "feedback":
            atomic_write(exports / "revision_context.json", contexts[1].canonical_json(indent=2) + "\n")
            feedback = contexts[1].inbox[0]
            atomic_write(exports / "feedback_message.json", feedback.canonical_json(indent=2) + "\n")
        print(f"{name}: {status}/{stop}; rounds={rounds}; selected={selected}", flush=True)
    for model in (AgentContext, AgentResult, ExecutionAssessment, Message, WorkflowState, Event):
        atomic_write(exports / f"{model.__name__}.schema.json", json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
    manifest = json.loads((ROOT / "evaluation/reuse_manifest.json").read_text(encoding="utf-8"))
    from vendor_audit import audit_vendor
    vendor_audit = audit_vendor(ROOT)
    assert vendor_audit["verified"]
    checks = {}
    if not args.skip_tests:
        checks = run_checks(root)
    evidence = {"phase": "P4", "date": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                "python": sys.version.split()[0], "generation_mode": "offline", "execution_mode": "real",
                "automatic_feedback_scheduler": True, "external_model_requests": 0,
                "pytest_processes": sum(item["pytest_processes"] for item in samples.values()),
                "vendor_manifest_files": len(manifest["files"]),
                "vendor_files_unchanged": vendor_audit["unchanged"],
                "vendor_files_verified": vendor_audit["verified"], "checks": checks, "samples": samples}
    atomic_write(ROOT / "evaluation/p4_results.json", json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return 0 if all(item["exit_code"] == 0 for item in checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
