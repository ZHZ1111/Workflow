"""D2: real coverage observations and controlled report/export failure paths."""

import json
import subprocess

import pytest

from testgen_agent.models import BranchArc, CoverageResult, ExecutionKind
from testgen_agent.tools import (
    build_coverage_source_hints, parse_coverage_json, run_tests_with_coverage,
)
from testgen_agent.tools import coverage_runner as collector


BRANCH_SOURCE = "def choose(x):\n    if x > 0:\n        return 1\n    return -1\n"


def prepare(tmp_path, test_code, source=BRANCH_SOURCE):
    root = tmp_path / "task [one] 中文"
    (root / "rounds/01").mkdir(parents=True)
    (root / "subject.py").write_text(source, encoding="utf-8")
    (root / "rounds/01/test_subject.py").write_text(test_code, encoding="utf-8")
    return root


def test_real_line_and_branch_percentages_are_distinct_and_target_only(tmp_path):
    root = prepare(tmp_path, "from subject import choose\ndef test_positive():\n    assert choose(1) == 1\n")
    source_before = (root / "subject.py").read_bytes()
    result = run_tests_with_coverage(root)

    assert result.execution.all_passed and result.candidate_all_passed
    assert result.coverage.status == "available", result.coverage.error
    assert (result.coverage.covered_lines, result.coverage.total_lines) == (3, 4)
    assert (result.coverage.covered_branches, result.coverage.total_branches) == (1, 2)
    assert result.coverage.line_percent == 75 and result.coverage.branch_percent == 50
    assert result.coverage.missing_lines == (4,)
    assert result.coverage.missing_branches == (BranchArc(start=2, end=4),)
    assert not result.coverage.meets_target
    raw = json.loads((result.artifacts / "coverage.json").read_text(encoding="utf-8"))
    assert set(raw["files"]) == {"subject.py"}
    assert raw["totals"]["percent_covered"] != result.coverage.line_percent
    assert (root / "subject.py").read_bytes() == source_before
    assert (result.artifacts / ".coverage").is_file()
    saved = CoverageResult.model_validate_json((result.artifacts / "coverage_result.json").read_text(encoding="utf-8"))
    assert saved == result.coverage
    combined = json.loads((result.artifacts / "run_result.json").read_text(encoding="utf-8"))
    assert combined["candidate_all_passed"] and combined["execution"]["kind"] == "passed"
    missing_line, = result.source_hints["missing_lines"]
    assert missing_line == {"line": 4, "kind": "source_line", "source": "    return -1", "function": "choose"}
    arc, = result.source_hints["missing_branches"]
    assert arc["start_location"]["source"] == "    if x > 0:"
    assert arc["end_location"] == missing_line


def test_fresh_database_ignores_previous_full_coverage_and_external_configuration(tmp_path, monkeypatch):
    root = prepare(tmp_path, "from subject import choose\ndef test_all():\n    assert choose(1) == 1\n    assert choose(0) == -1\n")
    first = run_tests_with_coverage(root)
    assert first.coverage.line_percent == first.coverage.branch_percent == 100
    first_database = (first.artifacts / ".coverage").read_bytes()
    (root / ".coverage").write_bytes(first_database)
    bad_config = root / ".coveragerc"
    bad_config.write_text("[run]\nsource = no_such_source\n[report]\nfail_under = 100\n", encoding="utf-8")
    monkeypatch.setenv("COVERAGE_FILE", str(root / ".coverage"))
    monkeypatch.setenv("COVERAGE_RCFILE", str(bad_config))
    monkeypatch.setenv("COVERAGE_PROCESS_START", str(bad_config))
    (root / "rounds/01/test_subject.py").write_text(
        "from subject import choose\ndef test_negative_only():\n    assert choose(0) == -1\n", encoding="utf-8",
    )
    second = run_tests_with_coverage(root)
    assert second.candidate_all_passed
    assert second.artifacts != first.artifacts
    assert second.coverage.line_percent == 75 and second.coverage.branch_percent == 50
    assert second.coverage.missing_lines == (3,)
    assert second.coverage.missing_branches == (BranchArc(start=2, end=3),)
    assert (root / ".coverage").read_bytes() == first_database
    assert (first.artifacts / ".coverage").read_bytes() == first_database


