"""B2 DeepSeek adapter tests use MockTransport and never make network calls."""

import json

import httpx
import pytest

from testgen_agent.config import load_config
from testgen_agent.llm_client import (
    DeepSeekClient, GenerationContext, LLMError, LLMErrorCode, RetryPolicy,
    generate_tests,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return load_config(environ={"LLM_MODEL": "test-model", "DEEPSEEK_API_KEY": "adapter-secret"})


@pytest.fixture
def context():
    return GenerationContext(
        system_instruction="你是测试生成器。",
        user_prompt="为 calculate_total 生成一个正常场景。",
    )


def valid_generated() -> dict:
    return {
        "plan": {
            "target_functions": ["calculate_total"],
            "oracle_mode": "specification",
            "scenarios": [{
                "scenario_id": "normal-1",
                "target_function": "calculate_total",
                "category": "normal",
                "inputs": {"args": [12, 3], "kwargs": {}},
                "expected_behavior": "返回 36",
                "expected_basis": {"kind": "specification", "detail": "单价乘以数量"},
            }],
        },
        "code": "from subject import calculate_total\n\ndef test_total():\n    assert calculate_total(12, 3) == 36\n",
        "mappings": [{"test_function": "test_total", "scenario_ids": ["normal-1"]}],
        "assumptions": [],
        "questions": [],
    }


def generated_with_incomplete_collaboration_metadata() -> dict:
    value = valid_generated()
    value["plan"] = {
        "schema_version": "1.1",
        "target_functions": ["calculate_total", "checkout"],
        "oracle_mode": "specification",
        "entry_functions": ["checkout"],
        "relationships": [{
            "relation_id": "rel-checkout-total",
            "kind": "direct_call",
            "involved_functions": ["checkout", "calculate_total"],
            "evidence": [{
                "kind": "source", "detail": "checkout calls calculate_total", "line": 2,
                "call_site_ids": ["call-checkout-total"],
            }],
            "confidence": "confirmed",
            "unresolved_questions": [],
        }],
        "strategy": [
            {"subject_kind": "function", "function_name": "calculate_total", "relationship_id": None,
             "decision": "unit_test", "reason": "independent arithmetic"},
            {"subject_kind": "function", "function_name": "checkout", "relationship_id": None,
             "decision": "unit_test", "reason": "public entry"},
            {"subject_kind": "relationship", "function_name": None, "relationship_id": "rel-checkout-total",
             "decision": "collaboration_test", "reason": "entry uses the calculation"},
        ],
        "scenarios": [{
            "scenario_id": "collaboration-checkout",
            "target_function": "checkout",
            "category": "normal",
            "inputs": {"args": [12, 3], "kwargs": {}},
            "expected_behavior": "returns 36",
            "expected_basis": {"kind": "specification", "detail": "checkout returns the total"},
            "test_kind": "collaboration",
            # The model redundantly omitted calculate_total here even though
            # the referenced relationship already names it.
            "involved_functions": ["checkout"],
            "relationship_ids": ["rel-checkout-total"],
            "steps": [],
        }],
    }
    value["code"] = "from subject import checkout\n\ndef test_checkout():\n    assert checkout(12, 3) == 36\n"
    value["mappings"] = [{"test_function": "test_checkout", "scenario_ids": ["collaboration-checkout"]}]
    return value


def generated_with_extra_unit_metadata() -> dict:
    value = generated_with_incomplete_collaboration_metadata()
    scenario = value["plan"]["scenarios"][0]
    scenario["scenario_id"] = "unit-checkout"
    scenario["test_kind"] = "unit"
    scenario["involved_functions"] = ["checkout", "calculate_total"]
    scenario["relationship_ids"] = []
    value["mappings"][0]["scenario_ids"] = ["unit-checkout"]
    return value


def completion(content, *, usage=None, finish_reason="stop", model="test-model", response_id="response-1"):
    body = {
        "id": response_id,
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return httpx.Response(200, json=body)


def test_valid_generation_uses_json_mode_schema_and_records_usage(config, context):
    requests = []

    def handle(request):
        requests.append(request)
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer adapter-secret"
        assert body["model"] == "test-model"
        assert body["stream"] is False
        assert body["max_tokens"] == 8192
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert "JSON Schema" in body["messages"][0]["content"]
        assert "GeneratedTests" in body["messages"][0]["content"]
        return completion(
            json.dumps(valid_generated(), ensure_ascii=False),
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
        )

    result = generate_tests(config, context, transport=httpx.MockTransport(handle), sleep=lambda _: None)
    assert result.generated.plan.scenarios[0].expected_behavior == "返回 36"
    assert result.attempts == len(requests) == 1
    assert result.format_repairs == 0
    assert result.total_usage.total_tokens == 150
    assert result.calls[0].response_id == "response-1"
    rendered = result.model_dump_json()
    assert "adapter-secret" not in rendered
    assert "Authorization" not in rendered


def test_one_format_repair_is_allowed_and_usage_is_aggregated(config, context):
    requests = []
    responses = [
        completion(
            '{"unexpected":true}', response_id="bad",
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        ),
        completion(
            json.dumps(valid_generated()), response_id="fixed",
            usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        ),
    ]

    def handle(request):
        requests.append(json.loads(request.content))
        return responses.pop(0)

    result = DeepSeekClient(
        config, transport=httpx.MockTransport(handle), sleep=lambda _: None,
    ).generate_tests(context)
    assert result.attempts == 2 and result.format_repairs == 1
    assert [call.purpose for call in result.calls] == ["generation", "format_repair"]
    assert result.total_usage.model_dump() == {
        "prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42,
    }
    assert len(requests[1]["messages"]) == 4
    assert requests[1]["messages"][-2]["content"] == '{"unexpected":true}'
    repair = requests[1]["messages"][-1]["content"]
    assert "VALIDATION_ERRORS" in repair
    assert '"path":"plan"' in repair
    assert '"true"' not in repair


def test_deterministic_collaboration_metadata_omission_is_normalized_without_retry(config, context):
    requests = []

    def handle(request):
        requests.append(request)
        return completion(json.dumps(generated_with_incomplete_collaboration_metadata()))

    result = DeepSeekClient(
        config, transport=httpx.MockTransport(handle), sleep=lambda _: None,
    ).generate_tests(context)

    scenario = result.generated.plan.scenarios[0]
    assert scenario.involved_functions == ("checkout", "calculate_total")
    assert result.attempts == len(requests) == 1
    assert result.format_repairs == 0
    assert result.host_normalizations == (
        "completed involved_functions for collaboration scenario collaboration-checkout",
    )


def test_redundant_unit_involved_functions_are_trimmed_without_retry(config, context):
    requests = []

    def handle(request):
        requests.append(request)
        return completion(json.dumps(generated_with_extra_unit_metadata()))

    result = DeepSeekClient(
        config, transport=httpx.MockTransport(handle), sleep=lambda _: None,
    ).generate_tests(context)

    scenario = result.generated.plan.scenarios[0]
    assert scenario.involved_functions == ("checkout",)
    assert result.attempts == len(requests) == 1
    assert result.format_repairs == 0
    assert result.host_normalizations == (
        "trimmed involved_functions for unit scenario unit-checkout",
    )


def test_repeated_invalid_output_fails_without_unbounded_repair(config, context):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return completion('{"still":"invalid"}')

    with pytest.raises(LLMError) as caught:
        generate_tests(config, context, transport=httpx.MockTransport(handle), sleep=lambda _: None)
    assert caught.value.code == LLMErrorCode.INVALID_GENERATED_OUTPUT
    assert caught.value.attempts == count == 3
    assert not caught.value.retryable
    assert "校验摘要" in str(caught.value)
    assert '"invalid"' not in str(caught.value)


def test_transient_failures_retry_with_bounded_backoff(config, context):
    delays = []
    statuses = [429, 503, 200]

    def handle(request):
        status = statuses.pop(0)
        if status == 429:
            return httpx.Response(status, headers={"Retry-After": "9"}, json={"error": "adapter-secret"})
        if status == 503:
            return httpx.Response(status, json={"error": "adapter-secret"})
        return completion(json.dumps(valid_generated()))

    client = DeepSeekClient(
        config,
        retry_policy=RetryPolicy(base_delay_seconds=0.25, max_delay_seconds=2),
        transport=httpx.MockTransport(handle), sleep=delays.append,
    )
    result = client.generate_tests(context)
    assert result.attempts == 3
    assert delays == [2, 0.5]


def test_transient_budget_is_finite(config, context):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(429, json={"error": "adapter-secret"})

    with pytest.raises(LLMError) as caught:
        DeepSeekClient(
            config, retry_policy=RetryPolicy(max_transient_retries=2),
            transport=httpx.MockTransport(handle), sleep=lambda _: None,
        ).generate_tests(context)
    assert caught.value.code == LLMErrorCode.RATE_LIMIT
    assert caught.value.retryable and caught.value.attempts == count == 3
    assert "adapter-secret" not in json.dumps(caught.value.safe_details(), ensure_ascii=False)


@pytest.mark.parametrize("status,code", [
    (400, LLMErrorCode.INVALID_REQUEST),
    (401, LLMErrorCode.AUTHENTICATION),
    (402, LLMErrorCode.INSUFFICIENT_BALANCE),
    (403, LLMErrorCode.PERMISSION),
    (404, LLMErrorCode.NOT_FOUND),
])
def test_permanent_http_errors_fail_fast_without_response_body(config, context, status, code):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(status, json={"secret": "adapter-secret"})

    with pytest.raises(LLMError) as caught:
        generate_tests(config, context, transport=httpx.MockTransport(handle), sleep=lambda _: None)
    assert caught.value.code == code and caught.value.attempts == count == 1
    assert not caught.value.retryable
    assert "adapter-secret" not in str(caught.value)


@pytest.mark.parametrize("failure,code", [
    (httpx.ConnectError("adapter-secret"), LLMErrorCode.NETWORK),
    (httpx.ReadTimeout("adapter-secret"), LLMErrorCode.TIMEOUT),
])
def test_network_and_timeout_errors_retry_then_return_safe_error(config, context, failure, code):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        failure.request = request
        raise failure

    with pytest.raises(LLMError) as caught:
        DeepSeekClient(
            config, retry_policy=RetryPolicy(max_transient_retries=1),
            transport=httpx.MockTransport(handle), sleep=lambda _: None,
        ).generate_tests(context)
    assert caught.value.code == code and caught.value.attempts == count == 2
    assert "adapter-secret" not in str(caught.value)


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not-json"),
    httpx.Response(200, json={"choices": []}),
    completion("", usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}),
])
def test_malformed_or_truncated_api_response_is_not_treated_as_format_repair(config, context, response):
    with pytest.raises(LLMError) as caught:
        generate_tests(
            config, context, transport=httpx.MockTransport(lambda request: response), sleep=lambda _: None,
        )
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert caught.value.attempts == 1


