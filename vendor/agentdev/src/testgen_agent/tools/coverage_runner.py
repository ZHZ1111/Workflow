"""D2: fresh branch coverage and target-only, source-linked observations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from typing import Any

from pydantic import ValidationError

from ..models import BranchArc, CoverageResult, ExecutionKind, ExecutionResult
from ..workspace import SUBJECT_FILENAME
from .coverage_mapper import build_coverage_source_hints
from .source_reader import SourceReadError, read_source
from .test_runner import (
    ExecutionArtifactError, _read_log, _run_tests, _test_environment, _validate_workspace,
)


@dataclass(frozen=True, slots=True)
class CoverageRunResult:
    """One observed execution, its independent coverage result and host artifacts."""

    execution: ExecutionResult
    coverage: CoverageResult
    artifacts: Path
    source_hints: dict[str, Any]

    @property
    def candidate_all_passed(self) -> bool:
        return self.execution.all_passed


def run_tests_with_coverage(workspace: str | Path, timeout: int = 30, *, round_number: int = 1) -> CoverageRunResult:
    """Run the same D1 candidate once, under coverage, then export only subject.py.

    The pytest timeout remains 1..300 seconds; export has a separate timeout of
    min(timeout, 30) seconds. Export/parse failures keep execution and return
    unavailable coverage. Input and host artifact errors use D1 error types.
    No previous database is loaded, appended or combined.
    """
    if not isinstance(workspace, (str, Path)):
        raise TypeError("workspace must be a string or Path")
    if type(timeout) is not int:
        raise TypeError("timeout must be an integer")
    if not 1 <= timeout <= 300:
        raise ValueError("timeout must be in 1..300 seconds")
    root, _ = _validate_workspace(Path(workspace), round_number)
    target = root / SUBJECT_FILENAME
    snapshot = None
    snapshot_error = None
    try:
        snapshot = read_source(target)
    except SourceReadError:
        snapshot_error = "source_snapshot_unavailable"

    execution, artifacts = _run_tests(root, timeout, collect_coverage=True, round_number=round_number)
    export = _export_coverage(root, artifacts, execution, timeout)
    coverage = CoverageResult(target_file=target, status="unavailable", error=export["error"]) if export["error"] else (
        parse_coverage_json(artifacts / "coverage.json", target_file=target)
    )
    try:
        if snapshot is None:
            raise ValueError(snapshot_error)
        if read_source(target) != snapshot:
            raise ValueError("source_snapshot_changed")
        source_hints = build_coverage_source_hints(snapshot.code, coverage)
    except (SourceReadError, ValueError, SyntaxError):
        coverage = CoverageResult(
            target_file=target, status="unavailable",
            error="source_snapshot_changed_or_invalid: coverage cannot be linked to the input snapshot.",
        )
        source_hints = build_coverage_source_hints("", coverage)

    combined = {
        "candidate_all_passed": execution.all_passed,
        "execution": execution.model_dump(mode="json", round_trip=True),
        "coverage": coverage.model_dump(mode="json", round_trip=True),
        "source_hints": source_hints,
    }
    try:
        (artifacts / "coverage_result.json").write_text(coverage.canonical_json(indent=2) + "\n", encoding="utf-8")
        (artifacts / "coverage_source_hints.json").write_text(
            json.dumps(source_hints, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        (artifacts / "run_result.json").write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    except OSError as error:
        raise ExecutionArtifactError("Unable to save coverage result artifacts; execution.json is retained.") from error
    return CoverageRunResult(execution=execution, coverage=coverage, artifacts=artifacts, source_hints=source_hints)


def _export_coverage(
    root: Path, artifacts: Path, execution: ExecutionResult, timeout: int,
) -> dict[str, Any]:
    observation: dict[str, Any] = {
        "exit_code": None, "timed_out": False, "error": None,
        "duration_seconds": 0.0, "stdout": "", "stderr": "",
        "stdout_truncated": False, "stderr_truncated": False,
    }
    if execution.kind in {ExecutionKind.LAUNCH_ERROR, ExecutionKind.TIMEOUT}:
        observation["error"] = "coverage_not_finalized: pytest did not finish normally."
    elif not (artifacts / ".coverage").is_file():
        observation["error"] = "coverage_data_missing: no database from this execution."
    else:
        command = [
            sys.executable, "-P", "-m", "coverage", "json",
            f"--rcfile={artifacts / '.coveragerc'}", f"--data-file={artifacts / '.coverage'}",
            "-o", str(artifacts / "coverage.json"),
            # Explicit filename, not a glob: paths with spaces/brackets are safe.
            str(root / SUBJECT_FILENAME),
        ]
        started = perf_counter()
        try:
            with (artifacts / "coverage_export.stdout.txt").open("wb") as stdout_file, (
                artifacts / "coverage_export.stderr.txt"
            ).open("wb") as stderr_file:
                try:
                    process = subprocess.run(
                        command, cwd=root, env=_test_environment(artifacts / "pytest_result.json", root),
                        shell=False, stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file,
                        timeout=min(timeout, 30), check=False,
                    )
                    observation["exit_code"] = process.returncode
                    if process.returncode != 0:
                        observation["error"] = "coverage_export_failed: see coverage_export logs."
                except subprocess.TimeoutExpired:
                    observation["timed_out"] = True
                    observation["error"] = "coverage_export_timeout: execution result is retained."
                except OSError:
                    observation["error"] = "coverage_export_launch_failed: execution result is retained."
            observation["stdout"], observation["stdout_truncated"] = _read_log(artifacts / "coverage_export.stdout.txt")
            observation["stderr"], observation["stderr_truncated"] = _read_log(artifacts / "coverage_export.stderr.txt")
        except OSError:
            observation["error"] = "coverage_export_io_failed: execution result is retained."
        observation["duration_seconds"] = perf_counter() - started
    try:
        (artifacts / "coverage_export.json").write_text(
            json.dumps(observation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    except OSError as error:
        raise ExecutionArtifactError("Unable to save coverage export diagnostics; execution.json is retained.") from error
    return observation


def parse_coverage_json(report_path: str | Path, *, target_file: str | Path) -> CoverageResult:
    """Read exact target metrics; reject absent/extra files and malformed counters.

    The file summary's combined percentage and global totals are intentionally
    unused. Line/branch percentages are computed by CoverageResult separately.
    """
    target = Path(target_file).resolve()
    try:
        data = json.loads(Path(report_path).read_text(encoding="utf-8"))
        if data["meta"]["branch_coverage"] is not True:
            raise ValueError("branch coverage was not measured")
        files = data["files"]
        if not isinstance(files, dict) or len(files) != 1:
            raise ValueError("report must contain only the target")
        filename, result = next(iter(files.items()))
        path = Path(filename)
        if not path.is_absolute():
            path = target.parent / path
        if path.resolve() != target:
            raise ValueError("report belongs to another target")
        summary = result["summary"]
        missing_lines, missing_branches = result["missing_lines"], result["missing_branches"]
        if not isinstance(missing_lines, list) or not isinstance(missing_branches, list):
            raise ValueError("invalid missing positions")
        if any(not isinstance(arc, list) or len(arc) != 2 for arc in missing_branches):
            raise ValueError("invalid branch arcs")
        return CoverageResult(
            target_file=target, status="available",
            covered_lines=summary["covered_lines"], total_lines=summary["num_statements"],
            covered_branches=summary["covered_branches"], total_branches=summary["num_branches"],
            missing_lines=tuple(missing_lines),
            missing_branches=tuple(BranchArc(start=start, end=end) for start, end in missing_branches),
        )
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        return CoverageResult(
            target_file=target, status="unavailable",
            error="coverage_report_invalid: missing, malformed, non-branch or wrong-target JSON.",
        )
