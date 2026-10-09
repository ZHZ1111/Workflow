from __future__ import annotations

from pathlib import Path

import pytest

from testgen_agent.web import STATIC_ROOT, WebJob, _job_snapshot, build_bootstrap, validate_run_payload


def test_web_assets_are_packaged() -> None:
    assert (STATIC_ROOT / "index.html").is_file()
    assert (STATIC_ROOT / "styles.css").is_file()
    assert (STATIC_ROOT / "app.js").is_file()


def test_bootstrap_reports_configuration_without_ui_scenarios(tmp_path: Path) -> None:
    result = build_bootstrap(tmp_path)

    assert result == {"config": {"ready": False, "model": None}}


def test_validate_run_payload_accepts_only_user_inputs() -> None:
    result = validate_run_payload({
        "source": "def answer():\n    return 42\n",
        "spec": "answer 返回 42",
    })

    assert result == {
        "source": "def answer():\n    return 42\n",
        "spec": "answer 返回 42",
    }


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"source": "", "spec": "x"}, "Python 源码"),
        ({"source": "x", "spec": ""}, "行为规格"),
    ],
)
def test_validate_run_payload_rejects_invalid_values(payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_run_payload(payload)


def test_job_snapshot_exposes_report_only_after_report_file_exists(tmp_path: Path) -> None:
    output = tmp_path / "run"
    output.mkdir()
    job = WebJob(job_id="report-test", status="running", output_dir=output)

    assert _job_snapshot(job)["reportMarkdown"] is None

    report = "# 自动测试生成报告\n\n最终结论：通过\n"
    (output / "report.md").write_text(report, encoding="utf-8")
    snapshot = _job_snapshot(job)

    assert snapshot["reportMarkdown"] == report
