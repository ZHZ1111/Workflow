"""S8 offline CLI scenarios and a Q01-Q20 index backed by actual JUnit records.

Run scenarios once, save unit/integration/full JUnit reports beside scenarios.json,
then --collect after the frontend and installed-wheel checks. Never loads .env or
constructs a model client. All candidate execution uses the production CLI.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import unquote
from uuid import uuid4
import xml.etree.ElementTree as ET

from test_workflow.agents.test_generation.core.models import GeneratedTests
from test_workflow.quality_contracts import Finding, ReviewDecision
from test_workflow.reporting import inspect_summary
from test_workflow.service import DATA_ROOT
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest
from vendor_audit import audit_vendor

ROOT = Path(__file__).resolve().parents[1]

# Every selector must have actual passing testcase elements, including parameters.
MATRIX = {
    "Q01": ("test_quality_workflow.test_quality_disabled_preserves_two_node_workflow",),
    "Q02": ("test_quality_workflow.test_first_approval_runs_three_nodes_and_consumes_both_handoffs",),
    "Q03": ("test_quality_generation.test_production_nodes_close_quality_loop_and_deliver_full_prompt",
            "test_quality_entrypoints.test_offline_cli_accepts_review_responses_in_order"),
    "Q04": ("test_quality_agent.test_coverage_gap_or_source_defect_never_calls_review",
            "test_quality_workflow.test_base_noncompletion_never_invokes_review"),
    "Q05": ("test_quality_workflow.test_base_noncompletion_never_invokes_review",),
    "Q06": ("test_quality_generation.test_three_production_candidates_stop_at_quality_budget_without_round_four",),
    "Q07": ("test_quality_client.test_business_inconsistency_and_model_host_metadata_are_never_accepted",),
    "Q08": ("test_quality_agent.test_invalid_model_evidence_cannot_be_saved_as_success",
            "test_quality_contracts.test_quotes_and_name_references_use_the_actual_review_input"),
    "Q09": ("test_quality_workflow.test_invalid_quality_proposals_are_rejected_without_consuming_ready",
            "test_quality_generation.test_invalid_quality_context_fails_before_client_call",
            "test_quality_contracts.test_quality_messages_and_results_bind_to_the_same_assessment"),
    "Q10": ("test_quality_workflow.test_duplicate_consumption_and_conflicting_feedback_cannot_commit_another_candidate",),
    "Q11": ("test_quality_client.test_transient_failures_exhaust_shared_budget_with_safe_failure_telemetry",
            "test_quality_client.test_permanent_http_errors_fail_once_without_echoing_body_or_headers",
            "test_quality_client.test_one_format_repair_returns_independent_decision_and_accumulates_usage",
            "test_quality_client.test_invalid_output_on_last_attempt_does_not_create_a_fourth_request"),
    "Q12": ("test_quality_generation.test_quality_revision_keeps_original_passing_tests_and_support",),
    "Q13": ("test_quality_workflow.test_quality_feedback_reaches_second_generation_and_approved_candidate_is_exported",),
    "Q14": ("test_quality_workflow.test_review_stop_routes_preserve_findings_without_extra_feedback_or_candidates",
            "test_quality_entrypoints.test_shared_service_composes_quality_and_summary"),
    "Q15": ("test_quality_agent.test_concrete_spec_conflict_returns_human_review_without_generation_feedback",),
    "Q16": ("test_quality_contracts.test_saved_legacy_contracts_still_load",),
    "Q17": ("test_quality_entrypoints.test_cli_web_same_injected_responses_have_identical_routes_and_approved_export",),
    "Q18": ("test_quality_reporting.test_inspection_detects_response_result_or_consumption_tampering",
            "test_reporting.test_inspect_summary_is_read_only_and_detects_changed_evidence"),
    "Q19": ("test_quality_workflow.test_duplicate_candidate_after_quality_feedback_stops_before_another_review",
            "test_quality_workflow.test_review_stop_routes_preserve_findings_without_extra_feedback_or_candidates"),
    "Q20": (),  # Requires the independently installed-wheel evidence below.
}


def relative(path):
    return Path(path).resolve().relative_to(ROOT).as_posix()


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def snapshot(directory):
    return {relative(path): (file_digest(path), path.stat().st_mtime_ns)
            for path in directory.rglob("*") if path.is_file()}


def fixtures(directory):
    """Append specification-backed cases without changing any passing test."""
    directory.mkdir()
    first = GeneratedTests.model_validate_json((DATA_ROOT / "calculation_complete.json").read_text(encoding="utf-8"))
    generated = [first]
    decisions = []
    for name, arguments, expected, quote in (
        ("both_zero", (0, 0), 0, "单价或数量为 0 时返回 0。"),
        ("one_one", (1, 1), 1, "返回整数 `unit_price * quantity`"),
        ("large", (1000000, 1000000), 1000000000000, "返回整数 `unit_price * quantity`"),
    ):
        decisions.append(ReviewDecision(verdict="request_revision", summary=f"补充规格支持的 {name} 场景。", findings=(Finding(
            finding_id=name, category="missing_boundary", blocking=True, target_functions=("calculate_total",),
            spec_quote=quote, description="已有测试执行通过，尚未独立验证此输入组合。",
            suggested_case=f"calculate_total{arguments} == {expected}"),)))
        old = generated[-1]
        scenario = old.plan.scenarios[0].updated(scenario_id=name, inputs={"args": arguments, "kwargs": {}},
            expected_behavior=f"returns {expected}", expected_basis=old.plan.scenarios[0].expected_basis.updated(detail=quote))
        mapping = old.mappings[0].updated(test_function=f"test_{name}", scenario_ids=(name,))
        generated.append(old.updated(code=old.code + f"\ndef test_{name}():\n    assert calculate_total{arguments} == {expected}\n",
            plan=old.plan.updated(scenarios=(*old.plan.scenarios, scenario)), mappings=(*old.mappings, mapping)))
    paths = {}
    for number, item in enumerate(generated[:3], 1):
        paths[f"candidate{number}"] = directory / f"candidate-{number}.json"
        atomic_write(paths[f"candidate{number}"], item.canonical_json())
    for number, item in enumerate(decisions, 1):
        paths[f"revision{number}"] = directory / f"revision-{number}.json"
        atomic_write(paths[f"revision{number}"], item.canonical_json())
    human = ReviewDecision(verdict="request_review", summary="此条规格需要人工确认解释。", questions=("请确认规格解释。",),
        findings=(Finding(finding_id="clarify", category="ambiguous_specification", blocking=True,
            target_functions=("calculate_total",), spec_quote="输入域是前置条件", description="保留规格依据供人工确认。"),))
    paths["human"] = directory / "human.json"
    atomic_write(paths["human"], human.canonical_json())
    # Keep the candidate valid in isolation while removing an earlier passing test.
    old = generated[1]
    paths["removed"] = directory / "removed-passing-test.json"
    atomic_write(paths["removed"], old.updated(
        code=old.code.replace("def test_total_normal():\n    assert calculate_total(12, 3) == 36\n", ""),
        mappings=tuple(item for item in old.mappings if item.test_function != "test_total_normal"),
        plan=old.plan.updated(scenarios=tuple(item for item in old.plan.scenarios if item.scenario_id != "total_normal"))).canonical_json())
    return paths


def collect_run(output, expected):
    before = snapshot(output)
    state = StateStore(output).load()
    summary = inspect_summary(state)
    assert snapshot(output) == before, "inspection changed task files"
    for key, value in expected.items():
        assert summary[key] == value, (key, summary[key], value)
    assert summary["external_requests"] == 0 and summary["quality_integrity_errors"] == []
    assert not (output / "node_results/04").exists()
    assert all(row["assessment"]["observation_mode"] == "real" for row in summary["round_details"])
    links = re.findall(r"\]\(([^)]+)\)", (output / "report.md").read_text(encoding="utf-8"))
    assert all((output / unquote(target)).is_file() for target in links), "broken report evidence link"
    feedback = [message for message in summary["messages"] if message["type"] == "quality_feedback"]
    if state.quality_status == "passed":
        assert summary["exported_quality_approved"] and state.selected_round == len(state.agent_state.history)
    return {"run_id": state.run_id, "output": relative(output), "inspect_read_only": True,
        "report_links_verified": len(links), "input_source_sha256": file_digest(state.agent_state.request.source_path),
        "input_spec_sha256": file_digest(state.agent_state.request.spec_path),
        **{key: summary[key] for key in ("workflow_status", "task_status", "stop_reason", "quality_status", "quality_stop_reason",
            "rounds", "selected_round", "selected_quality_approved", "latest_quality_approved", "exported_quality_approved",
            "generation_calls", "review_calls", "external_requests", "pytest_processes", "unresolved_findings_round",
            "unresolved_findings", "final_test_sha256", "model_usage")},
        "feedback": feedback, "node_order": [{"round": node["round"], "node": node["node"], "committed": node["committed"]}
                                              for node in summary["node_invocations"]],
        "round_details": summary["round_details"],
        "artifacts": [{"path": ref.path, "sha256": ref.sha256, "kind": ref.kind} for ref in state.artifacts],
        "report": relative(output / "report.md"), "summary": relative(output / "summary.json")}


def scenarios(evidence):
    assert not (evidence / "scenarios.json").exists(), "use a new evidence directory to rerun scenarios"
    fixed = fixtures(evidence / "fixtures")
    approve = DATA_ROOT / "quality_approve.json"
    cases = (
        ("disabled", "calculation", (fixed["candidate1"],), (), 3, False,
         {"task_status": "completed", "quality_status": "disabled", "review_calls": 0, "rounds": 1}),
        ("approved", "calculation", (fixed["candidate1"],), (approve,), 3, True,
         {"task_status": "completed", "quality_status": "passed", "review_calls": 1, "rounds": 1}),
        ("quality_feedback", "calculation", (fixed["candidate1"], fixed["candidate2"]), (fixed["revision1"], approve), 3, True,
         {"task_status": "completed", "quality_status": "passed", "review_calls": 2, "rounds": 2, "selected_round": 2}),
        ("three_round_budget", "calculation", tuple(fixed[f"candidate{n}"] for n in (1, 2, 3)),
         tuple(fixed[f"revision{n}"] for n in (1, 2, 3)), 3, True,
         {"task_status": "partial", "quality_stop_reason": "quality_budget_reached", "stop_reason": "round_limit_reached", "rounds": 3, "review_calls": 3}),
        ("human_review", "calculation", (fixed["candidate1"],), (fixed["human"],), 3, True,
         {"task_status": "needs_review", "quality_status": "needs_review", "rounds": 1}),
        ("review_exhausted", "calculation", (fixed["candidate1"], fixed["candidate2"]), (fixed["revision1"],), 3, True,
         {"workflow_status": "error", "quality_status": "error", "rounds": 2}),
        ("execution_feedback_first", "validation", (DATA_ROOT / "validation_round_01.json", DATA_ROOT / "validation_round_02.json"),
         (approve,), 3, True, {"task_status": "completed", "quality_status": "passed", "rounds": 2, "review_calls": 1}),
        ("source_defect", "discount", (DATA_ROOT / "discount_defect.json",), (approve,), 3, True,
         {"task_status": "needs_review", "stop_reason": "suspected_source_defect", "review_calls": 0, "rounds": 1}),
        ("duplicate", "calculation", (fixed["candidate1"], fixed["candidate1"]), (fixed["revision1"],), 3, True,
         {"task_status": "partial", "quality_stop_reason": "quality_no_progress", "review_calls": 1, "rounds": 2, "pytest_processes": 1}),
        ("passing_test_removed", "calculation", (fixed["candidate1"], fixed["removed"]), (fixed["revision1"],), 2, True,
         {"task_status": "partial", "stop_reason": "round_limit_reached", "review_calls": 1, "rounds": 2, "pytest_processes": 1}),
    )
    source_hashes = {str(path): file_digest(path) for sample in ("calculation", "validation", "discount")
                     for path in (ROOT / f"examples/{sample}.py", ROOT / f"examples/{sample}.md")}
    results = {"created_at": datetime.now(timezone.utc).isoformat(), "model_requests_sent": 0,
               "clients": "fixed generation and review fixtures; production nodes and real pytest/coverage", "cases": [], "errors": []}
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    for name, sample, generated, reviews, budget, enabled, expected in cases:
        print(f"S8 CLI: {name}...", flush=True)
        output = evidence / name
        command = [sys.executable, "-m", "test_workflow", "run", "--demo", "--with-summary", "--source",
            str(ROOT / f"examples/{sample}.py"), "--spec", str(ROOT / f"examples/{sample}.md"), "--output", str(output),
            "--max-rounds", str(budget)]
        for path in generated:
            command += ["--fixture", str(path)]
        if enabled:
            command += ["--with-quality-review"]
        for path in reviews:
            command += ["--review-fixture", str(path)]
        try:
            completed = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True, timeout=300)
            atomic_write(evidence / f"{name}.log", (completed.stdout + completed.stderr).decode("utf-8", errors="replace"))
            assert completed.returncode == (0 if expected.get("task_status") == "completed" else 1), completed.returncode
            record = collect_run(output, expected)
            if name == "quality_feedback":
                assert len(record["feedback"]) == 1 and record["feedback"][0]["consumer_round"] == 2
                assert all(row["assessment"]["task_status"] == "completed" for row in record["round_details"])
            if name == "passing_test_removed":
                assert record["round_details"][-1]["validation_error"].startswith("preserve_tests")
            assert all(file_digest(Path(path)) == digest for path, digest in source_hashes.items())
            results["cases"].append({"name": name, "passed": True, "cli_exit": completed.returncode, **record})
        except Exception as error:
            results["errors"].append({"case": name, "type": type(error).__name__, "detail": str(error)})
        write_json(evidence / "scenarios.json", results)
    results["success"] = len(results["cases"]) == len(cases) and not results["errors"]
    results["inputs_unchanged"] = all(file_digest(Path(path)) == digest for path, digest in source_hashes.items())
    results["success"] = results["success"] and results["inputs_unchanged"]
    write_json(evidence / "scenarios.json", results)
    return results["success"]


def junit(path):
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    return {"path": relative(path), "sha256": file_digest(path), "tests": len(cases),
            **{name: sum(case.find(tag) is not None for case in cases)
               for name, tag in (("failures", "failure"), ("errors", "error"), ("skipped", "skipped"))},
            "duration_seconds": sum(float(case.get("time", 0)) for case in cases),
            "suite_duration_seconds": sum(float(suite.get("time", 0)) for suite in root.iter("testsuite"))}, cases


def collect_legacy(directory):
    before = snapshot(directory)
    raw = json.loads((directory / "state.json").read_text(encoding="utf-8"))
    saved = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert "quality_review_enabled" not in raw["config"] and "quality_status" not in raw, "requires a pre-quality task"
    state = StateStore(directory).load()
    summary = inspect_summary(state)
    assert summary["quality_status"] == "disabled" and summary["review_calls"] == 0
    for key in ("task_status", "stop_reason", "selected_round", "rounds", "generation_calls", "external_requests"):
        assert summary[key] == saved[key], (key, summary[key], saved[key])
    assert before == snapshot(directory), "legacy inspection changed files"
    return {"run_id": state.run_id, "directory": relative(directory), "saved_summary_version": saved["schema_version"],
        "current_summary_version": summary["schema_version"], "task_status": summary["task_status"],
        "selected_round": summary["selected_round"], "files_unchanged": len(before), "passed": True,
        "saved_summary_sha256": file_digest(directory / "summary.json"), "saved_report_sha256": file_digest(directory / "report.md")}


def check_frontend(evidence):
    command = ["node", "--test", "--test-reporter=tap", *[relative(path) for path in sorted((ROOT / "tests/web").glob("*.test.cjs"))]]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=60)
    output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    atomic_write(evidence / "frontend.log", output)
    record = {"command": command, "exit_code": result.returncode}
    for name, label in (("tests", "tests"), ("passed", "pass"), ("failed", "fail"), ("skipped", "skipped")):
        match = re.search(rf"^# {label} (\d+)$", output, re.M)
        record[name] = int(match[1]) if match else 0
    record["log_sha256"] = file_digest(evidence / "frontend.log")
    write_json(evidence / "frontend.json", record)
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return result.returncode == 0 and record["tests"] > 0 and record["failed"] == 0


def check_full(evidence):
    """Persist progress and the exit code independently of a terminal session."""
    if (evidence / "full-run.json").exists() or (evidence / "full.xml").exists():
        raise ValueError("full regression already recorded; use a new evidence directory")
    command = [sys.executable, "-m", "pytest", "tests", "-q",
               "--junitxml", str(evidence / "full.xml")]
    started = time.monotonic()
    record = {"command": command, "started_at": datetime.now(timezone.utc).isoformat(),
              "runner_pid": os.getpid(), "status": "running", "exit_code": None}
    write_json(evidence / "full-run.json", record)
    try:
        with (evidence / "full.log").open("wb") as log:
            completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}, timeout=5400)
        record.update(status="finished", exit_code=completed.returncode)
    except Exception as error:
        record.update(status="error", error_type=type(error).__name__)
    finally:
        record.update(finished_at=datetime.now(timezone.utc).isoformat(), duration_seconds=time.monotonic() - started)
        record["success"] = record["exit_code"] == 0 and (evidence / "full.xml").is_file()
        write_json(evidence / "full-run.json", record)
    return record["success"]


def collect(evidence, installed, legacy_runs):
    phases = {}
    for name in ("contracts-client-agent", "integration", "full"):
        phases[name], _ = junit(evidence / f"{name}.xml")
    full, cases = junit(evidence / "full.xml")
    installed_result = json.loads(installed.read_text(encoding="utf-8"))
    offline = json.loads((evidence / "scenarios.json").read_text(encoding="utf-8"))
    frontend = json.loads((evidence / "frontend.json").read_text(encoding="utf-8"))
    full_run = json.loads((evidence / "full-run.json").read_text(encoding="utf-8"))
    legacy = [collect_legacy(path.resolve()) for path in legacy_runs]
    matrix = {}
    for identity, selectors in MATRIX.items():
        matched, missing = [], []
        for selector in selectors:
            module, name = selector.split(".")
            actual = [case for case in cases if case.get("classname", "").split(".")[-1] == module
                      and case.get("name", "").split("[")[0] == name]
            if not actual:
                missing.append(selector)
            matched += actual
        passed = bool(selectors) and not missing and all(
            not any(case.find(tag) is not None for tag in ("failure", "error", "skipped")) for case in matched)
        if identity == "Q20":
            passed = installed_result.get("success") is True and set(installed_result.get("checks", ())) == {
                "build-wheels", "wheel-review-resources", "install-wheels", "installed-quality-cli"}
        if identity == "Q17":
            passed = passed and frontend["exit_code"] == 0 and frontend["tests"] > 0
        if identity == "Q16":
            passed = passed and bool(legacy) and all(item["passed"] for item in legacy)
        matrix[identity] = {"passed": passed, "missing_selectors": missing,
            "testcases": [f"{case.get('classname')}::{case.get('name')}" for case in matched],
            "evidence": relative(installed) if identity == "Q20" else full["path"]}
    vendor = audit_vendor(ROOT)
    archived_junit = ROOT / "evaluation/quality-review-s8-tests.xml"
    shutil.copyfile(evidence / "full.xml", archived_junit)
    sample = next(case for case in offline["cases"] if case["name"] == "quality_feedback")
    trace_path = ROOT / "evaluation/quality-review-s8-trace.json"
    write_json(trace_path, {"run_id": sample["run_id"], "mode": "offline", "output": sample["output"],
        "feedback": sample["feedback"], "final_test_sha256": sample["final_test_sha256"],
        "selected_round": sample["selected_round"], "exported_quality_approved": sample["exported_quality_approved"],
        "node_order": sample["node_order"], "rounds": [{"round": row["round"], "candidate_id": row["candidate_id"],
            "execution": row["execution"], "coverage": row["coverage"],
            "base_task_status": row["assessment"]["task_status"], "quality_assessment": row["quality"]["assessment"],
            "quality_evidence_verified": row["quality"]["evidence_verified"]} for row in sample["round_details"]]})
    checks = {}
    for name, command in (("dependencies", [sys.executable, "-m", "pip", "check"]), ("diff", ["git", "diff", "--check"])):
        checked = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=60)
        log = evidence / f"{name}.log"
        atomic_write(log, (checked.stdout + checked.stderr).decode("utf-8", errors="replace"))
        checks[name] = {"exit_code": checked.returncode, "log": relative(log), "sha256": file_digest(log)}
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(), "python": sys.version,
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "full_scope_counts": {"workflow": sum(case.get("classname", "").startswith("tests.") and not case.get("classname", "").startswith("tests.agentdev.") for case in cases),
                              "vendor": sum(case.get("classname", "").startswith(("tests.agentdev.", "vendor.")) for case in cases)},
        "evidence_directory": relative(evidence), "phases": phases, "frontend": frontend,
        "full_run": full_run,
        "interrupted_attempts": json.loads((evidence / "interrupted.json").read_text(encoding="utf-8")) if (evidence / "interrupted.json").exists() else [],
        "archived_full_junit": {"path": relative(archived_junit), "sha256": file_digest(archived_junit)},
        "quality_feedback_trace": {"path": relative(trace_path), "sha256": file_digest(trace_path)},
        "legacy_inspection": legacy,
        "checks": checks,
        "legacy_excluded": json.loads((evidence / "legacy-excluded.json").read_text(encoding="utf-8")) if (evidence / "legacy-excluded.json").exists() else [],
        "installed": {"evidence": relative(installed), **installed_result}, "matrix": matrix,
        "scenarios": {"evidence": relative(evidence / "scenarios.json"), "success": offline["success"],
                      "count": len(offline["cases"]), "pytest_processes": sum(case["pytest_processes"] for case in offline["cases"]),
                      "model_requests_sent": offline["model_requests_sent"], "inputs_unchanged": offline["inputs_unchanged"],
                      "cases": [{key: case[key] for key in ("name", "output", "cli_exit", "task_status", "quality_status", "quality_stop_reason",
                          "rounds", "selected_round", "pytest_processes", "review_calls", "exported_quality_approved", "final_test_sha256", "summary", "report")}
                          for case in offline["cases"]]},
        "vendor": vendor, "historical_baselines": {"pre_quality": 711, "S5": 974, "scope": "historical, not current results"}}
    result["success"] = (all(row["passed"] for row in matrix.values()) and offline["success"] and vendor["verified"]
        and full_run["success"] and all(check["exit_code"] == 0 for check in checks.values()) and frontend["exit_code"] == 0
        and all(row["tests"] and not (row["failures"] or row["errors"] or row["skipped"]) for row in phases.values()))
    write_json(evidence / "index.json", result)
    # The compact portable index is tracked; raw task directories remain ignored.
    write_json(ROOT / "evaluation/quality-review-s8.json", result)
    print(f"S8 matrix: {sum(row['passed'] for row in matrix.values())}/20; full regression: {full['tests']}; success={result['success']}", flush=True)
    return result["success"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=ROOT / "outputs/quality-s8" / uuid4().hex)
    stages = parser.add_mutually_exclusive_group()
    stages.add_argument("--collect", action="store_true", help="Collect actual reports instead of running CLI scenarios")
    stages.add_argument("--frontend", action="store_true", help="Run all frontend tests and record TAP results")
    stages.add_argument("--full", action="store_true", help="Run the full Workflow/vendor regression and persist its exit code")
    parser.add_argument("--installed", type=Path, help="results.json from verify_quality_install.py, required for --collect")
    parser.add_argument("--legacy-run", type=Path, action="append", default=[], help="Existing pre-quality run for read-only inspection; repeatable")
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    if not evidence.is_relative_to(ROOT / "outputs"):
        parser.error("evidence must be inside this workspace's outputs directory")
    evidence.mkdir(parents=True, exist_ok=True)
    print(f"Evidence: {evidence}", flush=True)
    if args.collect and not args.installed:
        parser.error("--collect requires --installed")
    passed = (collect(evidence, args.installed.resolve(), args.legacy_run) if args.collect else
              check_full(evidence) if args.full else
              check_frontend(evidence) if args.frontend else scenarios(evidence))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
