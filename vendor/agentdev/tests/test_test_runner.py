"""D1 checks: real subprocess observations plus controlled host failure paths."""

import json
import os
import subprocess
from time import perf_counter

import pytest
from pydantic import ValidationError

from testgen_agent.models import ExecutionKind, ExecutionResult
from testgen_agent.tools import ExecutionArtifactError, ExecutionWorkspaceError, run_tests
from testgen_agent.tools import test_runner as runner


def prepare(tmp_path, code, *, subject="def double(x):\n    return x * 2\n"):
    workspace = tmp_path / "task workspace 中文"
    workspace.mkdir()
    (workspace / "subject.py").write_text(subject, encoding="utf-8")
    tests = workspace / "rounds" / "01"
    tests.mkdir(parents=True)
    (tests / "test_subject.py").write_text(code, encoding="utf-8")
    return workspace


def artifact_dirs(workspace):
    return sorted((workspace / "rounds" / "01" / "executions").glob("run-*"))


def test_real_pass_uses_fixed_file_and_preserves_inputs(tmp_path):
    workspace = prepare(tmp_path, "from subject import double\ndef test_double():\n    assert double(2) == 4\n")
    source_before = (workspace / "subject.py").read_bytes()
    test_before = (workspace / "rounds/01/test_subject.py").read_bytes()
    # Neither additional tests nor workspace conftests/config may run.
    (workspace / "test_extra.py").write_text("raise RuntimeError('unselected')", encoding="utf-8")
    (workspace / "conftest.py").write_text("raise RuntimeError('conftest')", encoding="utf-8")
    (workspace / "pytest.ini").write_text("[pytest]\naddopts = --invalid-option", encoding="utf-8")
    result = run_tests(workspace)

    assert result.kind == ExecutionKind.PASSED and result.exit_code == 0
    assert (result.collected, result.executed, result.passed) == (1, 1, 1)
    assert result.all_passed and result.runnable
    assert (workspace / "subject.py").read_bytes() == source_before
    assert (workspace / "rounds/01/test_subject.py").read_bytes() == test_before
    artifacts, = artifact_dirs(workspace)
    assert (artifacts / "pytest.xml").is_file()
    saved = ExecutionResult.model_validate_json((artifacts / "execution.json").read_text(encoding="utf-8"))
    assert saved == result
    observation = json.loads((artifacts / "pytest_result.json").read_text(encoding="utf-8"))
    assert observation["completed"] and observation["exit_code"] == 0
    assert not (workspace / ".pytest_cache").exists()


@pytest.mark.parametrize("body,kind,failed,errors", [
    ("assert double(2) == 5", ExecutionKind.ASSERTION_FAILED, 1, 0),
    ("raise ValueError('bad runtime')", ExecutionKind.EXECUTION_ERROR, 0, 1),
    ("pytest.fail('explicit failure')", ExecutionKind.ASSERTION_FAILED, 1, 0),
    ("with pytest.raises(ValueError):\n        double(2)", ExecutionKind.ASSERTION_FAILED, 1, 0),
])
def test_assertions_and_call_exceptions_have_distinct_kinds(tmp_path, body, kind, failed, errors):
    workspace = prepare(tmp_path, f"import pytest\nfrom subject import double\ndef test_bad():\n    {body}\n")
    result = run_tests(workspace)
    assert result.kind == kind and result.exit_code == 1
    assert (result.collected, result.executed, result.failed, result.errors) == (1, 1, failed, errors)
    assert not result.all_passed
    assert "test_subject.py" in result.stderr and "test_bad" in result.stderr


def test_parameterized_phase_outcomes_partition_items_without_double_counting(tmp_path):
    workspace = prepare(tmp_path, '''
import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("setup problem")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("teardown problem")

@pytest.mark.parametrize("n", [0, 1])
def test_values(n):
    assert n == 0

def test_runtime():
    raise ValueError("call problem")

def test_setup(broken_setup):
    assert True

def test_teardown(broken_teardown):
    assert False

@pytest.mark.skip(reason="controlled skip")
def test_skipped():
    assert False

@pytest.mark.xfail(reason="controlled xfail")
def test_xfailed():
    assert False

@pytest.mark.xfail(reason="controlled xpass")
def test_xpassed():
    assert True
''')
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.EXECUTION_ERROR and result.exit_code == 1
    assert (result.collected, result.executed) == (8, 6)
    assert (result.passed, result.failed, result.errors) == (1, 1, 3)
    assert (result.skipped, result.xfailed, result.xpassed) == (1, 1, 1)
    assert not result.runnable and not result.all_passed
    assert "setup problem" in result.stderr and "teardown problem" in result.stderr


