"""D1: execute a host-prepared first-round candidate with measured results."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from time import perf_counter

from pydantic import ValidationError

from ..models import ExecutionKind, ExecutionResult
from ..workspace import SUBJECT_FILENAME, TEST_FILENAME, round_directory


MAX_LOG_BYTES = 16_000
_TRUNCATION_MARKER = "\n... [log truncated; full log saved in execution directory] ...\n"


class ExecutionWorkspaceError(ValueError):
    """The caller did not supply a valid, fixed-layout task workspace."""


class ExecutionArtifactError(OSError):
    """Host diagnostics could not be created or saved."""


def run_tests(workspace: str | Path, timeout: int = 30, *, round_number: int = 1) -> ExecutionResult:
    """Run only rounds/NN/test_subject.py (NN defaults to 01).

    ``workspace`` is a trusted, host-prepared task directory with ``subject.py``.
    ``timeout`` is an integer in 1..300 seconds. Invalid input raises TypeError,
    ValueError or ExecutionWorkspaceError; artifact I/O raises ExecutionArtifactError.
    Launch, pytest, timeout and diagnostic failures return ExecutionResult. Each call
    saves raw logs, plugin JSON, JUnit XML (when available), and execution.json in
    rounds/NN/executions/run-*/. This is process isolation, not a security sandbox.
    """
    return _run_tests(workspace, timeout, round_number=round_number)[0]


def _run_tests(
    workspace: str | Path, timeout: int, *, collect_coverage: bool = False, round_number: int = 1,
) -> tuple[ExecutionResult, Path]:
    """Shared D1/D2 execution; each invocation creates its own artifact directory."""
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise TypeError("timeout must be an integer")
    if not 1 <= timeout <= 300:
        raise ValueError("timeout must be in 1..300 seconds")
    if not isinstance(workspace, (str, Path)):
        raise TypeError("workspace must be a string or Path")
    root, test_path = _validate_workspace(Path(workspace), round_number)
    artifacts = _create_artifacts(root, round_number)
    report_path = artifacts / "pytest_result.json"
    config_path = artifacts / "pytest.ini"
    try:
        config_path.write_text("[pytest]\naddopts =\n", encoding="utf-8")
        command = [
            sys.executable, "-P", "-m", "pytest", "-c", str(config_path),
            "--rootdir", str(root), "--noconftest", "--import-mode=importlib",
            "-p", "no:cacheprovider",
            "-p", "testgen_agent.tools._pytest_results",
            f"--junitxml={artifacts / 'pytest.xml'}", "--color=no", "--tb=short",
            # The validated relative filename avoids pytest interpreting [] in
            # parent directory names as a parametrized node selector.
            "-rA", "-q", str(test_path.relative_to(root)),
        ]
        if collect_coverage:
            coverage_config = artifacts / ".coveragerc"
            coverage_config.write_text("[run]\nbranch = True\n[report]\nfail_under = 0\n", encoding="utf-8")
            # Never append/combine or consult workspace/project coverage config.
            command = [
                sys.executable, "-P", "-m", "coverage", "run",
                f"--rcfile={coverage_config}", f"--data-file={artifacts / '.coverage'}",
                "--branch", "-m", "pytest", *command[4:],
            ]
        environment = _test_environment(report_path, root)
        started = perf_counter()
        timed_out = False
        launch_error = None
        return_code = None
        with (artifacts / "stdout.txt").open("wb") as stdout_file, (
            artifacts / "stderr.txt"
        ).open("wb") as stderr_file:
            try:
                process = subprocess.Popen(
                    command, cwd=root, env=environment, shell=False,
                    stdin=subprocess.DEVNULL, stdout=stdout_file, stderr=stderr_file,
                    start_new_session=os.name != "nt",
                )
            except OSError as error:
                launch_error = f"pytest process could not start ({type(error).__name__})."
            else:
                try:
                    return_code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _stop_process(process)
                    return_code = process.returncode
                except KeyboardInterrupt:
                    _stop_process(process)
                    raise
        elapsed = perf_counter() - started
        stdout, stdout_truncated = _read_log(artifacts / "stdout.txt")
        stderr, stderr_truncated = _read_log(artifacts / "stderr.txt")
        counters, details, complete, recorded_exit, report_error = _read_observations(report_path)
        note = ""
        if launch_error:
            kind = ExecutionKind.LAUNCH_ERROR
            counters = {}
            note = launch_error
        elif timed_out:
            kind = ExecutionKind.TIMEOUT
            note = f"Execution exceeded {timeout} seconds; process terminated."
            if report_error:
                note += f" {report_error}"
        else:
            kind = _classify(return_code, counters)
            if report_error or not complete or recorded_exit != return_code:
                kind = ExecutionKind.RESULT_ERROR
                counters = {}
                note = report_error or "Incomplete or inconsistent pytest result checkpoint."
            elif kind == ExecutionKind.RESULT_ERROR:
                note = "Unexpected pytest exit code or incomplete test outcomes."
        diagnostic_text = "\n".join(part for part in (stderr, note, details) if part)
        stderr, details_truncated = _truncate_text(diagnostic_text)
        fields = dict(
            kind=kind, exit_code=return_code, **counters,
            stdout=stdout, stderr=stderr, timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated or details_truncated,
            duration_seconds=elapsed,
        )
        try:
            result = ExecutionResult(**fields)
        except ValidationError:
            # Keep the actual exit code even when the structured observation is bad.
            stderr, truncated = _truncate_text(
                diagnostic_text + "\nPytest result violates the execution counter contract."
            )
            result = ExecutionResult(
                kind=ExecutionKind.RESULT_ERROR, exit_code=return_code,
                stdout=stdout, stderr=stderr, duration_seconds=elapsed,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated or truncated,
            )
        (artifacts / "execution.json").write_text(
            result.canonical_json(indent=2) + "\n", encoding="utf-8",
        )
        return result, artifacts
    except OSError as error:
        raise ExecutionArtifactError("Unable to save or read execution artifacts.") from error


def _contained_path(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise ExecutionWorkspaceError("Workspace paths cannot be symbolic links or junctions.")
    try:
        current.resolve().relative_to(root)
    except ValueError:
        raise ExecutionWorkspaceError("Execution path leaves the task workspace.") from None
    return current


def _validate_workspace(workspace: Path, round_number: int = 1) -> tuple[Path, Path]:
    relative = round_directory(round_number)
    if workspace.is_symlink() or workspace.is_junction():
        raise ExecutionWorkspaceError("Workspace cannot be a symbolic link or junction.")
    try:
        root = workspace.resolve(strict=True)
    except OSError as error:
        raise ExecutionWorkspaceError("Task workspace does not exist.") from error
    if not root.is_dir():
        raise ExecutionWorkspaceError("Task workspace must be a directory.")
    subject = _contained_path(root, Path(SUBJECT_FILENAME))
    test_path = _contained_path(root, relative / TEST_FILENAME)
    if not subject.is_file() or not test_path.is_file():
        raise ExecutionWorkspaceError(f"Workspace needs subject.py and {relative / TEST_FILENAME}.")
    return root, test_path


def _create_artifacts(root: Path, round_number: int = 1) -> Path:
    parent = _contained_path(root, round_directory(round_number) / "executions")
    try:
        parent.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    except OSError as error:
        raise ExecutionArtifactError("Unable to create execution directory.") from error


def _test_environment(report_path: Path, workspace: Path) -> dict[str, str]:
    environment = _process_environment()
    environment.update(
        PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONUNBUFFERED="1",
        # Only the host package and fixed subject root are added. Using the OS
        # path separator also handles workspace names containing spaces/Chinese.
        PYTHONPATH=os.pathsep.join((str(Path(__file__).resolve().parents[2]), str(workspace))),
        TESTGEN_EXECUTION_REPORT=str(report_path),
    )
    return environment


def _process_environment() -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _stop_process(process: subprocess.Popen) -> None:
    if os.name == "nt":
        taskkill = Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=5, check=False, shell=False, env=_process_environment(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait()


def _read_log(path: Path) -> tuple[str, bool]:
    with path.open("rb") as stream:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return stream.read().decode("utf-8", errors="replace"), False
        head = stream.read(MAX_LOG_BYTES // 2)
        stream.seek(-MAX_LOG_BYTES // 2, os.SEEK_END)
        tail = stream.read()
    return head.decode("utf-8", errors="replace") + _TRUNCATION_MARKER + tail.decode(
        "utf-8", errors="replace",
    ), True


def _truncate_text(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_LOG_BYTES:
        return text, False
    return text[:MAX_LOG_BYTES // 2] + _TRUNCATION_MARKER + text[-MAX_LOG_BYTES // 2:], True


def _read_observations(path: Path) -> tuple[dict, str, bool, int | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data["schema_version"]) is not int or data["schema_version"] != 1 or type(data["completed"]) is not bool:
            raise ValueError("invalid checkpoint header")
        if data["exit_code"] is not None and type(data["exit_code"]) is not int:
            raise ValueError("invalid checkpoint exit code")
        items = data["items"]
        if items is not None and (
            not isinstance(items, list) or not all(isinstance(item, str) for item in items)
            or len(items) != len(set(items))
        ):
            raise ValueError("invalid collected items")
        reports = data["reports"]
        started_calls = data["started_calls"]
        collection_errors = data["collection_errors"]
        if not isinstance(reports, list) or not isinstance(collection_errors, list):
            raise ValueError("invalid reports")
        if (
            not isinstance(started_calls, list)
            or not all(isinstance(nodeid, str) for nodeid in started_calls)
            or len(started_calls) != len(set(started_calls))
            or any(items is None or nodeid not in items for nodeid in started_calls)
        ):
            raise ValueError("invalid call phase entries")
        grouped: dict[str, dict] = {}
        details = []
        for report in reports:
            nodeid, phase = report["nodeid"], report["when"]
            if (
                items is None or nodeid not in items or phase not in {"setup", "call", "teardown"}
                or report["outcome"] not in {"passed", "failed", "skipped"}
                or type(report["assertion"]) is not bool or type(report["xfail"]) is not bool
                or not isinstance(report["detail"], str)
            ):
                raise ValueError("invalid item report")
            phases = grouped.setdefault(nodeid, {})
            if phase == "call" and nodeid not in started_calls:
                raise ValueError("call report has no observed phase entry")
            if phase in phases:
                raise ValueError("duplicate phase report")
            phases[phase] = report
            if report["detail"]:
                location = report["location"]
                if (
                    not isinstance(location, list) or len(location) != 3
                    or not isinstance(location[0], str) or type(location[1]) is not int
                    or location[1] < 0 or not isinstance(location[2], str)
                ):
                    raise ValueError("invalid report location")
                details.append(f"{nodeid} ({phase}, {location[0]}:{location[1] + 1})\n{report['detail']}")
        if data["completed"] and data["exit_code"] in (0, 1):
            if items is None:
                raise ValueError("missing collection result")
            for nodeid in items:
                phases = grouped.get(nodeid, {})
                if "setup" not in phases or "teardown" not in phases:
                    raise ValueError("incomplete item lifecycle")
                if phases["setup"]["outcome"] == "passed" and "call" not in phases:
                    raise ValueError("missing test call")
        for error in collection_errors:
            if not isinstance(error["nodeid"], str) or not isinstance(error["detail"], str):
                raise ValueError("invalid collection error")
            details.append(f"{error['nodeid']} (collection)\n{error['detail']}")
        counters = dict(
            collected=len(items) if items is not None else None,
            executed=len(started_calls),
            passed=0, failed=0, errors=len(collection_errors), skipped=0, xfailed=0, xpassed=0,
        )
        # Missing fields preserve compatibility with older result checkpoints.
        counters["failures"] = tuple(
            report["failure"] for report in (*reports, *collection_errors)
            if report.get("failure") is not None
        )
        for phases in grouped.values():
            if any(report["outcome"] == "failed" for phase, report in phases.items() if phase != "call"):
                counters["errors"] += 1
            else:
                call = phases.get("call")
                skipped_phase = next((report for report in phases.values() if report["outcome"] == "skipped"), None)
                terminal = call if call and call["outcome"] == "failed" else (
                    skipped_phase or call or phases.get("setup")
                )
                if terminal is None:
                    continue
                outcome = terminal["outcome"]
                if outcome == "skipped":
                    counters["xfailed" if terminal["xfail"] else "skipped"] += 1
                elif call:
                    if outcome == "failed":
                        counters["failed" if call["assertion"] else "errors"] += 1
                    else:
                        counters["xpassed" if call["xfail"] else "passed"] += 1
        return counters, "\n\n".join(details), data["completed"], data["exit_code"], None
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return {}, "", False, None, "Structured pytest checkpoint is missing or invalid."


def _classify(exit_code: int, counters: dict) -> ExecutionKind:
    if exit_code == 0:
        return ExecutionKind.PASSED
    if exit_code == 1:
        return ExecutionKind.EXECUTION_ERROR if counters.get("errors") else ExecutionKind.ASSERTION_FAILED
    if exit_code == 2:
        return ExecutionKind.COLLECTION_ERROR if counters.get("errors") else ExecutionKind.INTERRUPTED
    return {
        3: ExecutionKind.INTERNAL_ERROR, 4: ExecutionKind.USAGE_ERROR, 5: ExecutionKind.NO_TESTS,
    }.get(exit_code, ExecutionKind.RESULT_ERROR)
