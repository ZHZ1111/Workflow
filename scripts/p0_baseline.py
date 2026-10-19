"""Reproduce the AgentDev baseline; this is not the new Workflow runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

from test_workflow.agents.test_generation.core.agent import run_agent
from test_workflow.agents.test_generation.core.config import ConfigError, load_config
from test_workflow.agents.test_generation.core.llm_client import DeepSeekClient, LLMCallRecord, LLMGenerationResult
from test_workflow.agents.test_generation.core.models import GeneratedTests, ModelInfo, OracleMode, TaskInput
from test_workflow.agents.test_generation.core.tools import analyze_source

ROOT = Path(__file__).resolve().parents[1]


class FixtureClient:
    """Controlled local responses, with no HTTP or model inference."""
    def __init__(self, names):
        self.names = iter(names)
        self.contexts = []

    def generate_tests(self, context):
        self.contexts.append(context)
        name = next(self.names)
        generated = GeneratedTests.model_validate_json((ROOT / "examples/fixtures" / name).read_text(encoding="utf-8"))
        return LLMGenerationResult(generated=generated, provider="deepseek",
            requested_model="offline-fixture-model", response_model="offline-fixture-model",
            attempts=1, format_repairs=0, latency_seconds=0,
            calls=(LLMCallRecord(purpose="generation", response_id="offline-fixture"),))


def summarize(result, elapsed, original, source, client):
    rounds = []
    for candidate in result.state.history:
        execution, coverage = candidate.execution, candidate.coverage
        rounds.append({"round": candidate.number, "validation_error": candidate.validation_error,
            "execution_kind": execution.kind if execution else None,
            "passed": execution.passed if execution else None, "failed": execution.failed if execution else None,
            "coverage_status": coverage.status if coverage else None,
            "line_percent": coverage.line_percent if coverage else None,
            "branch_percent": coverage.branch_percent if coverage else None})
    return {"task_status": result.decision.status, "stop_reason": result.state.stop_reason,
        "selected_round": result.state.best_available_round, "api_calls": result.state.api_calls,
        "external_model_requests": result.state.api_calls if isinstance(client, DeepSeekClient) else 0,
        "duration_seconds": round(elapsed, 3), "source_unchanged": source.read_bytes() == original,
        "report": result.report_file.relative_to(ROOT).as_posix(), "rounds": rounds,
        "issue_kinds": sorted({issue.kind for issue in result.state.issues}),
        "feedback_received": len(getattr(client, "contexts", [])) > 1 and
            "CONTROLLER_FEEDBACK_DATA" in client.contexts[1].user_prompt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="Run one real calculation sample if configuration is valid")
    parser.add_argument("--env-file", type=Path, help="Read a selected config file without copying it")
    args = parser.parse_args()
    now = datetime.now(timezone(timedelta(hours=8)))
    directory = ROOT / "outputs/p0" / (now.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6])
    directory.mkdir(parents=True)
    evidence = {"schema_version": "1.0", "checked_at": now.isoformat(),
        "purpose": "AgentDev P0 baseline, not Workflow acceptance", "python": platform.python_version(),
        "platform": platform.platform(), "interpreter": ".venv/Scripts/python.exe",
        "dependencies": {name: metadata.version(name) for name in
            ("httpx", "pydantic", "python-dotenv", "pytest", "coverage", "setuptools", "wheel")},
        "checks": {}, "samples": {}, "real_model": {"status": "not_requested"}}
    for name, arguments, timeout in (
        ("pip_check", ["-m", "pip", "check"], 60),
        ("compileall", ["-m", "compileall", "-q", "src/test_workflow/agents/test_generation/core", "tests/agentdev", "scripts"], 60),
        ("agentdev_tests", ["-m", "pytest", "tests/agentdev", "-q", "-W", "error", "--basetemp", str(directory / "pytest-tmp"), "-o", "cache_dir=" + str(directory / "pytest-cache")], 600)):
        started = perf_counter()
        observed = subprocess.run([sys.executable, *arguments], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        log = directory / (name + ".txt")
        log.write_text(observed.stdout + observed.stderr, encoding="utf-8")
        evidence["checks"][name] = {"exit_code": observed.returncode, "duration_seconds": round(perf_counter() - started, 3),
            "command": ".venv/Scripts/python.exe " + " ".join(argument.replace(str(ROOT), ".") for argument in arguments),
            "log": log.relative_to(ROOT).as_posix(), "summary": observed.stdout.strip().splitlines()[-1:]}
        print(name, observed.returncode, evidence["checks"][name]["summary"], flush=True)
    evidence["input_audit"] = []
    for name in ("calculation", "validation", "discount", "relationships"):
        path = ROOT / "examples" / (name + ".py")
        source = analyze_source(path)
        specification = path.with_suffix(".md").read_text(encoding="utf-8")
        evidence["input_audit"].append({"sample": name, "bytes": path.stat().st_size,
            "functions": [item.name for item in source.functions], "spec_nonblank": bool(specification.strip()),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "direct_calls": [(edge.caller, edge.callee) for edge in source.direct_calls]})
    for name, sample, fixture_names, expected in (
        ("offline_normal", "calculation", ["calculation_complete.json"], "completed"),
        ("offline_feedback", "validation", ["validation_round_01.json", "validation_round_02.json"], "completed"),
        ("offline_defect", "discount", ["discount_defect.json"], "needs_review")):
        source_path = ROOT / "examples" / (sample + ".py")
        original = source_path.read_bytes()
        client = FixtureClient(fixture_names)
        request = TaskInput(source_path=source_path, spec_path=source_path.with_suffix(".md"),
            output_dir=directory / name, model=ModelInfo(model="offline-fixture-model", base_url="https://example.com"))
        started = perf_counter()
        result = run_agent(request, client=client, oracle_mode=OracleMode.SPECIFICATION)
        details = summarize(result, perf_counter() - started, original, source_path, client)
        details["expected_status"] = expected
        details["expectation_met"] = details["task_status"] == expected and details["source_unchanged"]
        if name == "offline_feedback":
            details["expectation_met"] &= len(details["rounds"]) == 2 and details["feedback_received"]
        if name == "offline_defect":
            details["expectation_met"] &= details["stop_reason"] == "suspected_source_defect"
        evidence["samples"][name] = details
        print(name, details["task_status"], details["stop_reason"], flush=True)
    if args.real:
        try:
            config = load_config(env_file=args.env_file, overrides={"max_rounds": 1})
        except ConfigError as error:
            evidence["real_model"] = {"status": "configuration_unavailable", "reason": str(error)}
            print("Real model configuration unavailable; offline evidence retained.", flush=True)
        else:
            print("Real model configuration valid; running calculation (one candidate round).", flush=True)
            source_path = ROOT / "examples/calculation.py"
            original = source_path.read_bytes()
            client = DeepSeekClient(config)
            request = TaskInput(source_path=source_path, spec_path=source_path.with_suffix(".md"),
                output_dir=directory / "real_calculation", max_rounds=1, model=ModelInfo.from_config(config))
            started = perf_counter()
            result = run_agent(request, client=client, oracle_mode=OracleMode.SPECIFICATION)
            evidence["real_model"] = {"status": "executed", "public_config": client.safe_config,
                **summarize(result, perf_counter() - started, original, source_path, client)}
            print("real_calculation", result.decision.status, result.state.stop_reason, flush=True)
    evidence["offline_baseline_passed"] = all(check["exit_code"] == 0 for check in evidence["checks"].values()) and all(sample["expectation_met"] for sample in evidence["samples"].values())
    for destination in (directory / "p0_results.json", ROOT / "evaluation/p0_results.json"):
        destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Evidence: evaluation/p0_results.json", flush=True)
    return 0 if evidence["offline_baseline_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
