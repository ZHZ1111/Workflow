"""Run the deterministic boundary loop and publish a 45-second evidence replay."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import difflib
import json
from pathlib import Path
from uuid import uuid4

from test_workflow.cli import main as cli
from test_workflow.reporting import inspect_summary
from test_workflow.service import DATA_ROOT
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, file_digest

ROOT = Path(__file__).resolve().parents[1]


def render_demo(trace, directory):
    template = (ROOT / "examples/quality_demo_template.html").read_text(encoding="utf-8")
    encoded = json.dumps(trace, ensure_ascii=False).replace("<", "\\u003c")
    atomic_write(ROOT / "evaluation/quality-review-demo.html", template.replace("__DEMO_DATA__", encoded))
    local = json.dumps({**trace, "report_href": "run/report.md"}, ensure_ascii=False).replace("<", "\\u003c")
    atomic_write(directory / "replay.html", template.replace("__DEMO_DATA__", local))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/quality-demo" / uuid4().hex)
    args = parser.parse_args()
    directory = args.output.resolve()
    if not directory.is_relative_to(ROOT / "outputs") or directory.exists():
        parser.error("output must be a new workspace outputs directory")
    directory.mkdir(parents=True)
    source, spec = ROOT / "examples/quality_boundary.py", ROOT / "examples/quality_boundary.md"
    input_hashes = (file_digest(source), file_digest(spec))
    command = ["run", "--demo", "--with-quality-review", "--with-summary", "--max-rounds", "2",
        "--source", str(source), "--spec", str(spec), "--output", str(directory / "run")]
    for number in (1, 2):
        command += ["--fixture", str(ROOT / f"examples/fixtures/quality_boundary_round_0{number}.json")]
    for name in ("quality_request_revision.json", "quality_approve.json"):
        command += ["--review-fixture", str(DATA_ROOT / name)]
    with (directory / "cli.log").open("w", encoding="utf-8") as log, redirect_stdout(log):
        code = cli(command)
    if code:
        raise RuntimeError(f"offline demo failed; see {directory / 'cli.log'}")
    state = StateStore(directory / "run").load()
    before = {path: (file_digest(path), path.stat().st_mtime_ns) for path in state.run_dir.rglob("*") if path.is_file()}
    summary = inspect_summary(state)
    assert before == {path: (file_digest(path), path.stat().st_mtime_ns) for path in before}
    assert input_hashes == (file_digest(source), file_digest(spec))
    first, second = summary["round_details"]
    assert [row["execution"]["passed"] for row in (first, second)] == [2, 3]
    assert all(row["execution"]["all_passed"] and row["coverage"]["meets_target"] for row in (first, second))
    assert summary["selected_round"] == 2 and summary["exported_quality_approved"]
    assert summary["external_requests"] == 0 and summary["review_calls"] == 2
    assert [row["quality"]["assessment"]["decision"]["verdict"] for row in (first, second)] == ["request_revision", "approve"]
    feedback = next(item for item in summary["messages"] if item["type"] == "quality_feedback")
    assert feedback["consumer_round"] == 2 and feedback["consumed"]
    old, new = (candidate.generated.code for candidate in state.agent_state.history)
    difference = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile="round-01.py", tofile="round-02.py"))
    atomic_write(directory / "test-diff.patch", difference)
    trace = {"recorded_at": datetime.now(timezone.utc).isoformat(), "mode": "fixed_clients_real_pytest",
        "duration_seconds": summary["duration_seconds"], "replay_seconds": 45, "run_id": state.run_id,
        "run_dir": state.run_dir.relative_to(ROOT).as_posix(), "source_sha256": input_hashes[0], "spec_sha256": input_hashes[1],
        "source": source.read_text(encoding="utf-8"), "first_tests": old, "second_tests": new, "test_diff": difference,
        "feedback": feedback, "rounds": summary["round_details"], "agents": summary["agents"],
        "selected_round": 2, "exported_quality_approved": True, "final_test_sha256": summary["final_test_sha256"],
        "external_requests": 0, "pytest_processes": 2,
        "node_records": [{key: node[key] for key in ("node", "round", "context", "result", "committed")} for node in summary["node_invocations"]]}
    payload = json.dumps(trace, ensure_ascii=False, indent=2) + "\n"
    atomic_write(directory / "demo-trace.json", payload)
    atomic_write(ROOT / "evaluation/quality-demo-trace.json", payload)
    render_demo(trace, directory)
    print(f"Demo: {ROOT / 'evaluation/quality-review-demo.html'}")
    print(f"Evidence: {directory}; two real pytest runs, 2 -> 3 passing tests, selected round 2, zero model requests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
