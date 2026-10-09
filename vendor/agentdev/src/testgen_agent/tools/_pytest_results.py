"""Host-loaded pytest hooks for D1; no model output or console parsing."""

from __future__ import annotations

import json
import os
import traceback
from pathlib import Path

import pytest


class _Recorder:
    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.data = {
            "schema_version": 1,
            "items": None,
            "reports": [],
            "started_calls": [],
            "collection_errors": [],
            "completed": False,
            "exit_code": None,
        }

    def save(self) -> None:
        # Replace atomically so a timed-out process leaves the last full checkpoint.
        temporary = self.destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.destination)

    def pytest_sessionstart(self, session) -> None:
        self.save()

    def pytest_collection_finish(self, session) -> None:
        self.data["items"] = [item.nodeid for item in session.items]
        self.save()

    def pytest_collectreport(self, report) -> None:
        if report.failed:
            self.data["collection_errors"].append({
                "nodeid": report.nodeid,
                "detail": str(report.longrepr),
                "failure": getattr(report, "testgen_failure", None),
            })
            self.save()

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_make_collect_report(self, collector):
        outcome = yield
        report = outcome.get_result()
        call = getattr(report, "call", None)
        if call is not None and call.excinfo is not None:
            exception = call.excinfo.value
            # pytest wraps import/syntax exceptions in CollectError. Retain the
            # original exception object, not a guess from rendered diagnostics.
            if isinstance(exception, pytest.Collector.CollectError) and exception.__cause__ is not None:
                exception = exception.__cause__
            report.testgen_failure = _exception_failure(report.nodeid, "collection", exception)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_call(self, item):
        # A hung call never produces logreport. Record phase entry separately so
        # timeout observations count tests that actually started, not only ended.
        self.data["started_calls"].append(item.nodeid)
        self.save()
        yield

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()
        # pytest.fail()/raises failures and strict XPASS are pytest failures;
        # exceptions such as ValueError in the test call are execution errors.
        report.testgen_assertion = (
            call.excinfo is None
            or call.excinfo.errisinstance((AssertionError, pytest.fail.Exception))
        )
        report.testgen_failure = None
        if call.excinfo is not None:
            report.testgen_failure = _exception_failure(report.nodeid, report.when, call.excinfo.value)

    def pytest_runtest_logreport(self, report) -> None:
        self.data["reports"].append({
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "assertion": getattr(report, "testgen_assertion", False),
            "xfail": hasattr(report, "wasxfail"),
            "location": list(report.location),
            "detail": str(report.longrepr) if report.failed or report.skipped else "",
            "failure": getattr(report, "testgen_failure", None) if report.failed else None,
        })
        self.save()

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus) -> None:
        self.data["completed"] = True
        self.data["exit_code"] = int(exitstatus)
        self.save()


def _exception_failure(nodeid: str, phase: str, exception: BaseException) -> dict:
    frames = traceback.extract_tb(exception.__traceback__)
    frame = frames[-1] if frames else None
    return {"nodeid": nodeid, "phase": phase, "exception_type": type(exception).__name__,
            "path": exception.filename if isinstance(exception, SyntaxError) else frame.filename if frame else None,
            "line": exception.lineno if isinstance(exception, SyntaxError) else frame.lineno if frame else None,
            "missing_module": exception.name if isinstance(exception, ModuleNotFoundError) else None}


def pytest_configure(config) -> None:
    destination = Path(os.environ["TESTGEN_EXECUTION_REPORT"])
    config.pluginmanager.register(_Recorder(destination), "testgen-result-recorder")