@pytest.mark.parametrize("test_code,kind", [
    ("from subject import choose\ndef test_bad():\n    assert choose(1) == 99\n", ExecutionKind.ASSERTION_FAILED),
    ("from subject import choose\ndef test_bad():\n    choose(1)\n    raise ValueError('runtime')\n", ExecutionKind.EXECUTION_ERROR),
    ("import missing_d2_package\ndef test_bad():\n    assert True\n", ExecutionKind.COLLECTION_ERROR),
    ("# empty tests\n", ExecutionKind.NO_TESTS),
])
def test_failed_candidates_keep_measured_coverage_without_claiming_pass(tmp_path, test_code, kind):
    result = run_tests_with_coverage(prepare(tmp_path, test_code))
    assert result.execution.kind == kind
    assert not result.candidate_all_passed
    assert result.coverage.status == "available", result.coverage.error
    combined = json.loads((result.artifacts / "run_result.json").read_text(encoding="utf-8"))
    assert combined["candidate_all_passed"] is False
    assert combined["execution"]["kind"] == kind.value
    assert combined["coverage"]["status"] == "available"


def test_coverage_of_an_unimported_target_is_zero_not_coverage_of_the_tests(tmp_path):
    result = run_tests_with_coverage(prepare(tmp_path, "def test_unrelated():\n    assert True\n"))
    assert result.execution.all_passed
    assert result.coverage.status == "available", result.coverage.error
    assert (result.coverage.covered_lines, result.coverage.total_lines) == (0, 4)
    assert (result.coverage.covered_branches, result.coverage.total_branches) == (0, 2)
    assert result.coverage.line_percent == result.coverage.branch_percent == 0


def test_zero_denominators_are_not_applicable(tmp_path):
    result = run_tests_with_coverage(prepare(
        tmp_path, "import subject\ndef test_no_statements():\n    assert True\n", source="# comment only\n",
    ))
    assert result.execution.all_passed and result.coverage.status == "available", result.coverage.error
    assert result.coverage.total_lines == result.coverage.total_branches == 0
    assert result.coverage.line_percent is result.coverage.branch_percent is None
    assert not result.coverage.meets_target


def test_straight_line_function_has_no_branch_percentage(tmp_path):
    result = run_tests_with_coverage(prepare(
        tmp_path, "from subject import double\ndef test_double():\n    assert double(3) == 6\n",
        source="def double(x):\n    return x * 2\n",
    ))
    assert result.coverage.line_percent == 100 and result.coverage.branch_percent is None
    assert result.coverage.total_branches == 0 and result.coverage.meets_target


def test_implicit_function_exit_arc_keeps_its_signed_endpoint(tmp_path):
    source = "def choose(x):\n    if x:\n        return 1\n"
    result = run_tests_with_coverage(prepare(
        tmp_path, "from subject import choose\ndef test_true():\n    assert choose(True) == 1\n", source,
    ))
    assert result.coverage.line_percent == 100 and result.coverage.branch_percent == 50
    assert result.coverage.missing_branches == (BranchArc(start=2, end=-1),)
    arc, = result.source_hints["missing_branches"]
    assert arc["end"] == -1
    assert arc["end_location"] == {
        "line": 1, "kind": "code_object_exit", "source": "def choose(x):", "function": "choose",
    }


