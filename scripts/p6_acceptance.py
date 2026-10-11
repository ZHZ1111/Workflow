"""Reproduce P6 reports/extension with offline CLI and real local execution."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote
from uuid import uuid4
import xml.etree.ElementTree as ET

from test_workflow.contracts import AgentResult
from test_workflow.events import Event
from test_workflow.reporting import inspect_summary
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "evaluation/p6_results.json"
CASES = (
    ("completed", "calculation", ("calculation_complete.json",), 3, "completed", 1),
    ("feedback", "validation", ("validation_round_01.json", "validation_round_02.json"), 3, "completed", 2),
    ("history", "validation", ("validation_round_01.json", "p4/later_name_error.json"), 2, "partial", 1),
    ("review", "discount", ("discount_defect.json",), 3, "needs_review", 1),
    ("rejected", "calculation", ("calculation_invalid_syntax.json",), 1, "failed", None),
)


def relative(path):
    return path.relative_to(ROOT).as_posix()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--tests-only", action="store_true")
    args = parser.parse_args()
    if args.skip_tests and args.tests_only:
        parser.error("--skip-tests and --tests-only are mutually exclusive")
    root = ROOT / "outputs/p6" / f"acceptance-{uuid4().hex[:10]}"
    root.mkdir(parents=True, exist_ok=False)
    evidence = json.loads(INDEX.read_text(encoding="utf-8")) if args.tests_only else {"phase": "P6", "cases": {}}
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if not args.tests_only:
        for name, source, fixtures, budget, status, selected in CASES:
            directory = root / name
            inputs = (ROOT / f"examples/{source}.py", ROOT / f"examples/{source}.md")
            input_hashes = [file_digest(path) for path in inputs]
            command = [sys.executable, "-m", "test_workflow", "run", "--demo", "--with-summary",
                       "--source", f"examples/{source}.py", "--spec", f"examples/{source}.md", "--output", str(directory),
                       "--max-rounds", str(budget)]
            for fixture in fixtures:
                command += ["--fixture", f"examples/fixtures/{fixture}"]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=environment)
            atomic_write(root / f"{name}_cli.txt", result.stdout + result.stderr)
            state = StateStore(directory).load()
            view = inspect_summary(state)
            assert (result.returncode, state.workflow_status, state.task_status, state.selected_round) == (
                0 if status == "completed" else 1, "finished", status, selected)
            assert view["external_requests"] == 0 and view["agents"]["result_summary"]["committed_results"] == 1
            assert input_hashes == [file_digest(path) for path in inputs]
            report = (directory / "report.md").read_text(encoding="utf-8")
            links = re.findall(r"\]\(([^)]+)\)", report)
            assert all((directory / unquote(target)).is_file() for target in links)
            before = {relative(path): (file_digest(path), path.stat().st_mtime_ns) for path in directory.rglob("*") if path.is_file()}
            inspected = subprocess.run([sys.executable, "-m", "test_workflow", "inspect", str(directory), "--summary"],
                                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=environment)
            assert inspected.returncode == 0 and json.loads(inspected.stdout)["last_event_seq"] == state.last_event_seq
            assert before == {relative(path): (file_digest(path), path.stat().st_mtime_ns) for path in directory.rglob("*") if path.is_file()}
            evidence["cases"][name] = {"run_id": state.run_id, "run_dir": relative(directory),
                "task_status": state.task_status, "stop_reason": state.stop_reason, "selected_round": selected,
                "rounds": view["rounds"], "pytest_processes": view["pytest_processes"],
                "generation_calls": view["generation_calls"], "external_requests": view["external_requests"],
                "agents": view["agents"], "report": relative(directory / "report.md"),
                "summary": relative(directory / "summary.json"), "links_verified": len(links), "inspect_read_only": True,
                "source_sha256": input_hashes[0], "spec_sha256": input_hashes[1], "inputs_unchanged": True}
            if name == "feedback":
                examples = ROOT / "examples/contracts/p6"
                examples.mkdir(parents=True, exist_ok=True)
                message = next(item for item in state.messages if item.type == "execution_feedback")
                atomic_write(examples / "feedback_message.json", message.canonical_json(indent=2) + "\n")
                result_path = directory / f"node_results/{state.current_round:02d}/result_summary/result.json"
                result_node = AgentResult.model_validate_json(result_path.read_text(encoding="utf-8"))
                atomic_write(examples / "summary_node_result.json", result_node.canonical_json(indent=2) + "\n")
                atomic_write(examples / "events.jsonl", (directory / "events.jsonl").read_text(encoding="utf-8"))
                atomic_write(examples / "event.schema.json", json.dumps(Event.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
            atomic_write(INDEX, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
            print(f"{name}: {state.task_status}/{state.stop_reason}; links={len(links)}; inspect=read_only", flush=True)
    if not args.skip_tests:
        xml = ROOT / "evaluation/p6_tests.xml"
        result = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "--basetemp", str(root / "tests"),
                                 "--junitxml", str(xml)], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=environment)
        atomic_write(ROOT / "evaluation/p6_tests.txt", result.stdout + result.stderr)
        suites = ET.parse(xml).getroot().findall("testsuite")
        evidence["tests"] = {field: sum(int(suite.attrib.get(field, 0)) for suite in suites)
                             for field in ("tests", "failures", "errors", "skipped")}
        evidence["tests"].update(exit_code=result.returncode, junit=relative(xml))
        print(result.stdout.strip(), flush=True)
    manifest = json.loads((ROOT / "evaluation/reuse_manifest.json").read_text(encoding="utf-8"))
    evidence["vendor_files_unchanged"] = all(file_digest(ROOT / "vendor/agentdev" / item["path"]) == item["sha256"] for item in manifest["files"])
    from vendor_audit import audit_vendor
    evidence["vendor_files_verified"] = audit_vendor(ROOT)["verified"]
    evidence["updated_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    evidence["p6_complete"] = (len(evidence["cases"]) == len(CASES) and evidence["vendor_files_verified"]
                              and evidence.get("tests", {}).get("exit_code") == 0)
    atomic_write(INDEX, json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    return 0 if evidence["vendor_files_verified"] and (args.skip_tests or evidence.get("tests", {}).get("exit_code") == 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
