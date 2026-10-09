"""B1 contracts: generated content, measured results and host state stay separate."""

from __future__ import annotations

from enum import StrEnum
import keyword
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator, BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictInt,
    StringConstraints, computed_field, field_validator, model_validator,
)

from .config import AppConfig


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("text must not be blank")
    return value  # Preserve source, code, whitespace and Chinese text verbatim.


def _identifier(value: str) -> str:
    if not value.isidentifier() or keyword.iskeyword(value):
        raise ValueError("a Python identifier is required")
    return value


def _test_name(value: str) -> str:
    if not value.startswith("test_") or len(value) == 5:
        raise ValueError("a pytest function name starting with test_ is required")
    return value


Text = Annotated[str, StringConstraints(strict=True, min_length=1), AfterValidator(_nonblank)]
Identifier = Annotated[Text, AfterValidator(_identifier)]
TestName = Annotated[Identifier, AfterValidator(_test_name)]
Count = Annotated[StrictInt, Field(ge=0)]
Line = Annotated[StrictInt, Field(ge=1)]
RoundNumber = Annotated[StrictInt, Field(ge=1, le=3)]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ContractModel(BaseModel):
    """Validated snapshots. Computed output fields are not accepted as input."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_default=True,
        revalidate_instances="always", hide_input_in_errors=True, allow_inf_nan=False,
    )

    def canonical_json(self, *, indent: int | None = None) -> str:
        """Serialize source fields only, so model_validate_json can restore them."""
        return self.model_dump_json(round_trip=True, indent=indent, ensure_ascii=False)

    def updated(self, **changes: object) -> Self:
        """Unlike model_copy(update=...), this validates all changed fields."""
        return type(self).model_validate({**self.model_dump(round_trip=True), **changes})


class OracleMode(StrEnum):
    SPECIFICATION = "specification"
    CHARACTERIZATION = "characterization"


class AgentAction(StrEnum):
    ANALYZE_SOURCE = "analyze_source"
    GENERATE_TESTS = "generate_tests"
    RUN_TESTS = "run_tests"
    REPAIR_GENERATED_TEST = "repair_generated_test"
    ADD_MISSING_CASES = "add_missing_cases"
    REQUEST_REVIEW = "request_review"
    FINISH = "finish"


class StopReason(StrEnum):
    COVERAGE_TARGET_MET = "coverage_target_met"
    ROUND_LIMIT_REACHED = "round_limit_reached"
    NO_PROGRESS = "no_progress"
    SUSPECTED_SOURCE_DEFECT = "suspected_source_defect"
    AMBIGUOUS_SPECIFICATION = "ambiguous_specification"
    EXECUTION_TIMEOUT = "execution_timeout"
    UNRECOVERABLE_ERROR = "unrecoverable_error"


class TaskStatus(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class ModelInfo(ContractModel):
    """Public configuration only; never embed AppConfig or a key in task reports."""

    provider: Literal["deepseek"] = "deepseek"
    model: Text
    base_url: Text
    request_timeout_seconds: Annotated[StrictInt, Field(ge=1, le=600)] = 60

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return AppConfig.validate_model(value)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return AppConfig.validate_base_url(value)

    @classmethod
    def from_config(cls, config: AppConfig) -> Self:
        return cls(
            provider=config.provider, model=config.model, base_url=config.base_url,
            request_timeout_seconds=config.request_timeout_seconds,
        )


class TaskInput(ContractModel):
    source_path: Path
    model: ModelInfo
    spec_path: Path | None = None
    output_dir: Path = Field(default="outputs")
    max_rounds: RoundNumber = 3
    execution_timeout_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 30


class FunctionInfo(ContractModel):
    name: Identifier
    signature: Text
    start_line: Line
    end_line: Line
    docstring: str | None = None

    @model_validator(mode="after")
    def ordered_lines(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("function end_line precedes start_line")
        return self


class ImportInfo(ContractModel):
    statement: Text
    line: Line
    module: str | None = None
    names: tuple[Text, ...] = ()


class SourceBranch(ContractModel):
    kind: Literal["if", "match", "loop", "try", "raise"]
    line: Line
    source: Text


class CallSite(ContractModel):
    """AST-observed call expression; classification is stored separately."""

    call_id: Text
    caller: Identifier
    expression: Text
    line: Line
    source: Text
    argument_expressions: tuple[Text, ...] = ()


class DirectCall(ContractModel):
    """Tool-confirmed same-file name binding for one observed call site."""

    call_site_id: Text
    caller: Identifier
    callee: Identifier


class UnresolvedCall(ContractModel):
    """Observed call that the static analyzer cannot safely bind."""

    call_site_id: Text
    caller: Identifier
    reason: Text


class SourceInfo(ContractModel):
    source_path: Path
    source_code: Text
    functions: Annotated[tuple[FunctionInfo, ...], Field(min_length=1, max_length=5)]
    encoding: Text = "utf-8"
    imports: tuple[ImportInfo, ...] = ()
    branches: tuple[SourceBranch, ...] = ()
    call_sites: tuple[CallSite, ...] = ()
    direct_calls: tuple[DirectCall, ...] = ()
    unresolved_calls: tuple[UnresolvedCall, ...] = ()

    @model_validator(mode="after")
    def validate_locations(self) -> Self:
        names = [function.name for function in self.functions]
        if len(names) != len(set(names)):
            raise ValueError("duplicate target function names are unsupported")
        last_line = len(self.source_code.splitlines())
        if any(function.end_line > last_line for function in self.functions):
            raise ValueError("function location is outside the source")
        if any(item.line > last_line for item in (*self.imports, *self.branches, *self.call_sites)):
            raise ValueError("import/branch/call location is outside the source")
        function_names = set(names)
        call_sites = {item.call_id: item for item in self.call_sites}
        if len(call_sites) != len(self.call_sites):
            raise ValueError("duplicate call-site IDs")
        if any(item.caller not in function_names for item in self.call_sites):
            raise ValueError("call-site caller is not a source function")
        direct_ids = [item.call_site_id for item in self.direct_calls]
        unresolved_ids = [item.call_site_id for item in self.unresolved_calls]
        if len(set(direct_ids)) != len(direct_ids) or len(set(unresolved_ids)) != len(unresolved_ids):
            raise ValueError("duplicate classified call-site references")
        if set(direct_ids) & set(unresolved_ids):
            raise ValueError("a call site cannot be both confirmed and unresolved")
        for item in self.direct_calls:
            call = call_sites.get(item.call_site_id)
            if call is None or call.caller != item.caller:
                raise ValueError("direct call must reference its matching observed call site")
            if item.caller not in function_names or item.callee not in function_names:
                raise ValueError("direct call functions must belong to the source")
        for item in self.unresolved_calls:
            call = call_sites.get(item.call_site_id)
            if call is None or call.caller != item.caller:
                raise ValueError("unresolved call must reference its matching observed call site")
        return self


class ExpectedBasis(ContractModel):
    kind: Literal["specification", "docstring", "implementation_assumption"]
    detail: Text


class ScenarioInput(ContractModel):
    args: tuple[JsonValue, ...] = ()
    kwargs: dict[Identifier, JsonValue] = Field(default_factory=dict)


class RelationshipEvidence(ContractModel):
    kind: Literal["source", "specification", "docstring"]
    detail: Text
    line: Line | None = None
    call_site_ids: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        if len(set(self.call_site_ids)) != len(self.call_site_ids):
            raise ValueError("duplicate call-site evidence references")
        if self.kind == "source" and self.line is None and not self.call_site_ids:
            raise ValueError("source relationship evidence needs a line or call-site reference")
        return self


class RelationshipAnalysis(ContractModel):
    relation_id: Text
    kind: Literal["direct_call", "data_flow", "shared_state", "unknown"]
    involved_functions: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=5)]
    evidence: Annotated[tuple[RelationshipEvidence, ...], Field(min_length=1)]
    confidence: Literal["confirmed", "candidate", "unknown"]
    unresolved_questions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def consistent_relationship(self) -> Self:
        if len(set(self.involved_functions)) != len(self.involved_functions):
            raise ValueError("duplicate functions in a relationship")
        if self.kind != "unknown" and len(self.involved_functions) < 2:
            raise ValueError("a concrete relationship needs at least two functions")
        if self.kind == "unknown" and self.confidence != "unknown":
            raise ValueError("unknown relationship kinds cannot claim confidence")
        if self.confidence == "unknown" and not self.unresolved_questions:
            raise ValueError("unknown relationships need an unresolved question")
        if self.confidence == "confirmed" and self.kind == "unknown":
            raise ValueError("unknown relationships cannot be confirmed")
        return self


class StrategyDecision(ContractModel):
    subject_kind: Literal["function", "relationship"]
    function_name: Identifier | None = None
    relationship_id: Text | None = None
    decision: Literal["unit_test", "collaboration_test", "not_selected"]
    reason: Text

    @model_validator(mode="after")
    def consistent_subject(self) -> Self:
        if self.subject_kind == "function":
            if self.function_name is None or self.relationship_id is not None:
                raise ValueError("function decisions need only function_name")
            if self.decision == "collaboration_test":
                raise ValueError("function decisions cannot directly select collaboration tests")
        elif self.relationship_id is None or self.function_name is not None:
            raise ValueError("relationship decisions need only relationship_id")
        elif self.decision == "unit_test":
            raise ValueError("relationship decisions cannot select unit tests")
        return self


class StepValue(ContractModel):
    kind: Literal["literal", "step_result"]
    value: JsonValue | None = None
    step_id: Text | None = None

    @model_validator(mode="after")
    def consistent_value(self) -> Self:
        if self.kind == "literal" and self.step_id is not None:
            raise ValueError("literal step values cannot reference a prior step")
        if self.kind == "step_result" and (self.step_id is None or self.value is not None):
            raise ValueError("step-result values need only a prior step ID")
        return self


class ScenarioStep(ContractModel):
    step_id: Text
    function: Identifier
    args: tuple[StepValue, ...] = ()
    kwargs: dict[Identifier, StepValue] = Field(default_factory=dict)


class TestScenario(ContractModel):
    scenario_id: Text
    target_function: Identifier
    category: Literal["normal", "boundary", "exception", "branch"]
    inputs: ScenarioInput
    expected_behavior: Text
    expected_basis: ExpectedBasis
    test_kind: Literal["unit", "collaboration"] = "unit"
    involved_functions: tuple[Identifier, ...] = ()
    relationship_ids: tuple[Text, ...] = ()
    steps: tuple[ScenarioStep, ...] = ()

    @model_validator(mode="after")
    def ordered_steps(self) -> Self:
        step_ids = [step.step_id for step in self.steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("duplicate scenario step IDs")
        seen: set[str] = set()
        for step in self.steps:
            references = [item.step_id for item in (*step.args, *step.kwargs.values()) if item.kind == "step_result"]
            if any(reference not in seen for reference in references):
                raise ValueError("scenario steps may reference only earlier step results")
            seen.add(step.step_id)
        return self


class TestPlan(ContractModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    target_functions: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=5)]
    oracle_mode: OracleMode
    entry_functions: tuple[Identifier, ...] = ()
    relationships: tuple[RelationshipAnalysis, ...] = ()
    strategy: tuple[StrategyDecision, ...] = ()
    scenarios: tuple[TestScenario, ...] = ()

    @model_validator(mode="after")
    def consistent_scenarios(self) -> Self:
        if len(set(self.target_functions)) != len(self.target_functions):
            raise ValueError("duplicate plan target functions")
        ids = [scenario.scenario_id for scenario in self.scenarios]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate scenario IDs")
        if any(s.target_function not in self.target_functions for s in self.scenarios):
            raise ValueError("scenario target is missing from the plan")
        if self.oracle_mode == OracleMode.CHARACTERIZATION and any(
            s.expected_basis.kind != "implementation_assumption" for s in self.scenarios
        ):
            raise ValueError("characterization scenarios must label implementation assumptions")
        relationship_ids = [item.relation_id for item in self.relationships]
        if len(set(relationship_ids)) != len(relationship_ids):
            raise ValueError("duplicate relationship IDs")
        target_set = set(self.target_functions)
        if any(not set(item.involved_functions).issubset(target_set) for item in self.relationships):
            raise ValueError("relationship functions must belong to plan targets")
        if len(set(self.entry_functions)) != len(self.entry_functions) or not set(self.entry_functions).issubset(target_set):
            raise ValueError("entry functions must be unique plan targets")
        if self.schema_version == "1.0":
            if self.entry_functions or self.relationships or self.strategy or any(
                scenario.involved_functions or scenario.relationship_ids or scenario.steps
                or scenario.test_kind != "unit" for scenario in self.scenarios
            ):
                raise ValueError("relationship fields require plan schema version 1.1")
            return self
        if not self.entry_functions:
            raise ValueError("schema 1.1 plans need at least one entry function")
        function_decisions = [item.function_name for item in self.strategy if item.subject_kind == "function"]
        relationship_decisions = [item.relationship_id for item in self.strategy if item.subject_kind == "relationship"]
        if len(set(function_decisions)) != len(function_decisions) or set(function_decisions) != target_set:
            raise ValueError("schema 1.1 needs exactly one strategy decision per target function")
        if len(set(relationship_decisions)) != len(relationship_decisions) or set(relationship_decisions) != set(relationship_ids):
            raise ValueError("schema 1.1 needs exactly one strategy decision per relationship")
        relationship_map = {item.relation_id: item for item in self.relationships}
        function_strategy = {
            item.function_name: item.decision
            for item in self.strategy if item.subject_kind == "function"
        }
        relationship_strategy = {
            item.relationship_id: item.decision
            for item in self.strategy if item.subject_kind == "relationship"
        }
        if any(
            relationship_map[relation_id].confidence == "unknown" and decision == "collaboration_test"
            for relation_id, decision in relationship_strategy.items()
        ):
            raise ValueError("unknown relationships cannot be selected for collaboration tests")
        for scenario in self.scenarios:
            involved = set(scenario.involved_functions)
            if not involved or scenario.target_function not in involved or not involved.issubset(target_set):
                raise ValueError("scenario involved functions must include its target and belong to the plan")
            if len(involved) != len(scenario.involved_functions):
                raise ValueError("duplicate involved functions in a scenario")
            if not set(scenario.relationship_ids).issubset(relationship_map):
                raise ValueError("scenario references an unknown relationship")
            if scenario.test_kind == "unit":
                if involved != {scenario.target_function} or scenario.relationship_ids or scenario.steps:
                    raise ValueError("unit scenarios cover one function without relationships or steps")
                if function_strategy[scenario.target_function] != "unit_test":
                    raise ValueError("unit scenarios require a matching unit-test strategy decision")
            else:
                if len(involved) < 2 or not scenario.relationship_ids:
                    raise ValueError("collaboration scenarios need multiple functions and relationships")
                if any(relationship_map[item].confidence == "unknown" for item in scenario.relationship_ids):
                    raise ValueError("unknown relationships cannot justify collaboration scenarios")
                if any(
                    not set(relationship_map[item].involved_functions).issubset(involved)
                    for item in scenario.relationship_ids
                ):
                    raise ValueError("collaboration scenarios must include all functions in referenced relationships")
                mismatched = [
                    item for item in scenario.relationship_ids
                    if relationship_strategy[item] != "collaboration_test"
                ]
                if mismatched:
                    raise ValueError(
                        f"collaboration scenario {scenario.scenario_id!r} references relationships "
                        f"without collaboration_test strategy decisions: {mismatched!r}"
                    )
                if scenario.steps:
                    if len(scenario.steps) < 2 or any(step.function not in involved for step in scenario.steps):
                        raise ValueError("collaboration steps must call involved functions in a multi-step sequence")
        return self


class TestCaseMapping(ContractModel):
    test_function: TestName
    scenario_ids: Annotated[tuple[Text, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_scenarios(self) -> Self:
        if len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError("duplicate scenario IDs in a mapping")
        return self


class GeneratedTests(ContractModel):
    """The only LLM generation payload: no paths, execution counts or coverage."""

    plan: TestPlan
    code: Text | None
    mappings: tuple[TestCaseMapping, ...] = ()
    assumptions: tuple[Text, ...] = ()
    questions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def map_generated_tests(self) -> Self:
        if self.code is None:
            if self.mappings or not self.questions:
                raise ValueError("no-code responses need questions and cannot map test functions")
            return self
        if not self.plan.scenarios:
            raise ValueError("generated code needs a nonempty test plan")
        names = [mapping.test_function for mapping in self.mappings]
        if len(set(names)) != len(names):
            raise ValueError("duplicate mapped test functions")
        mapped = {sid for mapping in self.mappings for sid in mapping.scenario_ids}
        if mapped != {scenario.scenario_id for scenario in self.plan.scenarios}:
            raise ValueError("mappings must cover exactly the plan scenario IDs")
        return self


class ExecutionKind(StrEnum):
    PASSED = "passed"
    ASSERTION_FAILED = "assertion_failed"
    EXECUTION_ERROR = "execution_error"
    COLLECTION_ERROR = "collection_error"
    INTERNAL_ERROR = "internal_error"
    USAGE_ERROR = "usage_error"
    INTERRUPTED = "interrupted"
    NO_TESTS = "no_tests"
    TIMEOUT = "timeout"
    LAUNCH_ERROR = "launch_error"
    RESULT_ERROR = "result_error"


class FailureObservation(ContractModel):
    """Structured pytest exception facts, never supplied by generation output."""

    nodeid: Text
    phase: Literal["collection", "setup", "call", "teardown"]
    exception_type: Text
    path: str | None = None
    line: Line | None = None
    missing_module: Text | None = None


class ExecutionResult(ContractModel):
    """Tool observations. Null counters mean unknown, not an observed zero."""

    kind: ExecutionKind
    exit_code: StrictInt | None
    collected: Count | None = None
    executed: Count | None = None
    passed: Count | None = None
    failed: Count | None = None
    errors: Count | None = None
    skipped: Count | None = None
    xfailed: Count | None = None
    xpassed: Count | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: StrictBool = False
    stdout_truncated: StrictBool = False
    stderr_truncated: StrictBool = False
    duration_seconds: Seconds | None = None
    failures: tuple[FailureObservation, ...] = ()

    @model_validator(mode="after")
    def consistent_execution(self) -> Self:
        expected_codes = {
            ExecutionKind.PASSED: 0, ExecutionKind.ASSERTION_FAILED: 1,
            ExecutionKind.EXECUTION_ERROR: 1, ExecutionKind.COLLECTION_ERROR: 2,
            ExecutionKind.INTERRUPTED: 2, ExecutionKind.INTERNAL_ERROR: 3,
            ExecutionKind.USAGE_ERROR: 4, ExecutionKind.NO_TESTS: 5,
        }
        if self.kind in expected_codes and self.exit_code != expected_codes[self.kind]:
            raise ValueError("execution kind contradicts the pytest exit code")
        if self.timed_out != (self.kind == ExecutionKind.TIMEOUT):
            raise ValueError("timeout kind and timed_out flag must agree")
        if self.kind == ExecutionKind.LAUNCH_ERROR and self.exit_code is not None:
            raise ValueError("an unstarted process cannot have an exit code")
        if self.kind == ExecutionKind.RESULT_ERROR and self.exit_code is None:
            raise ValueError("a result-error run must preserve the observed exit code")
        if self.failures and self.kind in {ExecutionKind.PASSED, ExecutionKind.NO_TESTS, ExecutionKind.LAUNCH_ERROR}:
            raise ValueError("successful/empty/unstarted runs cannot contain failure observations")
        if self.collected is not None and self.executed is not None and self.executed > self.collected:
            raise ValueError("executed items exceed collected items")
        if self.kind in {ExecutionKind.NO_TESTS, ExecutionKind.LAUNCH_ERROR} and any(
            count not in (0, None) for count in (
                self.collected, self.executed, self.passed, self.failed, self.errors,
                self.skipped, self.xfailed, self.xpassed,
            )
        ):
            raise ValueError("no-tests/unstarted execution cannot report test items")
        if self.kind in {ExecutionKind.PASSED, ExecutionKind.ASSERTION_FAILED, ExecutionKind.EXECUTION_ERROR}:
            outcomes = (self.passed, self.failed, self.errors, self.skipped, self.xfailed, self.xpassed)
            if self.collected is None or self.executed is None or any(value is None for value in outcomes):
                raise ValueError("finished test runs need known item counts")
            if sum(outcomes) != self.collected:
                raise ValueError("finished test outcomes must partition collected items")
            if self.collected == 0:
                raise ValueError("a finished test run must contain collected items")
            if self.executed < self.passed + self.failed + self.xpassed:
                raise ValueError("passing/failing test calls exceed executed items")
            if self.kind == ExecutionKind.PASSED and (self.failed or self.errors):
                raise ValueError("passed execution cannot contain failures or errors")
            if self.kind == ExecutionKind.ASSERTION_FAILED and (not self.failed or self.errors):
                raise ValueError("assertion failure needs failed tests and no framework errors")
            if self.kind == ExecutionKind.EXECUTION_ERROR and not self.errors:
                raise ValueError("execution-error run needs errors")
        return self

    @computed_field
    @property
    def runnable(self) -> bool:
        return (
            self.kind in {ExecutionKind.PASSED, ExecutionKind.ASSERTION_FAILED}
            and self.executed is not None and self.executed > 0 and not self.timed_out
            and self.errors == 0
            and (self.passed or 0) + (self.failed or 0) > 0
        )

    @computed_field
    @property
    def all_passed(self) -> bool:
        return (
            self.runnable and self.failed == 0 and self.passed == self.collected
            and self.executed == self.passed
            and self.skipped == self.xfailed == self.xpassed == 0
        )


class BranchArc(ContractModel):
    """Coverage arcs may use negative line numbers for code-object exits."""

    start: StrictInt
    end: StrictInt

    @field_validator("start", "end")
    @classmethod
    def nonzero_line(cls, value: int) -> int:
        if value == 0:
            raise ValueError("coverage arc line numbers cannot be zero")
        return value


class CoverageResult(ContractModel):
    target_file: Path
    status: Literal["available", "unavailable"]
    covered_lines: Count | None = None
    total_lines: Count | None = None
    covered_branches: Count | None = None
    total_branches: Count | None = None
    missing_lines: tuple[Line, ...] = ()
    missing_branches: tuple[BranchArc, ...] = ()
    error: Text | None = None

    @model_validator(mode="after")
    def consistent_coverage(self) -> Self:
        counts = (self.covered_lines, self.total_lines, self.covered_branches, self.total_branches)
        if self.status == "unavailable":
            if any(value is not None for value in counts) or self.missing_lines or self.missing_branches or not self.error:
                raise ValueError("unavailable coverage needs a reason and cannot invent metrics")
            return self
        if any(value is None for value in counts) or self.error is not None:
            raise ValueError("available coverage needs known counters and no collection error")
        if self.covered_lines > self.total_lines or self.covered_branches > self.total_branches:
            raise ValueError("covered counts exceed totals")
        if len(set(self.missing_lines)) != len(self.missing_lines):
            raise ValueError("duplicate missing lines")
        if len({(arc.start, arc.end) for arc in self.missing_branches}) != len(self.missing_branches):
            raise ValueError("duplicate missing branches")
        if len(self.missing_lines) != self.total_lines - self.covered_lines:
            raise ValueError("missing line count contradicts coverage counters")
        if len(self.missing_branches) != self.total_branches - self.covered_branches:
            raise ValueError("missing branch count contradicts coverage counters")
        return self

    @computed_field
    @property
    def line_percent(self) -> float | None:
        return 100 * self.covered_lines / self.total_lines if self.total_lines else None

    @computed_field
    @property
    def branch_percent(self) -> float | None:
        return 100 * self.covered_branches / self.total_branches if self.total_branches else None

    @computed_field
    @property
    def meets_target(self) -> bool:
        return (
            self.status == "available" and self.line_percent is not None and self.line_percent >= 90
            and (self.total_branches == 0 or self.branch_percent >= 80)
        )


class Issue(ContractModel):
    kind: Literal[
        "suspected_source_defect", "ambiguous_specification", "generation_error",
        "environment_error", "coverage_unavailable", "other",
    ]
    summary: Text
    evidence: tuple[Text, ...] = ()
    round_number: RoundNumber | None = None
    scenario_id: Text | None = None

    @property
    def requires_review(self) -> bool:
        return self.kind in {"suspected_source_defect", "ambiguous_specification"}


class CandidateRound(ContractModel):
    number: RoundNumber
    generated: GeneratedTests | None = None
    test_path: Path | None = None
    validation_error: Text | None = None
    execution: ExecutionResult | None = None
    coverage: CoverageResult | None = None

    @model_validator(mode="after")
    def tool_results_require_candidate(self) -> Self:
        if self.execution is not None and (
            self.generated is None or self.generated.code is None or self.validation_error is not None
        ):
            raise ValueError("execution requires a generated candidate without a validation error")
        if self.coverage is not None and self.execution is None:
            raise ValueError("coverage cannot precede execution")
        return self

    @property
    def available(self) -> bool:
        return self.execution is not None and self.execution.runnable


class TokenUsage(ContractModel):
    prompt_tokens: Count | None = None
    completion_tokens: Count | None = None
    total_tokens: Count | None = None

    @model_validator(mode="after")
    def consistent_total(self) -> Self:
        if all(value is not None for value in (self.prompt_tokens, self.completion_tokens, self.total_tokens)):
            if self.total_tokens != self.prompt_tokens + self.completion_tokens:
                raise ValueError("token total contradicts measured components")
        return self


def _history(rounds: tuple[CandidateRound, ...], request: TaskInput, oracle_mode: OracleMode) -> None:
    if len(rounds) > request.max_rounds or [item.number for item in rounds] != list(range(1, len(rounds) + 1)):
        raise ValueError("round history must be consecutive and within the configured budget")
    if any(item.generated and item.validation_error is None and item.generated.plan.oracle_mode != oracle_mode for item in rounds):
        raise ValueError("candidate oracle mode differs from the original task")


def _selected(rounds: tuple[CandidateRound, ...], number: int | None) -> CandidateRound | None:
    if number is None:
        return None
    if number > len(rounds) or not rounds[number - 1].available:
        raise ValueError("selected round must reference an available historical candidate")
    return rounds[number - 1]


def _issues(issues: tuple[Issue, ...], rounds: tuple[CandidateRound, ...]) -> None:
    for issue in issues:
        if issue.round_number is not None:
            if issue.round_number > len(rounds):
                raise ValueError("issue references a nonexistent round")
            candidate = rounds[issue.round_number - 1]
            if issue.scenario_id is not None and (
                candidate.generated is None or issue.scenario_id not in {
                    scenario.scenario_id for scenario in candidate.generated.plan.scenarios
                }
            ):
                raise ValueError("issue references a nonexistent scenario")


def validate_plan_against_source(plan: TestPlan, source: SourceInfo) -> None:
    """Cross-check model-produced relationship references against AST tool facts."""
    function_names = {function.name for function in source.functions}
    if not set(plan.target_functions).issubset(function_names):
        raise ValueError("generated plan must target analyzed source functions")
    call_site_ids = {item.call_id for item in source.call_sites}
    direct_by_id = {item.call_site_id: item for item in source.direct_calls}
    unresolved_ids = {item.call_site_id for item in source.unresolved_calls}
    for relationship in plan.relationships:
        evidence_ids = {
            call_site_id
            for evidence in relationship.evidence
            for call_site_id in evidence.call_site_ids
        }
        if not evidence_ids.issubset(call_site_ids):
            raise ValueError("relationship evidence references an unknown source call site")
        if any(
            evidence.kind == "source" and evidence.line is not None
            and evidence.line > len(source.source_code.splitlines())
            for evidence in relationship.evidence
        ):
            raise ValueError("relationship source evidence line is outside the source")
        if relationship.confidence == "confirmed" and evidence_ids & unresolved_ids:
            raise ValueError("unresolved source calls cannot support confirmed relationships")
        if relationship.kind == "direct_call" and relationship.confidence == "confirmed":
            expected = relationship.involved_functions
            if not any(
                call_site_id in direct_by_id
                and (direct_by_id[call_site_id].caller, direct_by_id[call_site_id].callee) == expected
                for call_site_id in evidence_ids
            ):
                raise ValueError("confirmed direct relationships need matching tool-confirmed call evidence")


class AgentState(ContractModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    run_id: Text
    request: TaskInput
    oracle_mode: OracleMode
    behavior_specification: str | None = None
    source_info: SourceInfo | None = None
    current_round: Annotated[StrictInt, Field(ge=0, le=3)] = 0
    history: tuple[CandidateRound, ...] = ()
    best_available_round: RoundNumber | None = None
    next_action: AgentAction | None = None
    stop_reason: StopReason | None = None
    issues: tuple[Issue, ...] = ()
    api_calls: Count = 0
    token_usage: TokenUsage | None = None

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        _history(self.history, self.request, self.oracle_mode)
        _issues(self.issues, self.history)
        if self.current_round != len(self.history):
            raise ValueError("current round must agree with the saved history")
        _selected(self.history, self.best_available_round)
        if self.source_info is not None and self.source_info.source_path != self.request.source_path:
            raise ValueError("source metadata belongs to a different input")
        for item in self.history:
            if item.generated is not None and item.validation_error is None:
                if item.generated.plan.schema_version != self.schema_version:
                    raise ValueError("state and generated plan schema versions must agree")
                if self.source_info is None:
                    raise ValueError("generated plans require analyzed source metadata")
                validate_plan_against_source(item.generated.plan, self.source_info)
        if self.stop_reason is not None and self.next_action not in {AgentAction.FINISH, AgentAction.REQUEST_REVIEW}:
            raise ValueError("a stopped task cannot request more generation or execution")
        return self


class ReportMetrics(ContractModel):
    runnable: StrictBool
    all_passed: StrictBool
    line_percent: float | None
    branch_percent: float | None
    executed_rounds: Count
    api_calls: Count


class FinalReport(ContractModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    run_id: Text
    request: TaskInput
    oracle_mode: OracleMode
    status: TaskStatus
    stop_reason: StopReason
    source_info: SourceInfo | None = None
    rounds: tuple[CandidateRound, ...] = ()
    selected_round: RoundNumber | None = None
    final_test_file: Path | None = None
    trace_file: Path | None = None
    unresolved_issues: tuple[Issue, ...] = ()
    api_calls: Count = 0
    token_usage: TokenUsage | None = None

    @model_validator(mode="after")
    def consistent_report(self) -> Self:
        _history(self.rounds, self.request, self.oracle_mode)
        _issues(self.unresolved_issues, self.rounds)
        if self.source_info is not None and self.source_info.source_path != self.request.source_path:
            raise ValueError("report source metadata belongs to a different input")
        for item in self.rounds:
            if item.generated is not None and item.validation_error is None:
                if item.generated.plan.schema_version != self.schema_version:
                    raise ValueError("report and generated plan schema versions must agree")
                if self.schema_version == "1.1":
                    if self.source_info is None:
                        raise ValueError("schema 1.1 reports with generated plans need source metadata")
                    validate_plan_against_source(item.generated.plan, self.source_info)
        candidate = _selected(self.rounds, self.selected_round)
        if (candidate is None) != (self.final_test_file is None):
            raise ValueError("final test file and selected candidate must both exist or both be null")
        review = any(issue.requires_review for issue in self.unresolved_issues) or bool(
            candidate and candidate.generated.questions
        )
        if review != (self.status == TaskStatus.NEEDS_REVIEW):
            raise ValueError("review evidence and task status must agree")
        if self.status == TaskStatus.FAILED and any(item.available for item in self.rounds):
            raise ValueError("an available candidate cannot be reported as total failure")
        if self.status in {TaskStatus.COMPLETED, TaskStatus.PARTIAL} and candidate is None:
            raise ValueError("completed/partial report requires an available candidate")
        if self.status == TaskStatus.COMPLETED and (
            not candidate.execution.all_passed or candidate.coverage is None
            or not candidate.coverage.meets_target or self.unresolved_issues or self.trace_file is None
            or self.stop_reason != StopReason.COVERAGE_TARGET_MET
        ):
            raise ValueError("completion requires passing tests, coverage, trace and no unresolved issues")
        return self

    @computed_field
    @property
    def metrics(self) -> ReportMetrics:
        candidate = _selected(self.rounds, self.selected_round)
        execution = candidate.execution if candidate else None
        coverage = candidate.coverage if candidate else None
        return ReportMetrics(
            runnable=bool(execution and execution.runnable),
            all_passed=bool(execution and execution.all_passed),
            line_percent=coverage.line_percent if coverage else None,
            branch_percent=coverage.branch_percent if coverage else None,
            executed_rounds=sum(item.execution is not None for item in self.rounds),
            api_calls=self.api_calls,
        )