@pytest.mark.parametrize("code", [
    "import package_that_does_not_exist_d1\ndef test_one():\n    assert True\n",
    "def test_broken(:\n    pass\n",
    "from subject import missing_function\ndef test_one():\n    assert True\n",
])
def test_import_and_syntax_collection_errors(tmp_path, code):
    workspace = prepare(tmp_path, code)
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.COLLECTION_ERROR and result.exit_code == 2
    assert result.collected == 0 and result.executed == 0 and result.errors == 1
    assert not result.all_passed and not result.runnable
    assert "test_subject.py" in result.stderr


@pytest.mark.parametrize("code", [
    "# deliberately empty test file\n",
    "import pytest\npytest.skip('module skip', allow_module_level=True)\n",
])
def test_no_tests_never_reports_success(tmp_path, code):
    result = run_tests(prepare(tmp_path, code))
    assert result.kind == ExecutionKind.NO_TESTS and result.exit_code == 5
    assert result.collected == result.executed == result.passed == 0
    assert not result.all_passed


@pytest.mark.parametrize("marker,body,kind,outcome,executed", [
    ("@pytest.mark.skip(reason='skip')", "assert False", ExecutionKind.PASSED, "skipped", 0),
    ("@pytest.mark.xfail(reason='xfail')", "assert False", ExecutionKind.PASSED, "xfailed", 1),
    ("@pytest.mark.xfail(reason='xpass')", "assert True", ExecutionKind.PASSED, "xpassed", 1),
    ("@pytest.mark.xfail(reason='strict', strict=True)", "assert True", ExecutionKind.ASSERTION_FAILED, "failed", 1),
])
def test_only_bypassed_items_are_not_all_passed(tmp_path, marker, body, kind, outcome, executed):
    result = run_tests(prepare(tmp_path, f"import pytest\n{marker}\ndef test_bypass():\n    {body}\n"))
    assert result.kind == kind
    assert getattr(result, outcome) == 1 and result.executed == executed
    assert not result.all_passed


def test_teardown_skip_does_not_hide_behind_a_passing_call(tmp_path):
    workspace = prepare(tmp_path, '''
import pytest
@pytest.fixture
def skipped_teardown():
    yield
    pytest.skip("teardown was skipped")
def test_one(skipped_teardown):
    assert True
''')
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.PASSED and result.exit_code == 0
    assert (result.collected, result.executed, result.passed, result.skipped) == (1, 1, 0, 1)
    assert not result.all_passed


def test_early_success_exit_during_teardown_is_not_complete(tmp_path):
    workspace = prepare(tmp_path, '''
import pytest
@pytest.fixture
def incomplete_teardown():
    yield
    pytest.exit("early exit", returncode=0)
def test_one(incomplete_teardown):
    assert True
''')
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.RESULT_ERROR and result.exit_code == 0
    assert not result.all_passed


def test_teardown_skip_keeps_an_existing_assertion_failure(tmp_path):
    workspace = prepare(tmp_path, '''
import pytest
@pytest.fixture
def skipped_teardown():
    yield
    pytest.skip("teardown skipped after failure")
def test_one(skipped_teardown):
    assert False
''')
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.ASSERTION_FAILED and result.exit_code == 1
    assert (result.collected, result.executed, result.failed, result.skipped) == (1, 1, 1, 0)
    assert not result.all_passed


def test_timeout_terminates_and_preserves_last_complete_checkpoint(tmp_path):
    workspace = prepare(tmp_path, '''
import time
def test_first():
    assert True
def test_slow():
    print("before timeout", flush=True)
    time.sleep(60)
''')
    started = perf_counter()
    result = run_tests(workspace, timeout=2)
    assert perf_counter() - started < 9
    assert result.kind == ExecutionKind.TIMEOUT and result.timed_out
    assert result.exit_code is not None and not result.all_passed
    assert result.collected == result.executed == 2 and result.passed == 1
    artifacts, = artifact_dirs(workspace)
    partial = json.loads((artifacts / "pytest_result.json").read_text(encoding="utf-8"))
    assert not partial["completed"]
    assert (artifacts / "execution.json").is_file()


def test_returned_logs_are_bounded_full_logs_and_failure_location_survive(tmp_path):
    workspace = prepare(tmp_path, '''
import sys
def test_long_output():
    print("中文" * 20000)
    print("stderr-end-marker", file=sys.stderr)
    assert False, "failure-location-marker"
''')
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.ASSERTION_FAILED
    assert result.stdout_truncated and len(result.stdout) < runner.MAX_LOG_BYTES + 100
    assert "failure-location-marker" in result.stderr and "test_subject.py:6" in result.stderr
    artifacts, = artifact_dirs(workspace)
    assert (artifacts / "stdout.txt").stat().st_size > runner.MAX_LOG_BYTES
    assert "中文" in result.stdout


