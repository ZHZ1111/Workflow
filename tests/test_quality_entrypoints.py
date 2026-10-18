"""S6 shared service, CLI and loopback Web quality-review wiring."""

import json
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from testgen_agent.config import AppConfig

from test_workflow.cli import main
from test_workflow.config import WorkflowConfig
from test_workflow.demo.client import FixtureClient
from test_workflow.reporting import inspect_summary
from test_workflow.service import DATA_ROOT, fixture_client, fixture_review_client, run_workflow, sample_inputs
from test_workflow.state import StateStore
from test_workflow.web import validate_payload
from test_quality_generation import candidates, revision_decision
from test_workflow_web import call, server, wait_job

ROOT = Path(__file__).resolve().parents[1]


def approve_client():
    return fixture_review_client((DATA_ROOT / "quality_approve.json",))


def entry_args(output):
    return ["run", "--source", str(ROOT / "examples/calculation.py"), "--spec", str(ROOT / "examples/calculation.md"), "--output", str(output)]


def two_round_fixtures(root):
    first, second, _ = candidates()
    paths = []
    for name, content in (("first.json", first), ("second.json", second), ("revision.json", revision_decision())):
        path = root / name
        path.write_text(content.canonical_json(), encoding="utf-8")
        paths.append(path)
    return tuple(paths)


