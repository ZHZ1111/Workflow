"""Injectable fixed responses; no HTTP, environment configuration or model inference."""

from pathlib import Path

from testgen_agent.llm_client import GenerationContext, LLMCallRecord, LLMGenerationResult
from testgen_agent.models import GeneratedTests


class FixtureClient:
    safe_config = {"max_http_attempts": 1}

    def __init__(self, fixtures: tuple[Path, ...]):
        self.fixtures = iter(fixtures)
        self.contexts: list[GenerationContext] = []

    def generate_tests(self, context: GenerationContext) -> LLMGenerationResult:
        self.contexts.append(context)
        generated = GeneratedTests.model_validate_json(next(self.fixtures).read_text(encoding="utf-8"))
        return LLMGenerationResult(generated=generated, provider="deepseek",
                                   requested_model="offline-fixture-model", response_model="offline-fixture-model",
                                   attempts=1, format_repairs=0, latency_seconds=0,
                                   calls=(LLMCallRecord(purpose="generation", response_id="offline-fixture"),))
