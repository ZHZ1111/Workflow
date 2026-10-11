"""Verify both entry points with offline cases and optional authorized DeepSeek calls."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen
from uuid import uuid4

from test_workflow.reporting import inspect_summary
from test_workflow.state import StateStore
from test_workflow.service import sample_inputs as samples
from test_workflow.web import WebServer

ROOT = Path(__file__).resolve().parents[1]


def request(base, path, payload=None):
    req = Request(base + path, data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=20) as response:
        return json.load(response)


def verify(directory, sample, mode):
    state = StateStore(directory).load()
    summary = inspect_summary(state)
    expected = "needs_review" if sample == "discount" else "completed"
    assert state.workflow_status == "finished" and state.task_status == expected, (
        sample, state.workflow_status, state.task_status, state.error_code, state.stop_reason)
    assert summary["pytest_processes"] >= 1
    assert summary["external_requests"] == 0 if mode == "offline" else summary["external_requests"] >= 1
    assert summary["selected_all_passed"] is (sample != "discount")
    assert summary["agents"]["test_generation"]["committed_results"] >= 1
    assert summary["agents"]["test_execution"]["committed_results"] >= 1
    if mode == "offline" and sample == "validation":
        assert summary["rounds"] == 2 and state.consumed_message_ids
    return {key: summary[key] for key in ("run_id", "mode", "task_status", "stop_reason", "rounds",
        "selected_round", "selected_execution", "selected_coverage", "external_requests", "pytest_processes")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="Also send the three bundled cases through DeepSeek via CLI and HTTP")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--skip-offline", action="store_true", help="Only meaningful with --real")
    parser.add_argument("--interfaces", nargs="+", choices=("cli", "web"), default=("cli", "web"))
    parser.add_argument("--samples", nargs="+", choices=tuple(samples()), default=tuple(samples()))
    args = parser.parse_args()
    if args.skip_offline and not args.real:
        parser.error("--skip-offline requires --real")
    if args.interfaces == ["web"] and not args.real:
        parser.error("Web only uses DeepSeek; --interfaces web requires --real")
    directory = ROOT / "outputs" / "verification" / uuid4().hex
    directory.mkdir(parents=True)
    results = {"verified_at": datetime.now(timezone.utc).isoformat(), "python": sys.version,
               "directory": str(directory), "cases": [], "errors": []}
    modes = ([] if args.skip_offline else ["offline"]) + (["real"] if args.real else [])
    with WebServer(("127.0.0.1", 0), directory, args.env_file) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            for mode in modes:
                for interface in args.interfaces:
                    if mode == "offline" and interface == "web":
                        continue
                    for sample, content in samples().items():
                        if sample not in args.samples:
                            continue
                        print(f"Verifying {mode}/{interface}/{sample}...", flush=True)
                        try:
                            if interface == "cli":
                                output = directory / f"{mode}-cli-{sample}"
                                command = [sys.executable, "-m", "test_workflow", "run", "--with-summary", "--source",
                                           str(ROOT / f"examples/{sample}.py"), "--spec", str(ROOT / f"examples/{sample}.md"),
                                           "--output", str(output)]
                                command += ["--demo"] if mode == "offline" else ["--env-file", str(args.env_file.resolve())]
                                completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=1800)
                                (directory / f"{mode}-cli-{sample}.log").write_text(completed.stdout + completed.stderr, encoding="utf-8")
                                assert completed.returncode == (1 if sample == "discount" else 0), completed.stdout
                            else:
                                created = request(base, "/api/runs", content)
                                deadline = time.monotonic() + 1800
                                while True:
                                    job = request(base, f"/api/runs/{created['id']}")
                                    if job["status"] in ("finished", "failed"):
                                        break
                                    if time.monotonic() > deadline:
                                        raise TimeoutError("Web job exceeded acceptance budget")
                                    time.sleep(1)
                                assert job["status"] == "finished", job["error"]
                                output = Path(job["outputDir"])
                                assert job["testCode"] and job["reportMarkdown"] and job["downloads"]
                            result = verify(output, sample, mode)
                            results["cases"].append({"interface": interface, "sample": sample, "output": str(output), **result})
                            print(f"  {result['task_status']}, rounds={result['rounds']}, requests={result['external_requests']}", flush=True)
                        except Exception as error:
                            results["errors"].append({"interface": interface, "sample": sample, "mode": mode,
                                                       "error": type(error).__name__})
                            print(f"  FAILED ({type(error).__name__}); inspect {directory}", flush=True)
                        finally:
                            (directory / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        finally:
            server.shutdown()
            thread.join(timeout=5)
    print(f"Evidence: {directory / 'results.json'}", flush=True)
    return 1 if results["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
