"""Conservative failure attribution using observed exceptions and explicit oracles.

Only literal single-argument equality assertions grounded by an unambiguous
two-column specification table or an exact oracle quote are automatically
supported. Exception oracles, workflows and uncertain provenance require
review rather than guesses.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .models import AgentState, CandidateRound, FailureObservation, Issue, OracleMode


def _table_values(text: str, argument: object) -> list[tuple[object, str]]:
    values = []
    in_table = False
    for line in text.splitlines():
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if not line.strip().startswith("|") or len(cells) != 2:
            in_table = False
            continue
        if cells[0].casefold() in {"输入", "input"} and cells[1].casefold() in {"预期", "预期结果", "expected"}:
            in_table = True
            continue
        if not in_table:
            continue
        try:
            inputs = ast.literal_eval(cells[0].strip("`*"))
            expected = ast.literal_eval(cells[1].strip("`*"))
        except (SyntaxError, ValueError):
            continue
        alternatives = inputs if isinstance(inputs, tuple) else (inputs,)
        if any(type(value) is type(argument) and value == argument for value in alternatives):
            values.append((expected, line.strip()))
    return values


def _supported_assertion(state: AgentState, candidate: CandidateRound, failure: FailureObservation) -> tuple[str, str] | None:
    generated, source = candidate.generated, state.source_info
    if generated is None or source is None or generated.code is None:
        return None
    if state.oracle_mode != OracleMode.SPECIFICATION or len(source.functions) != 1:
        return None
    if failure.phase != "call" or failure.exception_type != "AssertionError" or failure.line is None:
        return None
    if failure.path is None or Path(failure.path).name != "test_subject.py":
        return None
    # Parameterization and multi-scenario mappings are deliberately conservative.
    name = failure.nodeid.split("::")[-1]
    if "[" in name:
        return None
    mappings = [item for item in generated.mappings if item.test_function == name]
    if len(mappings) != 1 or len(mappings[0].scenario_ids) != 1:
        return None
    scenario = next(item for item in generated.plan.scenarios if item.scenario_id == mappings[0].scenario_ids[0])
    if scenario.expected_basis.kind not in {"specification", "docstring"}:
        return None
    try:
        tree = ast.parse(generated.code)
    except SyntaxError:
        return None
    function = next((item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == name), None)
    if function is None or len(function.body) != 1 or not isinstance(function.body[0], ast.Assert):
        return None
    assertions = [item for item in ast.walk(function) if isinstance(item, ast.Assert) and item.lineno == failure.line]
    if len(assertions) != 1:
        return None
    expr = assertions[0].test
    if not (isinstance(expr, ast.Compare) and len(expr.ops) == 1 and isinstance(expr.ops[0], ast.Eq)
            and len(expr.comparators) == 1 and isinstance(expr.left, ast.Call)):
        return None
    call = expr.left
    if not (isinstance(call.func, ast.Name) and call.func.id == scenario.target_function
            and len(call.args) == 1 and not call.keywords):
        return None
    try:
        argument, expected = ast.literal_eval(call.args[0]), ast.literal_eval(expr.comparators[0])
    except (ValueError, SyntaxError):
        return None
    if scenario.inputs.kwargs or len(scenario.inputs.args) != 1:
        return None
    if type(scenario.inputs.args[0]) is not type(argument) or scenario.inputs.args[0] != argument:
        return None
    specification = state.behavior_specification or ""
    if specification.strip():
        oracle = specification if scenario.expected_basis.kind == "specification" else ""
    else:
        oracle = source.functions[0].docstring or ""
    tables = _table_values(oracle, argument)
    if tables:
        if any(type(value) is not type(expected) or value != expected for value, _ in tables):
            return None
        return scenario.scenario_id, tables[0][1]
    quote = scenario.expected_basis.detail.strip()
    if not quote or quote not in oracle:
        return None
    return scenario.scenario_id, quote


def classify_failures(state: AgentState, candidate: CandidateRound) -> tuple[Issue, ...]:
    """Return evidence-bearing issues; missing facts default to review."""
    execution = candidate.execution
    if execution is None:
        return ()
    issues = []
    for failure in execution.failures:
        evidence = (f"{failure.nodeid}: {failure.phase}, {failure.exception_type}, {failure.path}:{failure.line}",)
        origin = Path(failure.path).name if failure.path else None
        if failure.exception_type == "ModuleNotFoundError" and failure.missing_module:
            # A nonexistent 'subject' import is a generated layout/import issue.
            kind = "generation_error" if failure.missing_module == "subject" and origin == "test_subject.py" else "environment_error"
            summary = f"缺少模块 {failure.missing_module}；不自动安装依赖。"
        elif failure.exception_type in {"ImportError", "SyntaxError", "NameError"} and origin == "test_subject.py":
            kind, summary = "generation_error", "生成测试存在语法、名称或导入错误。"
        else:
            supported = _supported_assertion(state, candidate, failure)
            if supported is not None:
                scenario_id, quote = supported
                issues.append(Issue(kind="suspected_source_defect", summary="真实失败断言与明确规格证据一致，疑似源码缺陷；保留预期和用例。",
                                    evidence=(*evidence, f"规格原文：{quote}"),
                                    round_number=candidate.number, scenario_id=scenario_id))
                continue
            kind, summary = "ambiguous_specification", "失败责任或预期依据不能可靠确认，需要人工检查规格与测试。"
        issues.append(Issue(kind=kind, summary=summary, evidence=evidence, round_number=candidate.number))
    if not issues and execution.kind.value in {"assertion_failed", "execution_error"}:
        issues.append(Issue(kind="ambiguous_specification", summary="缺少结构化失败位置，不能仅凭失败计数判定源码缺陷。",
                            round_number=candidate.number))
    return tuple(issues)


def repeated_without_progress(state: AgentState) -> bool:
    """A repeated candidate and identical measured outcome is not another retry."""
    if len(state.history) < 2:
        return False
    previous, current = state.history[-2:]
    if previous.generated is None or current.generated is None:
        return False
    if previous.generated.code != current.generated.code:
        return False
    if previous.validation_error or current.validation_error:
        return previous.validation_error == current.validation_error
    if previous.execution is None or current.execution is None:
        return False
    def signature(candidate):
        execution, coverage = candidate.execution, candidate.coverage
        return (
            execution.kind, execution.collected, execution.executed, execution.passed,
            execution.failed, execution.errors, execution.skipped, execution.xfailed, execution.xpassed,
            tuple((failure.nodeid.split("::")[-1], failure.phase, failure.exception_type,
                   Path(failure.path).name if failure.path else None, failure.line, failure.missing_module)
                  for failure in execution.failures),
            None if coverage is None else (
                coverage.status, coverage.covered_lines, coverage.total_lines,
                coverage.covered_branches, coverage.total_branches,
                coverage.missing_lines, coverage.missing_branches,
            ),
        )
    return signature(previous) == signature(current)