def test_length_limited_response_gets_one_concise_repair(config, context):
    requests = []
    responses = [
        completion('{"plan":', finish_reason="length", response_id="truncated",
                   usage={"prompt_tokens": 100, "completion_tokens": 8192, "total_tokens": 8292}),
        completion(json.dumps(valid_generated()), response_id="compact",
                   usage={"prompt_tokens": 120, "completion_tokens": 50, "total_tokens": 170}),
    ]

    def handle(request):
        requests.append(json.loads(request.content))
        return responses.pop(0)

    result = DeepSeekClient(
        config, transport=httpx.MockTransport(handle), sleep=lambda _: None,
    ).generate_tests(context)
    assert result.attempts == 2 and result.format_repairs == 1
    assert [call.finish_reason for call in result.calls] == ["length", "stop"]
    assert result.total_usage.total_tokens == 8462
    assert "shorter complete JSON" in requests[1]["messages"][-1]["content"]
    assert requests[1]["max_tokens"] == 8192


def test_second_length_limited_response_fails_with_bounded_error(config, context):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return completion('{"plan":', finish_reason="length")

    with pytest.raises(LLMError) as caught:
        DeepSeekClient(
            config, transport=httpx.MockTransport(handle), sleep=lambda _: None,
        ).generate_tests(context)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert caught.value.attempts == count == 3
    assert "长度上限" in str(caught.value)


def test_missing_or_inconsistent_usage_is_unknown_not_invented(config, context):
    for usage in (None, {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 99}):
        result = generate_tests(
            config, context,
            transport=httpx.MockTransport(lambda request: completion(json.dumps(valid_generated()), usage=usage)),
            sleep=lambda _: None,
        )
        assert result.calls[0].usage is None
        assert result.total_usage is None


def test_adapter_configuration_and_policy_validation_hide_key(config):
    client = DeepSeekClient(config)
    assert client.safe_config["max_http_attempts"] == 5
    assert client.safe_config["max_tokens"] == 8192
    assert "adapter-secret" not in json.dumps(client.safe_config)
    with pytest.raises(ValueError):
        RetryPolicy(max_transient_retries=5)
    with pytest.raises(ValueError):
        RetryPolicy(max_format_repairs=3)
    with pytest.raises(ValueError):
        DeepSeekClient(config, max_tokens=0)
