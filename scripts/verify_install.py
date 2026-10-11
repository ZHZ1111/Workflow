"""Build wheels, install into a fresh venv and exercise installed CLI/Web outside the checkout."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from uuid import uuid4
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "outputs" / "install-verification" / uuid4().hex
    directory.mkdir(parents=True)
    wheel_dir = directory / "wheels"
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONIOENCODING"] = "utf-8"
    results = {"directory": str(directory), "checks": []}

    def execute(name, command, cwd=directory):
        print(f"{name}...", flush=True)
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=600)
        (directory / f"{name}.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        assert result.returncode == 0, f"{name} failed; inspect {directory / (name + '.log')}"
        results["checks"].append(name)
        return result.stdout

    execute("build", [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                      "--wheel-dir", str(wheel_dir), str(ROOT / "vendor/agentdev"), str(ROOT)])
    venv.EnvBuilder(with_pip=True).create(directory / "venv")
    python = directory / "venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    scripts = python.parent
    execute("dependencies", [str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements.lock")])
    execute("packages", [str(python), "-m", "pip", "install", "--no-deps", *map(str, wheel_dir.glob("*.whl"))])
    execute("pip-check", [str(python), "-m", "pip", "check"])
    locations = execute("resources", [str(python), "-c",
        "import json; from test_workflow.web import STATIC_ROOT; from test_workflow.service import DATA_ROOT, sample_inputs as samples; "
        "assert len(samples()) == 3; assert all((STATIC_ROOT / p).is_file() for p in ('index.html','app.js','styles.css')); "
        "print(json.dumps({'data':str(DATA_ROOT), 'static':str(STATIC_ROOT)}))"])
    data_root = Path(json.loads(locations)["data"])
    assert data_root.is_relative_to(directory / "venv")
    execute("cli", [str(scripts / ("test-workflow.exe" if sys.platform == "win32" else "test-workflow")),
        "run", "--demo", "--source", str(data_root / "validation.py"), "--spec", str(data_root / "validation.md"),
        "--output", str(directory / "cli-run")])
    execute("inspect", [str(python), "-m", "test_workflow", "inspect", str(directory / "cli-run"), "--summary"])
    execute("web-entry", [str(scripts / ("test-workflow-web.exe" if sys.platform == "win32" else "test-workflow-web")), "--help"])
    with socket.socket() as temporary:
        temporary.bind(("127.0.0.1", 0))
        port = temporary.getsockname()[1]
    with (directory / "web.log").open("w", encoding="utf-8") as log:
        # Exercise the installed production service with an injected model
        # response. No offline mode exists in the Web API and no HTTP leaves
        # this machine during clean-install verification.
        harness = ("from pathlib import Path; import test_workflow.web as web; "
                   "from test_workflow.service import fixture_client; from testgen_agent.config import AppConfig; "
                   "web.load_config=lambda **kw: AppConfig(model='fixture-model', api_key='fixture-key'); "
                   "web.DeepSeekClient=lambda config: fixture_client('validation'); "
                   f"web.serve('127.0.0.1', {port}, Path.cwd())")
        process = subprocess.Popen([str(python), "-c", harness], cwd=directory, env=environment, stdout=log, stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        base = f"http://127.0.0.1:{port}"
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(base + "/api/bootstrap", timeout=5) as response:
                        bootstrap = json.load(response)
                    break
                except OSError:
                    if time.monotonic() > deadline or process.poll() is not None:
                        raise RuntimeError("installed Web server did not start")
                    time.sleep(0.2)
            assert "samples" not in bootstrap
            content = {"source": (data_root / "validation.py").read_text(encoding="utf-8"),
                       "spec": (data_root / "validation.md").read_text(encoding="utf-8")}
            with urlopen(Request(base + "/api/runs", data=json.dumps(content).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}), timeout=10) as response:
                created = json.load(response)
            deadline = time.monotonic() + 90
            while True:
                with urlopen(base + f"/api/runs/{created['id']}", timeout=10) as response:
                    job = json.load(response)
                if job["status"] in ("finished", "failed"):
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("installed Web job did not settle")
                time.sleep(0.2)
            assert job["status"] == "finished" and job["summary"]["task_status"] == "completed"
            assert job["summary"]["rounds"] == 2 and job["state"]["config"]["mode"] == "real"
            assert job["testCode"] and job["reportMarkdown"]
            results["checks"].append("installed-web-feedback-with-fixture-model")
        finally:
            process.terminate()
            process.wait(timeout=10)
    results["success"] = True
    (directory / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence: {directory / 'results.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
