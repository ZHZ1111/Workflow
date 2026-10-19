"""Independent review transport; one shared HTTP budget, no generation parser."""

from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
import json
import math
import time
from time import perf_counter
from typing import Annotated, Protocol, runtime_checkable

import httpx
from pydantic import Field, StrictInt, ValidationError, model_validator
from test_workflow.agents.test_generation.core.config import AppConfig
from test_workflow.agents.test_generation.core.llm_client import create_http_client
from test_workflow.agents.test_generation.core.models import ContractModel, Text, TokenUsage

from .quality_contracts import ReviewDecision, ReviewTelemetry
from .quality_prompts import ReviewContext, build_review_prompt, build_review_repair_prompt

MAX_RESPONSE_CHARS = 512_000
MAX_HTTP_RESPONSE_BYTES = 2_000_000


class ReviewRetryPolicy(ContractModel):
    max_http_attempts: Annotated[StrictInt, Field(ge=1, le=3)] = 3
    max_transient_retries: Annotated[StrictInt, Field(ge=0, le=2)] = 2
    max_format_repairs: Annotated[StrictInt, Field(ge=0, le=1)] = 1
    base_delay_seconds: Annotated[float, Field(ge=0, le=10)] = 0.25
    max_delay_seconds: Annotated[float, Field(ge=0, le=10)] = 2

    @model_validator(mode="after")
    def ordered_delay(self):
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("review retry delays must satisfy base <= max")
        return self


class ReviewResponse(ContractModel):
    decision: ReviewDecision
    raw_response: Annotated[Text, Field(max_length=MAX_RESPONSE_CHARS)]
    telemetry: ReviewTelemetry

    @model_validator(mode="after")
    def consistent_response(self):
        if parse_review_decision(self.raw_response) != self.decision:
            raise ValueError("raw review response differs from the validated decision")
        if self.telemetry.error_category is not None or self.telemetry.client_calls != 1:
            raise ValueError("successful review requires one error-free client invocation")
        return self


@runtime_checkable
class ReviewClient(Protocol):
    def review(self, context: ReviewContext) -> ReviewResponse: ...


class ReviewErrorCode(StrEnum):
    INVALID_CONTEXT = "invalid_review_context"
    INVALID_RESPONSE = "invalid_review_response"
    INVALID_OUTPUT = "invalid_review_output"
    TIMEOUT = "review_timeout"
    NETWORK = "review_network"
    AUTHENTICATION = "review_authentication"
    PERMISSION = "review_permission"
    INSUFFICIENT_BALANCE = "review_insufficient_balance"
    INVALID_REQUEST = "review_invalid_request"
    NOT_FOUND = "review_not_found"
    RATE_LIMIT = "review_rate_limit"
    SERVICE_UNAVAILABLE = "review_service_unavailable"
    CONFIGURATION = "review_configuration_error"
    INTERNAL = "review_internal_error"
    FIXTURE_EXHAUSTED = "review_fixture_exhausted"
    FIXTURE_INVALID = "review_fixture_invalid"
    FIXTURE_IO = "review_fixture_io"


class ReviewClientError(RuntimeError):
    """Fixed code and safe telemetry, without provider bodies or exception text."""

    def __init__(self, code: ReviewErrorCode, telemetry: ReviewTelemetry):
        super().__init__(f"Quality review failed: {code.value}")
        self.code = code
        self.telemetry = telemetry

    def safe_details(self) -> dict:
        return {"code": self.code.value, "review": self.telemetry.model_dump(mode="json")}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("non-finite JSON constant")


def parse_review_decision(content: str) -> ReviewDecision:
    if not isinstance(content, str) or len(content) > MAX_RESPONSE_CHARS:
        raise ValueError("review response exceeds the content limit")
    payload = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    return ReviewDecision.model_validate(payload)


def _diagnostics(error: ValidationError) -> tuple[dict[str, str], ...]:
    # Extra-field names and validation messages may contain model-controlled text.
    fields = {"verdict", "summary", "findings", "questions", "finding_id", "category", "blocking",
              "target_functions", "related_tests", "spec_quote", "test_code_quote", "description", "suggested_case"}
    return tuple({"path": ".".join(str(part) if type(part) is int else part if part in fields else "<field>"
                                   for part in item["loc"]) or "<root>", "type": item["type"]}
                 for item in error.errors(include_input=False, include_context=False, include_url=False)[:8])


