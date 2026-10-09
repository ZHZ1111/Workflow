"""Protect config precedence, budgets and secret handling before real API use."""

import json
import os
import subprocess
import sys

import httpx
import pytest

import testgen_agent.cli as cli
from testgen_agent.cli import main
from testgen_agent.config import ConfigError, load_config
from testgen_agent.llm_client import create_http_client
from testgen_agent.models import StopReason, TaskStatus


@pytest.fixture
def configured_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return {"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "test-only-secret"}


def test_environment_and_cli_override_file_without_mutating_environment(configured_environment, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_MODEL=file-model\nDEEPSEEK_API_KEY=file-secret\nAGENT_MAX_ROUNDS=1\n",
        encoding="utf-8",
    )
    original = configured_environment.copy()
    config = load_config(environ=configured_environment, overrides={"max_rounds": 2})
    assert config.model == "test-model"
    assert config.max_rounds == 2
    assert config.api_key.get_secret_value() == "test-only-secret"
    assert configured_environment == original


def test_custom_key_name_and_no_dotenv_interpolation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / "settings.env"
    env_file.write_text(
        "LLM_MODEL=test-model\nLLM_API_KEY_ENV=CUSTOM_KEY\nCUSTOM_KEY=literal-${VALUE}\n",
        encoding="utf-8-sig",
    )
    config = load_config(env_file=env_file, environ={"VALUE": "expanded"})
    assert config.api_key.get_secret_value() == "literal-${VALUE}"


@pytest.mark.parametrize("field,value", [
    ("AGENT_MAX_ROUNDS", "0"), ("AGENT_MAX_ROUNDS", "4"),
    ("AGENT_MAX_ROUNDS", "1.0"), ("AGENT_MAX_ROUNDS", ""),
    ("TEST_EXECUTION_TIMEOUT_SECONDS", "0"), ("TEST_EXECUTION_TIMEOUT_SECONDS", "301"),
    ("LLM_REQUEST_TIMEOUT_SECONDS", "0"), ("LLM_REQUEST_TIMEOUT_SECONDS", "601"),
    ("LLM_MAX_TOKENS", "1023"), ("LLM_MAX_TOKENS", "32769"),
    ("LLM_PROVIDER", "other"), ("LLM_MODEL", "your-deepseek-model"),
    ("LLM_BASE_URL", "https://user:secret@example.com"),
    ("LLM_BASE_URL", "https://api.deepseek.com?key=test-only-secret"),
    ("LLM_BASE_URL", "https://api.deepseek.com:invalid"),
    ("LLM_API_KEY_ENV", "INVALID-NAME"),
    ("DEEPSEEK_API_KEY", "your-deepseek-api-key"), ("DEEPSEEK_API_KEY", ""),
])
def test_invalid_settings_are_rejected_without_exposing_input(configured_environment, field, value):
    configured_environment[field] = value
    with pytest.raises(ConfigError) as caught:
        load_config(environ=configured_environment)
    assert "test-only-secret" not in str(caught.value)
    assert "user:secret" not in str(caught.value)


def test_missing_key_is_rejected(configured_environment):
    del configured_environment["DEEPSEEK_API_KEY"]
    with pytest.raises(ConfigError, match="API Key"):
        load_config(environ=configured_environment)


def test_missing_explicit_file_is_not_ignored(configured_environment, tmp_path):
    with pytest.raises(ConfigError, match="不存在"):
        load_config(env_file=tmp_path / "missing.env", environ=configured_environment)


def test_summary_serialization_and_repr_hide_key(configured_environment):
    config = load_config(environ=configured_environment)
    for rendered in [repr(config), config.model_dump_json(), json.dumps(config.safe_summary())]:
        assert "test-only-secret" not in rendered
    assert "api_key" not in config.model_dump()
    assert config.safe_summary()["api_key_configured"] is True


def test_explicit_proxy_is_validated_and_hidden_from_public_summary(configured_environment):
    configured_environment["LLM_PROXY_URL"] = "http://127.0.0.1:7890/"
    config = load_config(environ=configured_environment)

    assert config.proxy_url == "http://127.0.0.1:7890"
    assert config.safe_summary()["proxy_configured"] is True
    assert "127.0.0.1:7890" not in json.dumps(config.safe_summary())
    assert "127.0.0.1:7890" not in config.model_dump_json()


@pytest.mark.parametrize("proxy", [
    "socks5://127.0.0.1:7890",
    "http://user:password@127.0.0.1:7890",
    "http://127.0.0.1:invalid",
    "http://127.0.0.1:7890?secret=value",
])
def test_invalid_proxy_is_rejected_without_exposing_value(configured_environment, proxy):
    configured_environment["LLM_PROXY_URL"] = proxy
    with pytest.raises(ConfigError) as caught:
        load_config(environ=configured_environment)
    assert proxy not in str(caught.value)


def test_explicit_proxy_is_passed_to_http_client(configured_environment, monkeypatch):
    config = load_config(environ={
        **configured_environment,
        "LLM_PROXY_URL": "http://127.0.0.1:7890",
    })
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("testgen_agent.llm_client.httpx.Client", FakeClient)
    create_http_client(config)

    assert captured["proxy"] == "http://127.0.0.1:7890"


def test_cli_success_and_config_failure_have_distinct_exit_codes(configured_environment, monkeypatch, capsys):
    for key, value in configured_environment.items():
        monkeypatch.setenv(key, value)
    assert main(["--check-config", "--max-rounds", "1"]) == 0
    output = capsys.readouterr()
    assert "test-only-secret" not in output.out + output.err
    assert '"max_rounds": 1' in output.out
    assert main(["--check-config", "--max-rounds", "4"]) == 2
    assert "1..3" in capsys.readouterr().err


def test_cli_runs_agent_with_source_spec_and_fresh_output(configured_environment, tmp_path, monkeypatch, capsys):
    for key, value in configured_environment.items():
        monkeypatch.setenv(key, value)
    source = tmp_path / "subject.py"
    spec = tmp_path / "behavior.md"
    output = tmp_path / "run"
    source.write_text("def double(value):\n    return value * 2\n", encoding="utf-8")
    spec.write_text("double(3) returns 6.\n", encoding="utf-8")
    captured = {}

    class FakeResult:
        decision = type("Decision", (), {"status": TaskStatus.COMPLETED})()
        state = type("State", (), {"stop_reason": StopReason.COVERAGE_TARGET_MET})()
        final_test_file = output / "test_subject.py"
        summary_file = output / "summary.json"
        report_file = output / "report.md"

    monkeypatch.setattr(cli, "DeepSeekClient", lambda config: object())

    def fake_run(request, **kwargs):
        captured["request"] = request
        captured.update(kwargs)
        output.mkdir()
        (output / "summary.json").write_text(
            json.dumps({"conclusion": {"level": "passed", "title": "通过", "detail": "测试通过"}}),
            encoding="utf-8",
        )
        return FakeResult()

    monkeypatch.setattr(cli, "run_agent", fake_run)
    assert main(["--source", str(source), "--spec", str(spec), "--output", str(output)]) == 0
    assert captured["request"].source_path == source
    assert captured["request"].output_dir == output
    assert captured["behavior_specification"] == "double(3) returns 6.\n"
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["status"] == "completed"
    assert cli_output["conclusion"]["title"] == "通过"
    assert cli_output["report_file"].endswith("report.md")


def test_cli_requires_source_and_spec_together():
    with pytest.raises(SystemExit) as caught:
        main(["--source", "subject.py"])
    assert caught.value.code == 2


def test_mock_transport_keeps_base_path_and_uses_configured_timeout(configured_environment):
    config = load_config(environ=configured_environment, overrides={
        "base_url": "https://api.deepseek.com/v1/", "request_timeout_seconds": 17,
    })
    requests = []

    def handle(request):
        requests.append(request)
        assert str(request.url) == "https://api.deepseek.com/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-only-secret"
        assert set(request.extensions["timeout"].values()) == {17}
        return httpx.Response(200, json={"mock": True})

    with create_http_client(config, transport=httpx.MockTransport(handle)) as client:
        assert requests == []  # Creating a client alone must not call the API.
        result = client.post("chat/completions", json={"model": config.model, "messages": []})
    assert result.json() == {"mock": True}
    assert len(requests) == 1


def test_module_entrypoint_redirects_chinese_output_as_utf8(tmp_path):
    env_file = tmp_path / "check.env"
    env_file.write_text(
        "LLM_MODEL=test-model\nDEEPSEEK_API_KEY=entrypoint-test-secret\n",
        encoding="utf-8",
    )
    environment = {key: value for key, value in os.environ.items() if not (
        key.startswith("LLM_") or key in {
            "DEEPSEEK_API_KEY", "AGENT_MAX_ROUNDS", "TEST_EXECUTION_TIMEOUT_SECONDS",
        }
    )}
    result = subprocess.run(
        [sys.executable, "-m", "testgen_agent", "--check-config", "--env-file", str(env_file)],
        cwd=tmp_path, env=environment, capture_output=True, timeout=10,
    )
    assert result.returncode == 0
    output = result.stdout.decode("utf-8")
    assert "配置检查通过" in output
    assert "entrypoint-test-secret" not in output
    assert result.stderr == b""
