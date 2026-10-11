"""Exercise the Workflow HTTP entry point, feedback and exports."""

import json
from pathlib import Path
import threading
import time
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from testgen_agent.config import AppConfig

from test_workflow.service import fixture_client, sample_inputs as samples
from test_workflow.web import WebServer, validate_payload


@pytest.fixture
def server(tmp_path):
    with WebServer(("127.0.0.1", 0), tmp_path) as app:
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        yield app, f"http://127.0.0.1:{app.server_port}"
        app.shutdown()
        thread.join(timeout=5)


def call(base, path, payload=None, headers=None):
    request = Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                      headers={"Content-Type": "application/json", **(headers or {})})
    with urlopen(request, timeout=10) as response:
        raw = response.read()
        return response.status, json.loads(raw) if "application/json" in response.headers["Content-Type"] else raw


def wait_job(base, identity):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        _, job = call(base, f"/api/runs/{identity}")
        if job["status"] in ("finished", "failed"):
            return job
        time.sleep(0.05)
    pytest.fail("Web workflow did not settle")


@pytest.mark.parametrize("sample,status,rounds", [
    ("calculation", "completed", 1), ("validation", "completed", 2), ("discount", "needs_review", 1),
])
def test_http_workflow_with_fixture_model(server, sample, status, rounds):
    _, base = server
    config = AppConfig(model="fixture-model", api_key="fixture-key")
    with patch("test_workflow.web.load_config", return_value=config), patch(
            "test_workflow.web.DeepSeekClient", return_value=fixture_client(sample)) as factory:
        code, created = call(base, "/api/runs", samples()[sample])
        assert code == 202
        job = wait_job(base, created["id"])
    assert job["status"] == "finished" and job["summary"]["task_status"] == status
    assert job["summary"]["rounds"] == rounds
    factory.assert_called_once_with(config)
    assert job["state"]["config"]["mode"] == "real"
    assert job["summary"]["pytest_processes"] == rounds
    assert job["summary"]["agents"]["result_summary"]["committed_results"] == 1
    assert job["events"] and job["messages"] and job["reportMarkdown"] and job["testCode"]
    if sample == "validation":
        feedback = next(message for message in job["messages"] if message["type"] == "execution_feedback")
        assert feedback["message_id"] in job["state"]["consumed_message_ids"]
    for name, url in job["downloads"].items():
        assert call(base, url)[1] == (Path(job["outputDir"]) / {
            "tests": "final/test_subject.py", "source": "final/subject.py",
            "report": "report.md", "summary": "summary.json", "state": "state.json"}[name]).read_bytes()


def test_http_real_model_wiring_and_secret_redaction(server):
    app, base = server
    config = AppConfig(model="test-model", api_key="secret-must-not-appear")
    with patch("test_workflow.web.load_config", return_value=config) as load, patch(
            "test_workflow.web.DeepSeekClient", return_value=fixture_client("calculation")) as client:
        _, bootstrap = call(base, "/api/bootstrap")
        assert bootstrap["config"]["ready"] is True
        assert "samples" not in bootstrap
        _, created = call(base, "/api/runs", {**samples()["calculation"], "mode": "real", "max_rounds": 1, "timeout": 10})
        job = wait_job(base, created["id"])
    client.assert_called_once_with(config)
    assert all(item.kwargs == {"env_file": app.store.env_file} for item in load.call_args_list)
    assert job["summary"]["task_status"] == "completed" and job["state"]["config"]["mode"] == "real"
    assert job["state"]["config"]["max_rounds"] == 1
    assert "secret-must-not-appear" not in json.dumps([bootstrap, job])


@pytest.mark.parametrize("change", [
    {"source": ""}, {"spec": ""}, {"mode": "offline"}, {"max_rounds": True},
    {"max_rounds": 4}, {"timeout": 0}, {"spec": None}, {"source": 123},
])
def test_invalid_submission_creates_no_job(server, change):
    app, base = server
    payload = {**samples()["calculation"], **change}
    with pytest.raises(HTTPError) as error:
        call(base, "/api/runs", payload)
    assert error.value.code == 400 and not app.store._jobs
    assert not (app.store.root / "outputs").exists()


def test_http_assets_and_request_boundaries(server):
    app, base = server
    html = call(base, "/")[1]
    assert b"/app.js" in html
    assert b'<select' not in html
    assert b'<textarea id="source" spellcheck="false" required></textarea>' in html
    assert b'<textarea id="spec" required></textarea>' in html
    assert b"fetch(" in call(base, "/app.js")[1]
    assert call(base, "/styles.css")[0] == 200
    for headers, expected in [({"Origin": "https://elsewhere.example"}, 403),
                               ({"Host": "elsewhere.example"}, 403),
                               ({"Content-Type": "text/plain"}, 415)]:
        with pytest.raises(HTTPError) as error:
            call(base, "/api/runs", {}, headers)
        assert error.value.code == expected
    app.store.busy.acquire()
    try:
        with pytest.raises(HTTPError) as error:
            call(base, "/api/runs", samples()["calculation"])
        assert error.value.code == 409
    finally:
        app.store.busy.release()
    for path in ("/api/runs/missing", "/api/runs/missing/files/tests", "/../../.env"):
        with pytest.raises(HTTPError) as error:
            call(base, path)
        assert error.value.code == 404


def test_job_failure_releases_slot_and_does_not_echo_configuration(server):
    app, base = server
    with patch("test_workflow.web.load_config", side_effect=ValueError("secret-error-value")):
        _, created = call(base, "/api/runs", {**samples()["calculation"], "mode": "real"})
        failed = wait_job(base, created["id"])
    assert failed["status"] == "failed" and "secret-error-value" not in json.dumps(failed)
    assert not app.store.busy.locked()


def test_only_loopback_is_allowed(tmp_path):
    with pytest.raises(ValueError):
        WebServer(("0.0.0.0", 0), tmp_path)
    with pytest.raises(ValueError):
        validate_payload({"source": "界" * 50000, "spec": "spec", "mode": "real"})