def test_environment_excludes_credentials_and_inherited_pytest_configuration(tmp_path, monkeypatch):
    for name, value in {
        "DEEPSEEK_API_KEY": "secret-value", "CUSTOM_LLM_SECRET": "secret-value",
        "LLM_API_KEY_ENV": "CUSTOM_LLM_SECRET", "AWS_SESSION_TOKEN": "secret-value",
        "PYTEST_ADDOPTS": "--bad-inherited-option", "PYTEST_PLUGINS": "missing_plugin",
        "PYTHONPATH": "inherited-missing-path",
    }.items():
        monkeypatch.setenv(name, value)
    workspace = prepare(tmp_path, '''
import os
from subject import double
def test_environment():
    for name in ("DEEPSEEK_API_KEY", "CUSTOM_LLM_SECRET", "LLM_API_KEY_ENV",
                 "AWS_SESSION_TOKEN", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        assert name not in os.environ
    assert "inherited-missing-path" not in os.environ.get("PYTHONPATH", "")
    assert double(3) == 6
''')
    assert run_tests(workspace).all_passed


def test_second_run_does_not_reuse_previous_reports(tmp_path):
    workspace = prepare(tmp_path, "def test_one():\n    assert True\n")
    first = run_tests(workspace)
    first_dir, = artifact_dirs(workspace)
    first_json = (first_dir / "execution.json").read_bytes()
    (workspace / "rounds/01/test_subject.py").write_text("# no tests on second run\n", encoding="utf-8")
    second = run_tests(workspace)
    assert first.all_passed and second.kind == ExecutionKind.NO_TESTS
    assert len(artifact_dirs(workspace)) == 2
    assert (first_dir / "execution.json").read_bytes() == first_json


def test_missing_interpreter_returns_launch_error(tmp_path, monkeypatch):
    workspace = prepare(tmp_path, "def test_one():\n    assert True\n")
    monkeypatch.setattr(runner.sys, "executable", str(tmp_path / "missing-python.exe"))
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.LAUNCH_ERROR and result.exit_code is None
    assert result.collected is None and not result.all_passed
    assert "FileNotFoundError" in result.stderr


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "contradictory_counts", "exit_mismatch"])
def test_corrupted_structured_results_preserve_real_exit_code_without_success(tmp_path, monkeypatch, damage):
    workspace = prepare(tmp_path, "def test_one():\n    assert True\n")
    original = runner._read_observations

    def damaged(path):
        if damage == "missing":
            path.unlink()
        elif damage == "invalid_json":
            path.write_text("{invalid", encoding="utf-8")
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            if damage == "contradictory_counts":
                data["items"].append("phantom-item")
            else:
                data["exit_code"] = 1
            path.write_text(json.dumps(data), encoding="utf-8")
        return original(path)

    monkeypatch.setattr(runner, "_read_observations", damaged)
    result = run_tests(workspace)
    assert result.kind == ExecutionKind.RESULT_ERROR and result.exit_code == 0
    assert not result.all_passed and not result.runnable
    assert result.passed is None


@pytest.mark.parametrize("exit_code,kind", [
    (2, ExecutionKind.INTERRUPTED), (3, ExecutionKind.INTERNAL_ERROR),
    (4, ExecutionKind.USAGE_ERROR), (7, ExecutionKind.RESULT_ERROR),
])
def test_controlled_pytest_exit_statuses(tmp_path, exit_code, kind):
    workspace = prepare(tmp_path, f"import pytest\ndef test_exit():\n    pytest.exit('controlled exit', returncode={exit_code})\n")
    result = run_tests(workspace)
    assert result.exit_code == exit_code and result.kind == kind
    assert not result.all_passed


@pytest.mark.parametrize("timeout,error", [
    (True, TypeError), (2.5, TypeError), ("30", TypeError),
    (0, ValueError), (-1, ValueError), (301, ValueError),
])
def test_invalid_timeout_is_rejected_before_launch(tmp_path, timeout, error):
    with pytest.raises(error):
        run_tests(tmp_path / "not-created", timeout)


def test_invalid_workspace_and_artifact_path_have_explicit_errors(tmp_path):
    with pytest.raises(TypeError):
        run_tests(None)
    with pytest.raises(ExecutionWorkspaceError):
        run_tests(tmp_path / "missing")
    with pytest.raises(ExecutionWorkspaceError):
        run_tests(tmp_path)
    workspace = prepare(tmp_path, "def test_one():\n    assert True\n")
    (workspace / "rounds/01/executions").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ExecutionArtifactError):
        run_tests(workspace)


def test_directory_link_cannot_redirect_tests_outside_workspace(tmp_path):
    workspace = tmp_path / "task"
    workspace.mkdir()
    (workspace / "subject.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    (outside / "01").mkdir(parents=True)
    (outside / "01/test_subject.py").write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    link = workspace / "rounds"
    if os.name == "nt":
        completed = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, check=False,
        )
        assert completed.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExecutionWorkspaceError):
        run_tests(workspace)


def test_result_error_contract_never_claims_runnable_or_all_passed():
    result = ExecutionResult(kind="result_error", exit_code=0)
    assert not result.runnable and not result.all_passed
    with pytest.raises(ValidationError):
        result.updated(exit_code=None)
