"""Append-only host events; no prompts, headers or internal model reasoning."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, JsonValue, StrictInt

from .llm_client import LLMError, LLMGenerationResult
from .models import AgentAction, AgentState, ContractModel, Text, TokenUsage


class TraceArtifactError(OSError):
    """Trace persistence failed; never pretend a missing trace was saved."""


class TraceEvent(ContractModel):
    schema_version: Literal[1] = 1
    run_id: Text
    sequence: Annotated[StrictInt, Field(ge=1)]
    timestamp: datetime
    round_number: Annotated[StrictInt, Field(ge=0, le=3)]
    phase: Literal["run_started", "tool_started", "tool_finished", "tool_failed", "decision", "run_finished"]
    state: dict[str, JsonValue]
    tool: Text | None = None
    action: AgentAction | None = None
    reason: Text
    input_summary: dict[str, JsonValue] = Field(default_factory=dict)
    result_summary: dict[str, JsonValue] = Field(default_factory=dict)


class TraceWriter:
    def __init__(self, root: Path, run_id: str, metadata: dict[str, Any]):
        self.root, self.run_id, self.sequence = root, run_id, 0
        self.path = root / "trace.jsonl"
        self.metadata_path = root / "run.json"
        try:
            root.mkdir(parents=True, exist_ok=False)
            with self.metadata_path.open("x", encoding="utf-8") as stream:
                json.dump({"schema_version": 1, "run_id": run_id, **metadata}, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            self.path.open("x", encoding="utf-8").close()
        except OSError as error:
            raise TraceArtifactError("Unable to create a fresh run and trace workspace.") from error

    def record(self, state: AgentState, phase: str, reason: str, *, round_number: int | None = None,
               tool: str | None = None, action: AgentAction | None = None,
               inputs: dict | None = None, result: dict | None = None) -> None:
        event = TraceEvent(
            run_id=self.run_id, sequence=self.sequence + 1, timestamp=datetime.now(timezone.utc),
            round_number=state.current_round if round_number is None else round_number, phase=phase,
            state={"current_round": state.current_round, "history_rounds": len(state.history),
                   "best_available_round": state.best_available_round, "next_action": state.next_action,
                   "stop_reason": state.stop_reason, "api_calls": state.api_calls},
            tool=tool, action=action, reason=reason, input_summary=inputs or {}, result_summary=result or {},
        )
        try:
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(event.canonical_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as error:
            raise TraceArtifactError("Unable to append run trace; earlier events are retained.") from error
        self.sequence += 1


class ObservedGenerationClient:
    """Observe adapter-reported attempts even when candidate staging then fails."""

    def __init__(self, client, trace: TraceWriter, state_getter):
        self.client, self.trace, self.state_getter = client, trace, state_getter
        self.round_number = 1
        self.action = AgentAction.GENERATE_TESTS
        self.reason = "依据源码与行为说明生成候选。"
        self.api_calls = 0
        self.requests = 0
        self.usage: TokenUsage | None = None

    def generate_tests(self, context):
        from .agent import _add_usage
        state = self.state_getter()
        inputs = {"system_characters": len(context.system_instruction), "task_characters": len(context.user_prompt),
                  "requested_model": state.request.model.model, "provider": state.request.model.provider}
        self.trace.record(state, "tool_started", self.reason, round_number=self.round_number,
                          tool="llm.generate_tests", action=self.action, inputs=inputs)
        self.requests += 1
        try:
            result = self.client.generate_tests(context)
            if not isinstance(result, LLMGenerationResult):
                raise TypeError("generate_tests must return LLMGenerationResult")
        except Exception as error:
            if isinstance(error, LLMError):
                self.api_calls += error.attempts
            self.usage = None
            self.trace.record(state.updated(api_calls=self.api_calls, token_usage=None), "tool_failed", "模型适配器未返回可用候选。", round_number=self.round_number,
                              tool="llm.generate_tests", action=self.action,
                              result={"error_type": type(error).__name__,
                                      "error_code": error.code if isinstance(error, LLMError) else None,
                                      "attempts": error.attempts if isinstance(error, LLMError) else None,
                                      "cumulative_http_attempts": self.api_calls, "token_usage": None})
            raise
        self.api_calls += result.attempts
        self.usage = _add_usage(self.usage, result.total_usage, first=self.requests == 1)
        self.trace.record(state.updated(api_calls=self.api_calls, token_usage=self.usage), "tool_finished", "模型返回结构化候选，尚需宿主校验与执行。",
                          round_number=self.round_number, tool="llm.generate_tests", action=self.action,
                          result={"requested_model": result.requested_model, "response_model": result.response_model,
                                  "attempts": result.attempts, "attempt_count_source": "adapter_reported", "format_repairs": result.format_repairs,
                                  "host_normalizations": list(result.host_normalizations),
                                  "latency_seconds": result.latency_seconds, "cumulative_http_attempts": self.api_calls,
                                  "token_usage": result.total_usage.model_dump() if result.total_usage else None,
                                  "calls": [{"purpose": item.purpose, "response_model": item.response_model,
                                             "finish_reason": item.finish_reason,
                                             "usage": item.usage.model_dump() if item.usage else None} for item in result.calls]})
        return result
