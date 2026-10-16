"""Bounded review business output and telemetry, independent of workflow state."""

import ast
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StringConstraints, computed_field, model_validator
from testgen_agent.models import ContractModel, Identifier, TestName, Text, TokenUsage

ReviewText = Annotated[Text, StringConstraints(max_length=2000)]
ReviewQuote = Annotated[Text, StringConstraints(max_length=4000)]
FindingId = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
QualityStatus = Literal["disabled", "not_run", "passed", "changes_requested", "needs_review", "error"]
QualityStopReason = Literal["approved", "quality_gaps", "quality_budget_reached", "quality_no_progress",
                            "ambiguous_specification", "review_error"]


class Finding(ContractModel):
    finding_id: FindingId
    category: Literal["missing_requirement", "missing_boundary", "weak_assertion",
                      "unsupported_expectation", "redundancy", "ambiguous_specification"]
    blocking: StrictBool
    target_functions: Annotated[tuple[Annotated[Identifier, StringConstraints(max_length=128)], ...],
                                Field(min_length=1, max_length=5)]
    related_tests: Annotated[tuple[Annotated[TestName, StringConstraints(max_length=128)], ...],
                            Field(max_length=50)] = ()
    spec_quote: ReviewQuote | None = None
    test_code_quote: ReviewQuote | None = None
    description: ReviewText
    suggested_case: ReviewText | None = None

    @model_validator(mode="after")
    def supported_finding(self):
        if len(set(self.target_functions)) != len(self.target_functions) or len(set(self.related_tests)) != len(self.related_tests):
            raise ValueError("review references must be unique")
        if self.blocking and self.spec_quote is None:
            raise ValueError("blocking findings require specification evidence")
        if self.blocking and self.category == "redundancy":
            raise ValueError("redundancy is advisory in the first review version")
        if self.blocking and self.category != "ambiguous_specification" and self.suggested_case is None:
            raise ValueError("blocking revision findings require an actionable suggested case")
        return self


class ReviewDecision(ContractModel):
    verdict: Literal["approve", "request_revision", "request_review"]
    summary: ReviewText
    findings: Annotated[tuple[Finding, ...], Field(max_length=20)] = ()
    questions: Annotated[tuple[ReviewText, ...], Field(max_length=10)] = ()

    @model_validator(mode="after")
    def consistent_verdict(self):
        ids = [finding.finding_id for finding in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("finding IDs must be unique within one decision")
        blocking = tuple(finding for finding in self.findings if finding.blocking)
        ambiguous = any(finding.category == "ambiguous_specification" for finding in self.findings)
        if self.verdict == "approve" and (blocking or self.questions or ambiguous):
            raise ValueError("approval cannot contain unresolved blocking findings or specification questions")
        if self.verdict == "request_revision" and (not blocking or self.questions or ambiguous):
            raise ValueError("revision requires actionable blocking findings without unresolved specification questions")
        if self.verdict == "request_review" and (
            not self.questions or not any(finding.spec_quote is not None for finding in self.findings)
        ):
            raise ValueError("human review requires concrete questions and specification evidence")
        return self


class ReviewTelemetry(ContractModel):
    client_mode: Literal["offline", "mock", "real"]
    client_calls: Annotated[StrictInt, Field(ge=0, le=1)] = 0
    request_attempts: Annotated[StrictInt, Field(ge=0, le=3)] | None = 0
    max_request_attempts: Annotated[StrictInt, Field(ge=1, le=3)] = 3
    format_repairs: Annotated[StrictInt, Field(ge=0, le=2)] | None = 0
    total_usage: TokenUsage | None = None
    error_category: Literal["transient", "authentication", "configuration", "format",
                            "workspace", "budget", "contract", "internal"] | None = None
    provider_code: FindingId | None = None
    retryable: StrictBool = False
    status_code: Annotated[StrictInt, Field(ge=100, le=599)] | None = None

    @model_validator(mode="after")
    def bounded_attempts(self):
        attempts = self.request_attempts
        if attempts is not None and (attempts > self.max_request_attempts or (attempts and not self.client_calls)):
            raise ValueError("review attempts require a client call and must fit the shared request budget")
        if self.format_repairs and (
            not self.client_calls or (attempts is not None and self.format_repairs >= attempts)
        ):
            raise ValueError("format repairs must fit within the same request attempts")
        if self.client_mode == "offline" and (attempts or self.format_repairs):
            raise ValueError("offline review cannot perform HTTP attempts or format repairs")
        return self

    @computed_field
    @property
    def external_requests(self) -> int | None:
        return self.request_attempts if self.client_mode == "real" else 0


def validate_review_evidence(decision: ReviewDecision, *, specification: str,
                             test_code: str, target_functions: tuple[str, ...]) -> None:
    """Check reference provenance, not the semantic correctness of model opinions."""
    tests = {node.name for node in ast.walk(ast.parse(test_code))
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")}
    for finding in decision.findings:
        if not set(finding.target_functions).issubset(target_functions):
            raise ValueError("quality finding references an unknown target function")
        if not set(finding.related_tests).issubset(tests):
            raise ValueError("quality finding references an unknown existing test")
        if finding.spec_quote is not None and finding.spec_quote not in specification:
            raise ValueError("quality specification quote is absent from the submitted snapshot")
        if finding.test_code_quote is not None and finding.test_code_quote not in test_code:
            raise ValueError("quality test quote is absent from the submitted candidate")
