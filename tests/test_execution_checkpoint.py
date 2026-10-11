"""The pytest recorder must preserve checkpoints across Windows sharing violations."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from testgen_agent.tools._pytest_results import _Recorder


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violations")
def test_recorder_recovers_without_losing_results(tmp_path):
    path = tmp_path / "pytest_result.json"
    recorder = _Recorder(path)
    recorder.data["started_calls"] = ["test_subject.py::test_example"]
    original = Path.replace
    attempts = 0

    def replace_after_scanner_releases(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary sharing violation")
        return original(source, destination)

    with patch.object(Path, "replace", replace_after_scanner_releases):
        recorder.save()
    assert attempts == 3
    assert json.loads(path.read_text(encoding="utf-8"))["started_calls"] == recorder.data["started_calls"]
    assert not path.with_suffix(".tmp").exists()
