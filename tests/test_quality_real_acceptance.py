"""S9 request boundaries and preparation; MockTransport only, no paid requests."""

import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from test_workflow.agents.test_generation.core.config import AppConfig

ROOT = Path(__file__).resolve().parents[1]
definition = importlib.util.spec_from_file_location("s9_acceptance", ROOT / "scripts/verify_quality_real.py")
s9 = importlib.util.module_from_spec(definition)
definition.loader.exec_module(s9)


def request(url="https://api.deepseek.com/chat/completions", **payload):
    return httpx.Request("POST", url, json={"max_tokens": 8192, "messages": [], **payload})


def test_total_and_per_case_limits_block_before_transport_dispatch(tmp_path):
    budget = s9.DispatchBudget(tmp_path, maximum=3, per_case=2)
    for _ in range(2):
        budget.reserve("first", "generation", request())
    with pytest.raises(httpx.RequestError):
        budget.reserve("first", "review", request())
    budget.reserve("second", "review", request())
    with pytest.raises(httpx.RequestError):
        budget.reserve("third", "generation", request())
    assert len(budget.requests) == 3 and budget.blocked == 2


@pytest.mark.parametrize("url,payload", [
    ("https://example.com/chat/completions", {}), ("http://api.deepseek.com/chat/completions", {}),
    ("https://api.deepseek.com/unrelated", {}), ("https://api.deepseek.com/chat/completions", {"max_tokens":8193}),
    ("https://api.deepseek.com/chat/completions", {"messages":[{"content":"x" * 131072}]}),
])
def test_endpoint_and_payload_budgets_reject_before_network(tmp_path, monkeypatch, url, payload):
    def forbidden(**kwargs):
        raise AssertionError("network must not be constructed")
    monkeypatch.setattr(httpx, "HTTPTransport", forbidden)
    budget = s9.DispatchBudget(tmp_path)
    with pytest.raises(httpx.RequestError):
        s9.AuditedTransport(budget, "sample", "review").handle_request(request(url, **payload))
    assert not budget.requests and budget.blocked == 1


def test_transport_survives_sdk_context_closures_and_never_records_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: httpx.MockTransport(lambda req: httpx.Response(200, json={"ok":True})))
    budget = s9.DispatchBudget(tmp_path)
    transport = s9.AuditedTransport(budget, "sample", "generation")
    for _ in range(2):
        with httpx.Client(transport=transport) as client:
            assert client.post("https://api.deepseek.com/chat/completions", headers={"Authorization":"Bearer test-secret"}, json={"max_tokens":8192}).json() == {"ok":True}
    raw = (tmp_path / "dispatches.json").read_text(encoding="utf-8")
    assert "test-secret" not in raw and "Authorization" not in raw
    assert len(json.loads(raw)["requests"]) == 2


def test_preparation_requires_explicit_execution_and_saves_exact_source_scope(tmp_path, monkeypatch):
    for sample, _ in s9.SAMPLES:
        directory = tmp_path / "examples"
        directory.mkdir(exist_ok=True)
        for suffix in ("py", "md"):
            (directory / f"{sample}.{suffix}").write_bytes((ROOT / f"examples/{sample}.{suffix}").read_bytes())
    monkeypatch.setattr(s9, "ROOT", tmp_path)
    monkeypatch.setattr(s9, "load_config", lambda **kwargs: AppConfig(model="fixture-model", api_key="test-secret", max_tokens=8192))
    monkeypatch.setattr(s9, "run_workflow", lambda **kwargs: pytest.fail("preparation cannot start model workflow"))
    output = tmp_path / "outputs/prepared"
    monkeypatch.setattr(s9.sys, "argv", ["verify_quality_real.py", "--output", str(output)])
    assert s9.main() == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["samples"]) == 3 and manifest["http_dispatch_limit"] == 18
    assert all(len(item["source_sha256"]) == 64 for item in manifest["samples"])
    assert not (output / "dispatches.json").exists()
    assert "test-secret" not in (output / "manifest.json").read_text(encoding="utf-8")