def _usage(value) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    fields = {name: value.get(name) if type(value.get(name)) is int and value[name] >= 0 else None
              for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    if all(item is None for item in fields.values()):
        return None
    try:
        return TokenUsage(**fields)
    except ValidationError:
        return None


def _sum_usage(usages: list[TokenUsage | None]) -> TokenUsage | None:
    if not usages or any(item is None for item in usages):
        return None
    fields = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        values = [getattr(item, name) for item in usages]
        fields[name] = sum(values) if all(item is not None for item in values) else None
    return TokenUsage(**fields)


def _http_failure(status: int) -> tuple[ReviewErrorCode, str, bool]:
    permanent = {
        400: (ReviewErrorCode.INVALID_REQUEST, "configuration"),
        401: (ReviewErrorCode.AUTHENTICATION, "authentication"),
        402: (ReviewErrorCode.INSUFFICIENT_BALANCE, "authentication"),
        403: (ReviewErrorCode.PERMISSION, "authentication"),
        404: (ReviewErrorCode.NOT_FOUND, "configuration"),
    }
    if status in permanent:
        code, category = permanent[status]
        return code, category, False
    if status == 429:
        return ReviewErrorCode.RATE_LIMIT, "transient", True
    if status in {408, 409} or status >= 500:
        return ReviewErrorCode.SERVICE_UNAVAILABLE, "transient", True
    return ReviewErrorCode.INVALID_RESPONSE, "format", False


def _retry_delay(policy: ReviewRetryPolicy, retries: int, retry_after: str | None) -> float:
    delay = policy.base_delay_seconds * 2 ** retries
    if retry_after is not None:
        try:
            parsed = float(retry_after)
            if math.isfinite(parsed):
                delay = max(0, parsed)
        except ValueError:
            try:
                when = parsedate_to_datetime(retry_after)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                delay = max(0, (when - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return min(delay, policy.max_delay_seconds)


class DeepSeekReviewClient:
    def __init__(self, config: AppConfig, *, retry_policy: ReviewRetryPolicy | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep):
        self._config = config
        self._retry = ReviewRetryPolicy.model_validate(retry_policy or ReviewRetryPolicy())
        self._transport = transport
        self._sleep = sleep
        self.client_mode = "mock" if isinstance(transport, httpx.MockTransport) else "real"

    @property
    def safe_config(self) -> dict:
        return {"provider": self._config.provider, "model": self._config.model,
                "base_url": self._config.base_url, "request_timeout_seconds": self._config.request_timeout_seconds,
                "max_tokens": self._config.max_tokens, "client_mode": self.client_mode,
                **self._retry.model_dump(mode="json")}

    def review(self, context: ReviewContext) -> ReviewResponse:
        attempts = repairs = retries = 0
        usages: list[TokenUsage | None] = []
        start = perf_counter()
        limit = self._retry.max_http_attempts

        def telemetry(**failure):
            return ReviewTelemetry(client_mode=self.client_mode, client_calls=1, request_attempts=attempts,
                                   max_request_attempts=limit, format_repairs=repairs, total_usage=_sum_usage(usages),
                                   latency_seconds=round(perf_counter() - start, 6), **failure)

        def fail(code, category, *, retryable=False, status_code=None):
            return ReviewClientError(code, telemetry(error_category=category, provider_code=code.value,
                                                     retryable=retryable, status_code=status_code))

        try:
            if not isinstance(context, ReviewContext):
                raise TypeError("review requires a typed context")
            context = ReviewContext.model_validate_json(context.canonical_json())
            limit = min(limit, context.max_request_attempts)
            prompt = build_review_prompt(context)
        except (ValueError, TypeError):
            raise fail(ReviewErrorCode.INVALID_CONTEXT, "contract") from None

        messages = [{"role": "system", "content": prompt.system_instruction},
                    {"role": "user", "content": prompt.user_prompt}]
        try:
            with create_http_client(self._config, transport=self._transport) as client:
                while attempts < limit:
                    attempts += 1
                    usages.append(None)
                    retry_after = None
                    try:
                        response = client.post("chat/completions", json={
                            "model": self._config.model, "messages": messages, "stream": False,
                            "max_tokens": self._config.max_tokens, "thinking": {"type": "disabled"},
                            "response_format": {"type": "json_object"},
                        })
                    except httpx.TimeoutException:
                        code, category, retryable, status = ReviewErrorCode.TIMEOUT, "transient", True, None
                    except httpx.RequestError:
                        code, category, retryable, status = ReviewErrorCode.NETWORK, "transient", True, None
                    else:
                        status = response.status_code
                        if status == 200:
                            if len(response.content) > MAX_HTTP_RESPONSE_BYTES:
                                raise fail(ReviewErrorCode.INVALID_RESPONSE, "format", status_code=200)
                            try:
                                data = response.json()
                                if not isinstance(data, dict):
                                    raise ValueError("invalid envelope")
                                usages[-1] = _usage(data.get("usage"))
                                choice = data["choices"][0]
                                message = choice["message"]
                                content = message["content"]
                                finish = choice["finish_reason"]
                                if (not isinstance(content, str) or not content.strip() or finish not in {"stop", "length"}
                                        or message.get("tool_calls") or message.get("refusal")):
                                    raise ValueError("invalid completion")
                            except (ValueError, TypeError, KeyError, IndexError, RecursionError):
                                raise fail(ReviewErrorCode.INVALID_RESPONSE, "format", status_code=200) from None
                            diagnostics = ({"path": "<root>", "type": "output_truncated"},)
                            decision = None
                            if finish == "stop":
                                try:
                                    decision = parse_review_decision(content)
                                except ValidationError as error:
                                    diagnostics = _diagnostics(error)
                                except (ValueError, RecursionError):
                                    diagnostics = ({"path": "<root>", "type": "invalid_json"},)
                            if decision is not None:
                                return ReviewResponse(decision=decision, raw_response=content, telemetry=telemetry())
                            if repairs >= self._retry.max_format_repairs or attempts >= limit:
                                raise fail(ReviewErrorCode.INVALID_OUTPUT, "format", status_code=200)
                            repairs += 1
                            messages.append({"role": "user", "content": build_review_repair_prompt(diagnostics)})
                            continue
                        code, category, retryable = _http_failure(status)
                        retry_after = response.headers.get("Retry-After")
                    if not retryable or attempts >= limit or retries >= self._retry.max_transient_retries:
                        raise fail(code, category, retryable=retryable, status_code=status)
                    self._sleep(_retry_delay(self._retry, retries, retry_after))
                    retries += 1
        except ReviewClientError:
            raise
        except (ValueError, TypeError):
            raise fail(ReviewErrorCode.CONFIGURATION, "configuration") from None
        except Exception:
            raise fail(ReviewErrorCode.INTERNAL, "internal") from None
        raise fail(ReviewErrorCode.SERVICE_UNAVAILABLE, "transient", retryable=True)