@pytest.mark.parametrize("enabled,summary", [(False, False), (False, True), (True, False), (True, True)])
def test_shared_service_composes_quality_and_summary(tmp_path, enabled, summary):
    reviewer = approve_client() if enabled else None
    state = run_workflow(source=ROOT / "examples/calculation.py", specification=ROOT / "examples/calculation.md",
                         output=tmp_path / "run", config=WorkflowConfig(mode="offline", quality_review_enabled=enabled),
                         client=fixture_client("calculation"), review_client=reviewer, with_summary=summary)
    assert state.task_status == "completed" and state.quality_status == ("passed" if enabled else "disabled")
    events = [json.loads(line) for line in (state.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    nodes = [event["node"] for event in events if event["type"] == "node_finished"]
    assert nodes == ["test_generation", "test_execution"] + (["test_quality_review"] if enabled else []) + (["result_summary"] if summary else [])
    assert len(state.quality_assessments) == int(enabled)
    if enabled:
        assert len(reviewer.contexts) == 1


@pytest.mark.parametrize("enabled,client", [(True, None), (True, object()), (False, "unexpected-client")])
def test_missing_or_invalid_review_wiring_does_not_create_task(tmp_path, enabled, client):
    with pytest.raises((ValueError, TypeError)):
        run_workflow(source=ROOT / "examples/calculation.py", specification=ROOT / "examples/calculation.md",
                     output=tmp_path / "run", config=WorkflowConfig(mode="offline", quality_review_enabled=enabled),
                     client=fixture_client("calculation"), review_client=client)
    assert not (tmp_path / "run").exists()


def test_service_quality_feedback_runs_second_generation(tmp_path):
    first, second, revision = two_round_fixtures(tmp_path)
    generation = FixtureClient((first, second))
    reviewer = fixture_review_client((revision, DATA_ROOT / "quality_approve.json"))
    state = run_workflow(source=ROOT / "examples/calculation.py", specification=ROOT / "examples/calculation.md",
                         output=tmp_path / "run", config=WorkflowConfig(mode="offline", quality_review_enabled=True, max_rounds=2),
                         client=generation, review_client=reviewer, with_summary=True)
    assert state.task_status == "completed" and state.selected_round == 2
    assert len(generation.contexts) == len(reviewer.contexts) == 2
    assert '"task_kind": "quality_extension"' in generation.contexts[1].user_prompt


@pytest.mark.parametrize("summary", [False, True])
def test_offline_cli_quality_and_summary_flags(tmp_path, summary):
    output = tmp_path / "run"
    args = entry_args(output) + ["--demo", "--with-quality-review", "--review-fixture", str(DATA_ROOT / "quality_approve.json")]
    if summary:
        args.append("--with-summary")
    with patch("test_workflow.cli.load_config", side_effect=AssertionError("offline must not load model settings")), \
         patch("test_workflow.cli.DeepSeekReviewClient", side_effect=AssertionError("offline must not construct real reviewer")):
        assert main(args) == 0
    state = StateStore(output).load()
    assert state.quality_status == "passed" and state.config.quality_review_enabled
    assert (output / "node_results/01/result_summary").exists() == summary


def test_offline_cli_accepts_review_responses_in_order(tmp_path):
    first, second, revision = two_round_fixtures(tmp_path)
    output = tmp_path / "run"
    assert main(entry_args(output) + ["--demo", "--with-quality-review", "--with-summary", "--max-rounds", "2",
        "--fixture", str(first), "--fixture", str(second), "--review-fixture", str(revision),
        "--review-fixture", str(DATA_ROOT / "quality_approve.json")]) == 0
    state = StateStore(output).load()
    assert state.quality_status == "passed" and state.selected_round == 2
    feedback = next(message for message in state.messages if message.type == "quality_feedback")
    assert feedback.message_id in state.consumed_message_ids


@pytest.mark.parametrize("flags", [
    ["--demo", "--with-quality-review"],
    ["--demo", "--review-fixture", "not-read.json"],
    ["--with-quality-review", "--review-fixture", "not-read.json"],
])
def test_invalid_quality_cli_options_fail_before_configuration_or_creation(tmp_path, flags, capsys):
    with patch("test_workflow.cli.load_config", side_effect=AssertionError("invalid flags must not load config")):
        assert main(entry_args(tmp_path / "run") + flags) == 2
    assert "--review-fixture" in capsys.readouterr().out
    assert not (tmp_path / "run").exists()


def test_invalid_review_fixture_fails_before_run(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"verdict":"approve","verdict":"request_revision"}', encoding="utf-8")
    assert main(entry_args(tmp_path / "run") + ["--demo", "--with-quality-review", "--review-fixture", str(invalid)]) == 2
    assert not (tmp_path / "run").exists()


def test_real_cli_creates_independent_reviewer_from_same_settings(tmp_path):
    settings = AppConfig(model="fixture-model", api_key="secret-test-key")
    with patch("test_workflow.cli.load_config", return_value=settings), \
         patch("test_workflow.cli.DeepSeekClient", return_value=fixture_client("calculation")) as generation, \
         patch("test_workflow.cli.DeepSeekReviewClient", return_value=approve_client()) as review:
        assert main(entry_args(tmp_path / "run") + ["--with-quality-review", "--with-summary"]) == 0
    generation.assert_called_once_with(settings)
    review.assert_called_once_with(settings)
    state = StateStore(tmp_path / "run").load()
    assert state.config.mode == "real" and state.quality_status == "passed"
    assert "secret-test-key" not in state.canonical_json()


@pytest.mark.parametrize("enabled", [False, True])
def test_web_checkbox_flag_controls_actual_review_registration(server, enabled):
    _, base = server
    settings = AppConfig(model="fixture-model", api_key="secret-web-key")
    with patch("test_workflow.web.load_config", return_value=settings), \
         patch("test_workflow.web.DeepSeekClient", return_value=fixture_client("calculation")), \
         patch("test_workflow.web.DeepSeekReviewClient", return_value=approve_client()) as review:
        _, bootstrap = call(base, "/api/bootstrap")
        assert bootstrap["config"]["quality_review_enabled"] is False
        _, created = call(base, "/api/runs", {**sample_inputs()["calculation"], "quality_review_enabled": enabled})
        assert created["quality_review_enabled"] is enabled
        job = wait_job(base, created["id"])
    assert job["status"] == "finished" and job["state"]["config"]["quality_review_enabled"] is enabled
    assert job["quality_review_enabled"] is enabled
    assert job["state"]["quality_status"] == ("passed" if enabled else "disabled")
    assert review.call_count == int(enabled)
    if enabled:
        review.assert_called_once_with(settings)
    assert sum(event["node"] == "test_quality_review" and event["type"] == "node_finished" for event in job["events"]) == int(enabled)
    assert job["summary"]["agents"]["result_summary"]["committed_results"] == 1
    assert "secret-web-key" not in json.dumps([bootstrap, job])


def test_web_quality_feedback_uses_same_service_loop(server, tmp_path):
    _, base = server
    first, second, revision = two_round_fixtures(tmp_path)
    generation = FixtureClient((first, second))
    reviewer = fixture_review_client((revision, DATA_ROOT / "quality_approve.json"))
    settings = AppConfig(model="fixture-model", api_key="fixture-key")
    with patch("test_workflow.web.load_config", return_value=settings), \
         patch("test_workflow.web.DeepSeekClient", return_value=generation), \
         patch("test_workflow.web.DeepSeekReviewClient", return_value=reviewer):
        _, created = call(base, "/api/runs", {**sample_inputs()["calculation"], "max_rounds": 2, "quality_review_enabled": True})
        job = wait_job(base, created["id"])
    assert job["status"] == "finished" and job["summary"]["selected_round"] == 2
    assert len(generation.contexts) == len(reviewer.contexts) == 2


@pytest.mark.parametrize("revision", [False, True])
def test_cli_web_same_injected_responses_have_identical_routes_and_approved_export(server, tmp_path, revision):
    _, base = server
    first, second, feedback = two_round_fixtures(tmp_path)
    fixtures = (first, second) if revision else (first,)
    reviews = (feedback, DATA_ROOT / "quality_approve.json") if revision else (DATA_ROOT / "quality_approve.json",)
    settings = AppConfig(model="fixture-model", api_key="fixture-parity-key")
    output = tmp_path / "cli"
    with patch("test_workflow.cli.load_config", return_value=settings), \
         patch("test_workflow.cli.DeepSeekClient", return_value=FixtureClient(fixtures)), \
         patch("test_workflow.cli.DeepSeekReviewClient", return_value=fixture_review_client(reviews)):
        assert main(entry_args(output) + ["--with-quality-review", "--with-summary", "--max-rounds", "2"]) == 0
    with patch("test_workflow.web.load_config", return_value=settings), \
         patch("test_workflow.web.DeepSeekClient", return_value=FixtureClient(fixtures)), \
         patch("test_workflow.web.DeepSeekReviewClient", return_value=fixture_review_client(reviews)):
        _, created = call(base, "/api/runs", {**sample_inputs()["calculation"], "max_rounds": 2, "quality_review_enabled": True})
        job = wait_job(base, created["id"])
    assert job["status"] == "finished"
    cli_state = StateStore(output).load()
    cli_summary, web_summary = inspect_summary(cli_state), inspect_summary(StateStore(Path(job["outputDir"])).load())
    for key in ("task_status", "stop_reason", "quality_status", "quality_stop_reason", "selected_round", "rounds",
                "pytest_processes", "model_usage", "selected_quality_approved", "latest_quality_approved",
                "exported_quality_approved", "final_test_sha256"):
        assert cli_summary[key] == web_summary[key], key
    cli_events = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    for kind, fields in (("node_started", ("round", "node")), ("route_selected", ("round", "node", "action", "stop_reason"))):
        def trace(events):
            return [tuple(event.get(field) for field in fields) for event in events if event["type"] == kind]
        assert trace(cli_events) == trace(job["events"])
    assert cli_summary["selected_round"] == 1 + int(revision)
    assert cli_summary["exported_quality_approved"] and cli_summary["pytest_processes"] == 1 + int(revision)


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, [], {}])
def test_web_rejects_non_boolean_quality_flag_before_job(server, value):
    app, base = server
    payload = {**sample_inputs()["calculation"], "quality_review_enabled": value}
    with pytest.raises(ValueError):
        validate_payload(payload)
    with pytest.raises(HTTPError) as error:
        call(base, "/api/runs", payload)
    assert error.value.code == 400 and not app.store._jobs


def test_web_review_configuration_failure_never_falls_back(server):
    app, base = server
    settings = AppConfig(model="fixture-model", api_key="fixture-key")
    generation = fixture_client("calculation")
    with patch("test_workflow.web.load_config", return_value=settings), \
         patch("test_workflow.web.DeepSeekClient", return_value=generation), \
         patch("test_workflow.web.DeepSeekReviewClient", side_effect=ValueError("secret-error")):
        _, created = call(base, "/api/runs", {**sample_inputs()["calculation"], "quality_review_enabled": True})
        job = wait_job(base, created["id"])
    assert job["status"] == "failed" and not generation.contexts
    assert "secret-error" not in json.dumps(job) and not app.store.busy.locked()
    assert not (Path(job["outputDir"]) / "state.json").exists()