@pytest.mark.parametrize("failure", ["exit_error", "launch_error", "timeout"])
def test_export_failure_does_not_replace_a_successful_execution(tmp_path, monkeypatch, failure):
    def fail_export(command, **kwargs):
        if failure == "launch_error":
            raise FileNotFoundError("controlled export failure")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, 2)

    monkeypatch.setattr(collector.subprocess, "run", fail_export)
    result = run_tests_with_coverage(prepare(
        tmp_path, "from subject import choose\ndef test_one():\n    assert choose(1) == 1\n",
    ))
    assert result.execution.all_passed and result.candidate_all_passed
    assert result.coverage.status == "unavailable" and result.coverage.error
    assert result.coverage.covered_lines is None and result.coverage.total_branches is None
    assert (result.artifacts / "execution.json").is_file()
    export = json.loads((result.artifacts / "coverage_export.json").read_text(encoding="utf-8"))
    assert export["error"] and export["timed_out"] == (failure == "timeout")


def test_timeout_has_unavailable_coverage_and_retains_execution_snapshot(tmp_path):
    result = run_tests_with_coverage(prepare(tmp_path, "import time\ndef test_slow():\n    time.sleep(60)\n"), timeout=2)
    assert result.execution.kind == ExecutionKind.TIMEOUT and result.execution.timed_out
    assert result.coverage.status == "unavailable" and not result.candidate_all_passed
    assert "not_finalized" in result.coverage.error
    assert (result.artifacts / "execution.json").is_file()


def test_modified_source_invalidates_coverage_linking_without_losing_execution(tmp_path):
    result = run_tests_with_coverage(prepare(tmp_path, '''
from pathlib import Path
from subject import choose
def test_mutation():
    assert choose(1) == 1
    Path("subject.py").write_text("# changed source\\n", encoding="utf-8")
'''))
    assert result.execution.all_passed
    assert result.coverage.status == "unavailable" and "snapshot" in result.coverage.error


@pytest.mark.parametrize("damage", ["invalid_json", "outside_source"])
def test_bad_exported_json_keeps_execution_and_never_invents_source_hints(tmp_path, monkeypatch, damage):
    original_export = collector._export_coverage

    def damaged_export(root, artifacts, execution, timeout):
        observation = original_export(root, artifacts, execution, timeout)
        path = artifacts / "coverage.json"
        if damage == "invalid_json":
            path.write_text("{bad json", encoding="utf-8")
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["files"]["subject.py"]["missing_lines"] = [400]
            path.write_text(json.dumps(data), encoding="utf-8")
        return observation

    monkeypatch.setattr(collector, "_export_coverage", damaged_export)
    result = run_tests_with_coverage(prepare(
        tmp_path, "from subject import choose\ndef test_one():\n    assert choose(1) == 1\n",
    ))
    assert result.execution.all_passed and result.coverage.status == "unavailable"
    assert not result.source_hints["missing_lines"] and not result.source_hints["missing_branches"]


def report_payload():
    return {
        "meta": {"branch_coverage": True},
        "files": {"subject.py": {
            "summary": {"covered_lines": 3, "num_statements": 4, "covered_branches": 1, "num_branches": 2,
                        "percent_covered": 12.345},
            "missing_lines": [4], "missing_branches": [[2, 4]],
        }},
        "totals": {"num_statements": 10000, "covered_lines": 9999},
    }


def test_parser_uses_file_counts_and_ignores_combined_percentage_and_global_totals(tmp_path):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps(report_payload()), encoding="utf-8")
    result = parse_coverage_json(report, target_file=tmp_path / "subject.py")
    assert result.status == "available"
    assert result.line_percent == 75 and result.branch_percent == 50


