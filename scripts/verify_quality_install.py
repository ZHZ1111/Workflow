"""Offline quality wheel/--target check using the current locked dependencies."""

import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def main():
    directory = ROOT / "outputs" / "quality-install-verification" / uuid4().hex
    directory.mkdir(parents=True)
    wheels, installed = directory / "wheels", directory / "installed"
    results = {"success": False, "directory": str(directory), "checks": [],
               "isolation": "Python -I with one integrated non-editable wheel in --target; reuse current locked third-party dependencies"}
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    environment["PYTHONIOENCODING"] = "utf-8"

    def execute(name, command):
        print(f"{name}...", flush=True)
        result = subprocess.run(command, cwd=directory, env=environment, capture_output=True, timeout=600)
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        (directory / f"{name}.log").write_text(output, encoding="utf-8")
        if result.returncode:
            raise RuntimeError(f"{name} failed; see its evidence log")
        results["checks"].append(name)
        return output

    try:
        execute("build-wheels", [sys.executable, "-m", "pip", "wheel", "--no-index", "--no-cache-dir",
                "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(ROOT)])
        workflow_wheel = next(wheels.glob("test_workflow-*.whl"))
        assert len(list(wheels.glob("*.whl"))) == 1
        with ZipFile(workflow_wheel) as archive:
            for agent in ("test_generation", "test_execution", "test_quality_review"):
                assert f"test_workflow/agents/{agent}/agent.py" in archive.namelist()
                assert f"test_workflow/agents/{agent}.py" not in archive.namelist()
            assert "test_workflow/agents/test_generation/core/tools/_pytest_results.py" in archive.namelist()
            metadata = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
            assert "Requires-Dist: testgen-agent" not in archive.read(metadata).decode("utf-8")
            for name in ("quality_approve.json", "quality_request_revision.json", "quality_request_review.json"):
                json.loads(archive.read(f"test_workflow/demo/data/{name}"))
            assert "test_workflow/adapters/quality_revision.py" in archive.namelist()
            assert "test_workflow/quality_reporting.py" in archive.namelist()
            assert "test_workflow/report_metrics.py" in archive.namelist()
        results["checks"].append("wheel-review-resources")
        execute("install-wheels", [sys.executable, "-m", "pip", "install", "--no-index", "--no-cache-dir",
                "--no-deps", "--target", str(installed), *map(str, wheels.glob("*.whl"))])
        # -I excludes the current working directory and PYTHONPATH. Explicitly
        # assert that workflow and generation core resolve to the same wheel.
        harness = """
import json
from pathlib import Path
import sys
# Isolated mode ignores PYTHONIOENCODING; keep captured Chinese output explicit.
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
installed, directory = map(Path, sys.argv[1:3])
sys.path.insert(0, str(installed))
import test_workflow, test_workflow.agents.test_generation.core
assert Path(test_workflow.__file__).is_relative_to(installed)
assert Path(test_workflow.agents.test_generation.core.__file__).is_relative_to(installed)
from test_workflow.cli import main
from test_workflow.service import DATA_ROOT, fixture_review_client
from test_workflow.state import StateStore
from test_workflow.web import STATIC_ROOT
for name in ('quality_approve.json', 'quality_request_revision.json', 'quality_request_review.json'):
    fixture_review_client((DATA_ROOT / name,))
assert 'quality-review' in (STATIC_ROOT / 'index.html').read_text(encoding='utf-8')
assert 'quality_review_enabled' in (STATIC_ROOT / 'app.js').read_text(encoding='utf-8')
assert 'test_quality_review' in (STATIC_ROOT / 'workflow-view.js').read_text(encoding='utf-8')
assert 'model-usage' in (STATIC_ROOT / 'index.html').read_text(encoding='utf-8')
output = directory / 'cli-quality-run'
assert main(['run', '--demo', '--source', str(DATA_ROOT / 'calculation.py'), '--spec', str(DATA_ROOT / 'calculation.md'),
             '--output', str(output), '--with-quality-review', '--review-fixture', str(DATA_ROOT / 'quality_approve.json'), '--with-summary']) == 0
state = StateStore(output).load()
assert state.task_status == 'completed' and state.quality_status == 'passed' and state.config.quality_review_enabled
assert len(state.quality_assessments) == 1
assert (output / 'node_results/01/result_summary/result.json').is_file()
assert (output / 'final/test_subject.py').is_file()
assert main(['inspect', str(output), '--summary']) == 0
from test_workflow.reporting import inspect_summary
summary = inspect_summary(state)
assert summary['schema_version'] == '1.2'
assert summary['exported_quality_approved'] and summary['latest_quality_approved']
assert summary['review_calls'] == 1 and summary['total_external_requests'] == 0
assert summary['model_usage']['total']['client_calls'] == 2
print(json.dumps({'data_root': str(DATA_ROOT), 'static_root': str(STATIC_ROOT), 'run_dir': str(output)}, ensure_ascii=False))
"""
        execute("installed-quality-cli", [sys.executable, "-I", "-c", harness, str(installed), str(directory)])
        results["success"] = True
    finally:
        (directory / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Evidence: {directory / 'results.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
