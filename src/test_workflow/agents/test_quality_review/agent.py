"""One gated review call; only the workflow may commit state and schedule revisions."""

import json
from time import perf_counter
from typing import Literal

from test_workflow.agents.test_generation.core.models import AgentAction

from ...adapters.quality import (
    QualityGateError, preflight_quality, review_directory, save_quality_assessment, validate_quality_response,
)
from ...contracts import AgentContext, AgentResult, Message, MessagePayload, QualityFeedback
from ...quality_client import ReviewClient, ReviewClientError, ReviewErrorCode, ReviewResponse
from ...quality_contracts import ReviewTelemetry
from ...quality_prompts import ReviewContext
from ...storage import atomic_write


class TestQualityReviewAgent:
    __test__ = False
    agent_id = "test_quality_review"

    def __init__(self, client: ReviewClient, *, client_mode: Literal["offline", "mock", "real"] | None = None,
                 max_request_attempts: int | None = None):
        if not callable(getattr(client, "review", None)):
            raise TypeError("client must implement review(context)")
        policy = getattr(client, "safe_config", {})
        if not isinstance(policy, dict):
            raise ValueError("review client policy must be a public dictionary")
        declared_mode = policy.get("client_mode")
        mode = client_mode or declared_mode or "real"
        if mode not in {"offline", "mock", "real"} or (declared_mode is not None and mode != declared_mode):
            raise ValueError("review client mode must match its declared transport")
        client_limit = policy.get("max_http_attempts", 3)
        limit = client_limit if max_request_attempts is None else max_request_attempts
        if (type(client_limit) is not int or not 1 <= client_limit <= 3
                or type(limit) is not int or not 1 <= limit <= 3):
            raise ValueError("review request budget must be between one and three")
        self.client, self.client_mode, self.max_attempts = client, mode, min(limit, client_limit)

    def run(self, context: AgentContext) -> AgentResult:
        calls = 0
        reserved = False
        observed = None
        start = perf_counter()

        def accept_telemetry(value):
            if not isinstance(value, ReviewTelemetry):
                raise QualityGateError("quality_client_contract_error")
            telemetry = ReviewTelemetry.model_validate_json(value.canonical_json())
            if telemetry.client_mode != self.client_mode or telemetry.client_calls != 1:
                raise QualityGateError("quality_client_contract_error")
            if (telemetry.max_request_attempts > self.max_attempts
                    or (telemetry.request_attempts is not None and telemetry.request_attempts > self.max_attempts)
                    or (telemetry.format_repairs is not None and telemetry.format_repairs > 1)):
                raise QualityGateError("quality_request_budget_exceeded")
            return telemetry

        def failed(code, category):
            telemetry = observed.updated(error_category=category) if observed is not None else ReviewTelemetry(
                client_mode=self.client_mode, client_calls=calls, request_attempts=None if calls else 0,
                max_request_attempts=self.max_attempts, format_repairs=None if calls else 0,
                latency_seconds=round(perf_counter() - start, 6), error_category=category)
            diagnostics = ()
            if reserved:
                try:
                    directory = review_directory(context)
                    if directory.is_dir():
                        diagnostics = (directory.relative_to(context.run_dir).as_posix(),)
                        atomic_write(directory / "failure.json", json.dumps(
                            {"error_code": code, "review": telemetry.model_dump(mode="json")}, ensure_ascii=False) + "\n")
                except (OSError, ValueError):
                    pass
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=self.agent_id,
                               status="error", error_code=code, diagnostic_paths=diagnostics, review=telemetry)

        try:
            context = AgentContext.model_validate_json(context.canonical_json())
            prepared = preflight_quality(context, self.max_attempts)
            # Reserve once before spending a request; failed attempts remain diagnostic
            # records and cannot silently trigger another call on the same candidate.
            review_directory(context).mkdir(parents=True, exist_ok=False)
            reserved = True
            request = ReviewContext.model_validate_json(prepared.review.canonical_json())
            calls = 1
            try:
                response = self.client.review(request)
            except ReviewClientError:
                raise
            except Exception:
                return failed("quality_internal_error", "internal")
            if not isinstance(response, ReviewResponse):
                raise QualityGateError("quality_client_contract_error")
            observed = accept_telemetry(response.telemetry)
            response = ReviewResponse.model_validate_json(response.canonical_json())
            validate_quality_response(context, prepared, response)
            quality, artifacts = save_quality_assessment(context, prepared, response)
            messages = ()
            # This is a proposal; the S4 coordinator still owns permission to continue.
            if quality.decision.verdict == "request_revision" and context.round < prepared.review.max_rounds:
                reference = next(ref for ref in artifacts if ref.kind == "quality_assessment")
                feedback = QualityFeedback(candidate_id=quality.candidate_id, assessment=reference,
                                           findings=tuple(item for item in quality.decision.findings if item.blocking))
                messages = (Message(message_id=f"msg-quality-feedback-{context.round:02d}", run_id=context.run_id,
                                    round=context.round, sender=self.agent_id, receiver="test_generation", type="quality_feedback",
                                    payload=MessagePayload(candidate_id=quality.candidate_id, artifact=reference,
                                                           reason=quality.decision.summary, recommended_action=AgentAction.ADD_MISSING_CASES,
                                                           quality_feedback=feedback)),)
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=self.agent_id,
                               artifacts=artifacts, messages=messages, quality_assessment=quality, review=observed)
        except ReviewClientError as error:
            try:
                if not isinstance(error.code, ReviewErrorCode):
                    raise ValueError("invalid client code")
                observed = accept_telemetry(error.telemetry)
            except (ValueError, TypeError, AttributeError):
                return failed("quality_client_contract_error", "contract")
            return failed(error.code.value, observed.error_category or "contract")
        except QualityGateError as error:
            return failed(error.code, "budget" if error.code == "quality_request_budget_exceeded" else "contract")
        except OSError:
            return failed("quality_io_error", "workspace")
        except (ValueError, TypeError):
            return failed("quality_contract_error", "contract")
        except Exception:
            return failed("quality_internal_error", "internal")