@pytest.mark.parametrize("damage", [
    "invalid_json", "missing_file", "extra_file", "wrong_target", "not_branch",
    "missing_counter", "wrong_type", "missing_line_count", "duplicate_line",
    "bad_arc", "zero_arc", "duplicate_arc",
])
def test_malformed_or_wrong_scope_reports_are_unavailable(tmp_path, damage):
    data = report_payload()
    file = data["files"]["subject.py"]
    if damage == "missing_file":
        data["files"] = {}
    elif damage == "extra_file":
        data["files"]["test_subject.py"] = file.copy()
    elif damage == "wrong_target":
        data["files"] = {"../outside/subject.py": file}
    elif damage == "not_branch":
        data["meta"]["branch_coverage"] = False
    elif damage == "missing_counter":
        del file["summary"]["num_branches"]
    elif damage == "wrong_type":
        file["summary"]["covered_lines"] = True
    elif damage == "missing_line_count":
        file["summary"]["covered_lines"] = 4
    elif damage == "duplicate_line":
        file["missing_lines"] = [4, 4]
    elif damage == "bad_arc":
        file["missing_branches"] = [[2, 4, 5]]
    elif damage == "zero_arc":
        file["missing_branches"] = [[2, 0]]
    elif damage == "duplicate_arc":
        file["missing_branches"] = [[2, 4], [2, 4]]
    path = tmp_path / "coverage.json"
    path.write_text("{invalid" if damage == "invalid_json" else json.dumps(data), encoding="utf-8")
    result = parse_coverage_json(path, target_file=tmp_path / "subject.py")
    assert result.status == "unavailable"
    assert result.line_percent is result.branch_percent is None
    assert not result.meets_target


def test_missing_json_is_unavailable(tmp_path):
    result = parse_coverage_json(tmp_path / "not-there.json", target_file=tmp_path / "subject.py")
    assert result.status == "unavailable"


def test_source_hint_mapping_uses_innermost_function_and_rejects_out_of_range():
    code = "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    coverage = CoverageResult(
        target_file="subject.py", status="available", covered_lines=3, total_lines=4,
        covered_branches=0, total_branches=0, missing_lines=(3,),
    )
    assert build_coverage_source_hints(code, coverage)["missing_lines"][0]["function"] == "outer.inner"
    with pytest.raises(ValueError, match="outside"):
        build_coverage_source_hints(code, coverage.updated(missing_lines=(30,)))


@pytest.mark.parametrize("timeout,exception", [(True, TypeError), (2.5, TypeError), (0, ValueError), (301, ValueError)])
def test_coverage_interface_rejects_invalid_timeouts(tmp_path, timeout, exception):
    with pytest.raises(exception):
        run_tests_with_coverage(tmp_path, timeout)


def test_feedback_prompt_receives_measured_source_snippets(tmp_path):
    # Reuse the real D2 observation with a separately identified synthetic plan.
    from testgen_agent.models import CandidateRound, GeneratedTests
    from testgen_agent.prompts import build_feedback_context, TASK_DATA_START, TASK_DATA_END
    from testgen_agent.tools import analyze_source

    root = prepare(tmp_path, "from subject import choose\ndef test_positive():\n    assert choose(1) == 1\n")
    result = run_tests_with_coverage(root)
    source = analyze_source(root / "subject.py")
    generated = GeneratedTests.model_validate({
        "plan": {"target_functions": ["choose"], "oracle_mode": "specification", "scenarios": [{
            "scenario_id": "positive", "target_function": "choose", "category": "normal",
            "inputs": {"args": [1], "kwargs": {}}, "expected_behavior": "returns 1",
            "expected_basis": {"kind": "specification", "detail": "controlled test specification"},
        }]},
        "code": (root / "rounds/01/test_subject.py").read_text(encoding="utf-8"),
        "mappings": [{"test_function": "test_positive", "scenario_ids": ["positive"]}],
    })
    previous = CandidateRound(
        number=1, generated=generated, execution=result.execution, coverage=result.coverage,
        test_path="rounds/01/test_subject.py",
    )
    context = build_feedback_context(source, None, previous, requested_round=2, max_rounds=3)
    payload = json.loads(context.user_prompt.removeprefix(TASK_DATA_START + "\n").removesuffix("\n" + TASK_DATA_END))
    assert payload["coverage_source_hints"] == result.source_hints
    assert payload["observed_execution"]["all_passed"] is True
    assert payload["observed_coverage"]["line_percent"] == 75
