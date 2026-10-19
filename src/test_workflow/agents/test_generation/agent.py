"""Generate exactly one initial/revision candidate, leaving all execution outside."""

from typing import Literal

from pydantic import ValidationError
from test_workflow.agents.test_generation.core.config import ConfigError
from test_workflow.agents.test_generation.core.generation import GenerationClient
from test_workflow.agents.test_generation.core.llm_client import LLMError, LLMErrorCode
from test_workflow.agents.test_generation.core.models import AgentAction

from ...adapters.agentdev import (
    GenerationAdapterError, RecordedClient, adapt_candidate, cumulative_usage, generate_one, preflight,
)
from ...contracts import AgentContext, AgentResult, GenerationTelemetry, Message, MessagePayload, StatePatch


class TestGenerationAgent:
    __test__ = False

    def __init__(self, client: GenerationClient, *, client_mode: Literal["offline", "mock", "real"] = "real",
                 max_request_attempts: int | None = None):
        if not callable(getattr(client, "generate_tests", None)):
            raise TypeError("client must implement generate_tests(context)")
        if client_mode not in {"offline", "mock", "real"}:
            raise ValueError("unknown client mode")
        policy = getattr(client, "safe_config", {})
        client_limit = policy.get("max_http_attempts")
        limit = (client_limit or 5) if max_request_attempts is None else max_request_attempts
        if type(limit) is not int or not 1 <= limit <= 7 or (client_limit is not None and client_limit > limit):
            raise ValueError("client retry policy exceeds the action request budget")
        self.client = client
        self.client_mode = client_mode
        self.max_attempts = limit

    def run(self, context: AgentContext) -> AgentResult:
        recorded = RecordedClient(self.client, self.max_attempts)

        def telemetry(**error_fields):
            result = recorded.result
            return GenerationTelemetry(client_mode=self.client_mode, client_calls=recorded.calls,
                                       request_attempts=recorded.attempts, max_request_attempts=self.max_attempts,
                                       format_repairs=result.format_repairs if result is not None else (None if recorded.calls else 0),
                                       total_usage=result.total_usage if result is not None else None,
                                       **error_fields)

        def failed(code, category, **details):
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                               status="error", error_code=code,
                               generation=telemetry(error_category=category, **details))

        try:
            business = preflight(context)
            staged = generate_one(context, business, recorded)
            wrapped = adapt_candidate(context, staged)
            candidate = wrapped.candidate
            if candidate.generated.questions:
                action = AgentAction.REQUEST_REVIEW
            elif candidate.validation_error:
                action = AgentAction.REPAIR_GENERATED_TEST
            elif candidate.test_path is None:
                action = AgentAction.FINISH
            else:
                action = AgentAction.RUN_TESTS
            # Invalid candidates retain evidence, but never emit candidate_ready.
            messages = ()
            if action == AgentAction.RUN_TESTS:
                message = Message(message_id=f"msg-candidate-{context.round:02d}", run_id=context.run_id,
                                  round=context.round, sender=context.agent_id, receiver="test_execution",
                                  type="candidate_ready", payload=MessagePayload(
                                      candidate_id=f"{context.run_id}:{context.round}", artifact=wrapped.handoff,
                                      reason="Candidate passed host validation; execution has not started.",
                                      recommended_action=action))
                messages = (message,)
            observations = telemetry()
            return AgentResult(run_id=context.run_id, round=context.round, agent_id=context.agent_id,
                               patch=StatePatch(candidate=candidate, next_action=action,
                                                api_calls_delta=observations.external_requests,
                                                token_usage=cumulative_usage(business, observations.total_usage),
                                                token_usage_reported=True),
                               artifacts=wrapped.artifacts, messages=messages, generation=observations)
        except GenerationAdapterError as error:
            return failed(error.code, error.category)
        except LLMError as error:
            if error.attempts > self.max_attempts:
                return failed("request_budget_exceeded", "budget", provider_code=error.code.value)
            if error.code in {LLMErrorCode.AUTHENTICATION, LLMErrorCode.PERMISSION, LLMErrorCode.INSUFFICIENT_BALANCE}:
                category = "authentication"
            elif error.code in {LLMErrorCode.INVALID_RESPONSE, LLMErrorCode.INVALID_GENERATED_OUTPUT}:
                category = "format"
            elif error.retryable:
                category = "transient"
            else:
                category = "configuration"
            return failed(f"llm_{error.code.value}", category, provider_code=error.code.value,
                          retryable=error.retryable, status_code=error.status_code)
        except ConfigError:
            recorded.attempts = 0
            return failed("generation_configuration_error", "configuration")
        except ValidationError:
            return failed("generation_contract_error", "contract")
        except OSError:
            return failed("generation_workspace_error", "workspace")
        except (ValueError, TypeError):
            return failed("generation_contract_error", "contract")
        except Exception:
            return failed("generation_internal_error", "internal")
