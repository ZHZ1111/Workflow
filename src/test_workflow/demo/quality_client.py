"""Review fixtures implement the same protocol without configuration or HTTP."""

from pathlib import Path
from time import perf_counter

from pydantic import ValidationError

from ..quality_client import ReviewClientError, ReviewErrorCode, ReviewResponse, parse_review_decision
from ..quality_contracts import ReviewTelemetry
from ..quality_prompts import ReviewContext


class FixtureReviewClient:
    safe_config = {"client_mode": "offline", "max_http_attempts": 1, "max_format_repairs": 0}

    def __init__(self, fixtures: tuple[Path, ...]):
        if not fixtures:
            raise ValueError("at least one explicit review fixture is required")
        self.fixtures = iter(fixtures)
        self.contexts: list[ReviewContext] = []

    def review(self, context: ReviewContext) -> ReviewResponse:
        start = perf_counter()

        def telemetry(category=None, code=None):
            return ReviewTelemetry(client_mode="offline", client_calls=1, max_request_attempts=1,
                                   latency_seconds=round(perf_counter() - start, 6),
                                   error_category=category, provider_code=code.value if code else None)

        try:
            if not isinstance(context, ReviewContext):
                raise TypeError("review requires a typed context")
            context = ReviewContext.model_validate_json(context.canonical_json())
        except (ValueError, TypeError):
            raise ReviewClientError(ReviewErrorCode.INVALID_CONTEXT, telemetry("contract", ReviewErrorCode.INVALID_CONTEXT)) from None
        self.contexts.append(context)
        try:
            content = next(self.fixtures).read_text(encoding="utf-8")
        except StopIteration:
            raise ReviewClientError(ReviewErrorCode.FIXTURE_EXHAUSTED, telemetry("budget", ReviewErrorCode.FIXTURE_EXHAUSTED)) from None
        except (OSError, UnicodeError):
            raise ReviewClientError(ReviewErrorCode.FIXTURE_IO, telemetry("workspace", ReviewErrorCode.FIXTURE_IO)) from None
        try:
            decision = parse_review_decision(content)
        except (ValueError, ValidationError, RecursionError):
            raise ReviewClientError(ReviewErrorCode.FIXTURE_INVALID, telemetry("format", ReviewErrorCode.FIXTURE_INVALID)) from None
        return ReviewResponse(decision=decision, raw_response=content, telemetry=telemetry())
