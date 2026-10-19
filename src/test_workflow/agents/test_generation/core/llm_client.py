"""DeepSeek adapter with bounded retries and validated structured output."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
import json
import time
from time import perf_counter
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field, StrictInt, ValidationError

from .config import AppConfig
from .models import ContractModel, GeneratedTests, Text, TokenUsage


MAX_REPAIR_CONTENT_CHARS = 64_000


class LLMErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limit"
    SERVICE_UNAVAILABLE = "service_unavailable"
    TIMEOUT = "timeout"
    NETWORK = "network"
    INVALID_RESPONSE = "invalid_response"
    INVALID_GENERATED_OUTPUT = "invalid_generated_output"


class LLMError(RuntimeError):
    """Safe, actionable failure without request headers or response bodies."""

    def __init__(
        self, code: LLMErrorCode, message: str, *, retryable: bool,
        attempts: int, status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.attempts = attempts
        self.status_code = status_code

    def safe_details(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": str(self),
            "retryable": self.retryable,
            "attempts": self.attempts,
            "status_code": self.status_code,
        }


class GenerationContext(ContractModel):
    """Prompt text supplied by B3/E; this adapter only adds the output contract."""

    system_instruction: Text
    user_prompt: Text


class LLMCallRecord(ContractModel):
    purpose: Literal["generation", "format_repair"]
    response_id: str | None = None
    response_model: str | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class LLMGenerationResult(ContractModel):
    generated: GeneratedTests
    provider: Literal["deepseek"]
    requested_model: Text
    response_model: Text
    attempts: Annotated[StrictInt, Field(ge=1)]
    format_repairs: Annotated[StrictInt, Field(ge=0, le=2)]
    latency_seconds: Annotated[float, Field(ge=0)]
    calls: Annotated[tuple[LLMCallRecord, ...], Field(min_length=1)]
    total_usage: TokenUsage | None = None
    host_normalizations: tuple[Text, ...] = ()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """One initial request + transient retries + format repairs, all globally bounded."""

    max_transient_retries: int = 2
    max_format_repairs: int = 2
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if type(self.max_transient_retries) is not int or not 0 <= self.max_transient_retries <= 4:
            raise ValueError("max_transient_retries must be between 0 and 4")
        if type(self.max_format_repairs) is not int or not 0 <= self.max_format_repairs <= 2:
            raise ValueError("max_format_repairs must be between 0 and 2")
        if (
            isinstance(self.base_delay_seconds, bool) or isinstance(self.max_delay_seconds, bool)
            or not 0 <= self.base_delay_seconds <= self.max_delay_seconds <= 10
        ):
            raise ValueError("retry delays must satisfy 0 <= base <= max <= 10")

    @property
    def max_http_attempts(self) -> int:
        return 1 + self.max_transient_retries + self.max_format_repairs


def create_http_client(
    config: AppConfig, *, transport: httpx.BaseTransport | None = None,
) -> httpx.Client:
    """Create a no-auto-retry client; DeepSeekClient owns the complete retry budget."""
    return httpx.Client(
        base_url=config.base_url + "/",
        headers={
            "Authorization": f"Bearer {config.api_key.get_secret_value()}",
            "Content-Type": "application/json",
            "User-Agent": "testgen-agent/0.1.0",
        },
        timeout=httpx.Timeout(config.request_timeout_seconds),
        follow_redirects=False,
        proxy=config.proxy_url,
        transport=transport,
    )


class DeepSeekClient:
    """Generate one validated candidate through the DeepSeek Chat Completions API."""

    def __init__(
        self,
        config: AppConfig,
        *,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_tokens: int | None = None,
    ) -> None:
        max_tokens = config.max_tokens if max_tokens is None else max_tokens
        if not 1 <= max_tokens <= 32_768:
            raise ValueError("max_tokens must be between 1 and 32768")
        self._config = config
        self._retry = retry_policy or RetryPolicy()
        self._transport = transport
        self._sleep = sleep
        self._max_tokens = max_tokens

    @property
    def safe_config(self) -> dict[str, Any]:
        return {
            "provider": self._config.provider,
            "model": self._config.model,
            "base_url": self._config.base_url,
            "request_timeout_seconds": self._config.request_timeout_seconds,
            "max_tokens": self._max_tokens,
            "max_transient_retries": self._retry.max_transient_retries,
            "max_format_repairs": self._retry.max_format_repairs,
            "max_http_attempts": self._retry.max_http_attempts,
        }

    def generate_tests(self, context: GenerationContext) -> LLMGenerationResult:
        schema = json.dumps(
            GeneratedTests.model_json_schema(mode="validation"),
            ensure_ascii=False, separators=(",", ":"),
        )
        schema_instruction = (
            "Return exactly one JSON object. Do not use Markdown fences or explanatory text. "
            "The object must validate against this JSON Schema:\n" + schema
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": context.system_instruction + "\n\n" + schema_instruction},
            {"role": "user", "content": context.user_prompt},
        ]
        attempts = 0
        transient_retries = 0
        repairs = 0
        calls: list[LLMCallRecord] = []
        start = perf_counter()

        with create_http_client(self._config, transport=self._transport) as client:
            while attempts < self._retry.max_http_attempts:
                purpose = "format_repair" if repairs else "generation"
                attempts += 1
                try:
                    response = client.post("chat/completions", json={
                        "model": self._config.model,
                        "messages": messages,
                        "stream": False,
                        "max_tokens": self._max_tokens,
                        "thinking": {"type": "disabled"},
                        "response_format": {"type": "json_object"},
                    })
                except httpx.TimeoutException:
                    if self._can_retry(transient_retries, attempts):
                        self._sleep(self._delay(transient_retries, None))
                        transient_retries += 1
                        continue
                    raise LLMError(
                        LLMErrorCode.TIMEOUT,
                        "DeepSeek 请求超时，请检查网络或增大 LLM_REQUEST_TIMEOUT_SECONDS。",
                        retryable=True, attempts=attempts,
                    ) from None
                except httpx.RequestError:
                    if self._can_retry(transient_retries, attempts):
                        self._sleep(self._delay(transient_retries, None))
                        transient_retries += 1
                        continue
                    raise LLMError(
                        LLMErrorCode.NETWORK,
                        "无法连接 DeepSeek，请检查网络、服务地址或代理配置。",
                        retryable=True, attempts=attempts,
                    ) from None

                if response.status_code != 200:
                    code, message, retryable = _http_failure(response.status_code)
                    if retryable and self._can_retry(transient_retries, attempts):
                        self._sleep(self._delay(transient_retries, response.headers.get("Retry-After")))
                        transient_retries += 1
                        continue
                    raise LLMError(
                        code, message, retryable=retryable, attempts=attempts,
                        status_code=response.status_code,
                    )

                content, record = _extract_completion(response, purpose, attempts)
                calls.append(record)
                if record.finish_reason == "length":
                    if repairs >= self._retry.max_format_repairs or attempts >= self._retry.max_http_attempts:
                        raise LLMError(
                            LLMErrorCode.INVALID_RESPONSE,
                            "DeepSeek 输出达到长度上限，精简重试预算已用尽。",
                            retryable=False, attempts=attempts, status_code=200,
                        )
                    repairs += 1
                    messages.append({"role": "user", "content": (
                        "The previous response reached the output limit. Return a shorter complete JSON object "
                        "matching the supplied schema. Keep only the smallest set of parametrized tests needed "
                        "to cover the specified boundaries, exceptions, major branches, and selected collaborations. "
                        "Use concise IDs, reasons, evidence details, and Python code. Do not repeat equivalent cases "
                        "and do not add Markdown or explanations."
                    )})
                    continue
                normalizations: tuple[str, ...] = ()
                try:
                    try:
                        payload = json.loads(content)
                    except json.JSONDecodeError:
                        generated = GeneratedTests.model_validate_json(content)
                    else:
                        normalizations = _normalize_generated_payload(payload)
                        generated = GeneratedTests.model_validate(payload)
                except ValidationError as error:
                    diagnostics = _validation_diagnostics(error)
                    if repairs >= self._retry.max_format_repairs or attempts >= self._retry.max_http_attempts:
                        raise LLMError(
                            LLMErrorCode.INVALID_GENERATED_OUTPUT,
                            "DeepSeek 输出未通过 GeneratedTests 结构校验，且格式修复预算已用尽。"
                            f"校验摘要：{diagnostics}",
                            retryable=False, attempts=attempts,
                        ) from None
                    repairs += 1
                    messages.extend([
                        {"role": "assistant", "content": content[:MAX_REPAIR_CONTENT_CHARS]},
                        {"role": "user", "content": (
                            "The previous JSON did not validate. Return a corrected complete JSON object "
                            "matching the supplied schema. Fix every validation error listed below. "
                            "Return the entire replacement, including exactly one function strategy for every "
                            "target_function and exactly one relationship strategy for every relationship. "
                            "Every mapping must contain at least one scenario_id, and mappings together must cover "
                            "every scenario exactly. Remove any empty helper mapping. "
                            "For each collaboration scenario, involved_functions must contain its target_function "
                            "and every function listed by every referenced relationship_id. "
                            "For each collaboration scenario, every relationship_id it references must have "
                            "a relationship strategy decision of collaboration_test. A relationship with a "
                            "not_selected strategy must not appear in any collaboration scenario. "
                            "Do not add Markdown or explanations.\nVALIDATION_ERRORS:\n" + diagnostics
                        )},
                    ])
                    continue

                response_model = record.response_model or self._config.model
                return LLMGenerationResult(
                    generated=generated,
                    provider=self._config.provider,
                    requested_model=self._config.model,
                    response_model=response_model,
                    attempts=attempts,
                    format_repairs=repairs,
                    latency_seconds=round(perf_counter() - start, 3),
                    calls=tuple(calls),
                    total_usage=_sum_usage(calls),
                    host_normalizations=normalizations,
                )

        raise LLMError(
            LLMErrorCode.SERVICE_UNAVAILABLE,
            "DeepSeek 请求预算已用尽。",
            retryable=True, attempts=attempts,
        )

    def _can_retry(self, transient_retries: int, attempts: int) -> bool:
        return (
            transient_retries < self._retry.max_transient_retries
            and attempts < self._retry.max_http_attempts
        )

    def _delay(self, retry_index: int, retry_after: str | None) -> float:
        server_delay = _retry_after_seconds(retry_after)
        if server_delay is not None:
            return min(server_delay, self._retry.max_delay_seconds)
        return min(
            self._retry.base_delay_seconds * (2 ** retry_index),
            self._retry.max_delay_seconds,
        )


def generate_tests(
    config: AppConfig,
    context: GenerationContext,
    *,
    retry_policy: RetryPolicy | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_tokens: int | None = None,
) -> LLMGenerationResult:
    """Convenience interface for callers that do not need to reuse an adapter."""
    return DeepSeekClient(
        config, retry_policy=retry_policy, transport=transport,
        sleep=sleep, max_tokens=max_tokens,
    ).generate_tests(context)


def _extract_completion(
    response: httpx.Response, purpose: str, attempts: int,
) -> tuple[str, LLMCallRecord]:
    try:
        data = response.json()
        choice = data["choices"][0]
        message = choice["message"]
        content = message["content"]
        finish_reason = choice["finish_reason"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("empty completion")
        if finish_reason not in {"stop", "length"}:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "DeepSeek 响应未正常结束。",
                retryable=False, attempts=attempts, status_code=200,
            )
        usage = _parse_usage(data.get("usage"))
        return content, LLMCallRecord(
            purpose=purpose,
            response_id=data.get("id") if isinstance(data.get("id"), str) else None,
            response_model=data.get("model") if isinstance(data.get("model"), str) else None,
            finish_reason=finish_reason,
            usage=usage,
        )
    except LLMError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        raise LLMError(
            LLMErrorCode.INVALID_RESPONSE,
            "DeepSeek 返回了无法解析的响应结构。",
            retryable=False, attempts=attempts, status_code=200,
        ) from None


def _parse_usage(value: Any) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    fields: dict[str, int | None] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        item = value.get(name)
        fields[name] = item if type(item) is int and item >= 0 else None
    if not any(item is not None for item in fields.values()):
        return None
    try:
        return TokenUsage(**fields)
    except ValidationError:
        return None


def _validation_diagnostics(error: ValidationError) -> str:
    """Render bounded, input-free schema errors suitable for repair prompts and reports."""
    items = []
    for detail in error.errors(include_input=False, include_context=False)[:20]:
        location = ".".join(str(part) for part in detail.get("loc", ())) or "<root>"
        items.append({
            "path": location,
            "type": detail.get("type", "validation_error"),
            "message": detail.get("msg", "invalid value"),
        })
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def _normalize_generated_payload(value: Any) -> tuple[str, ...]:
    """Repair deterministic relationship metadata without changing tests or oracles.

    Larger plans occasionally emit redundant scenario metadata inconsistently.  A
    unit scenario with no relationship or steps can only involve its target, while
    a collaboration scenario must include all functions named by its relationships.
    Those facts are already authoritative elsewhere in the payload, so completing
    or trimming the redundant list is safe and avoids a probabilistic repair call.
    All semantic, source-evidence and code checks still run afterwards.
    """
    if not isinstance(value, dict):
        return ()
    plan = value.get("plan")
    if not isinstance(plan, dict) or plan.get("schema_version") != "1.1":
        return ()
    target_functions = plan.get("target_functions")
    relationships = plan.get("relationships")
    scenarios = plan.get("scenarios")
    if not isinstance(target_functions, list) or not isinstance(relationships, list) or not isinstance(scenarios, list):
        return ()
    target_order = [item for item in target_functions if isinstance(item, str)]
    relationship_functions: dict[str, list[str]] = {}
    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        relation_id = relationship.get("relation_id")
        involved = relationship.get("involved_functions")
        if isinstance(relation_id, str) and isinstance(involved, list) and all(isinstance(item, str) for item in involved):
            relationship_functions[relation_id] = involved

    changes: list[str] = []
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            continue
        involved = scenario.get("involved_functions")
        relation_ids = scenario.get("relationship_ids")
        steps = scenario.get("steps")
        target = scenario.get("target_function")
        if (
            not isinstance(involved, list)
            or not isinstance(relation_ids, list)
            or not isinstance(steps, list)
            or not isinstance(target, str)
        ):
            continue
        if not all(isinstance(item, str) for item in involved + relation_ids):
            continue
        scenario_id = scenario.get("scenario_id")
        label = scenario_id if isinstance(scenario_id, str) else str(index)
        if scenario.get("test_kind") == "unit" and not relation_ids and not steps:
            normalized = [target]
            if normalized != involved:
                scenario["involved_functions"] = normalized
                changes.append(f"trimmed involved_functions for unit scenario {label}")
            continue
        if scenario.get("test_kind") != "collaboration":
            continue
        required = set(involved)
        required.add(target)
        for relation_id in relation_ids:
            required.update(relationship_functions.get(relation_id, ()))
        normalized = list(dict.fromkeys(involved))
        normalized.extend(name for name in target_order if name in required and name not in normalized)
        if normalized != involved:
            scenario["involved_functions"] = normalized
            changes.append(f"completed involved_functions for collaboration scenario {label}")
    return tuple(changes)


def _sum_usage(calls: list[LLMCallRecord]) -> TokenUsage | None:
    usages = [call.usage for call in calls]
    if not usages or any(usage is None for usage in usages):
        return None
    values: dict[str, int | None] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        components = [getattr(usage, name) for usage in usages]
        values[name] = sum(components) if all(value is not None for value in components) else None
    return TokenUsage(**values)


def _http_failure(status: int) -> tuple[LLMErrorCode, str, bool]:
    if status == 400:
        return LLMErrorCode.INVALID_REQUEST, "DeepSeek 拒绝了请求，请检查模型名、上下文和请求参数。", False
    if status == 401:
        return LLMErrorCode.AUTHENTICATION, "DeepSeek 认证失败，请检查 API Key。", False
    if status == 402:
        return LLMErrorCode.INSUFFICIENT_BALANCE, "DeepSeek 账号余额不足。", False
    if status == 403:
        return LLMErrorCode.PERMISSION, "DeepSeek 拒绝访问，请检查账号或模型权限。", False
    if status == 404:
        return LLMErrorCode.NOT_FOUND, "DeepSeek 接口或模型不存在，请检查服务地址和模型名。", False
    if status == 429:
        return LLMErrorCode.RATE_LIMIT, "DeepSeek 请求受限，有限重试后仍未恢复。", True
    if status in {408, 409} or status >= 500:
        return LLMErrorCode.SERVICE_UNAVAILABLE, "DeepSeek 服务暂时不可用，有限重试后仍未恢复。", True
    return LLMErrorCode.INVALID_RESPONSE, f"DeepSeek 返回未处理的 HTTP 状态 {status}。", False


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
