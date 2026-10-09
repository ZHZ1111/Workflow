"""Candidate generation, host validation and deterministic staging."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Protocol

from .llm_client import GenerationContext, LLMGenerationResult
from .models import CandidateRound, GeneratedTests, OracleMode, SourceInfo, validate_plan_against_source
from .prompts import build_generation_context
from .workspace import ROUND_DIRECTORY, SUBJECT_FILENAME, TEST_FILENAME, round_directory


class CandidateValidationError(ValueError):
    """A generated candidate violates a host-owned validation rule."""

    def __init__(self, code: str, message: str, *, line: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.line = line

    @property
    def summary(self) -> str:
        location = f"（第 {self.line} 行）" if self.line is not None else ""
        return f"{self.code}{location}：{self}"


class GenerationClient(Protocol):
    """The generation method required from an LLM adapter."""

    def generate_tests(self, context: GenerationContext) -> LLMGenerationResult: ...


@dataclass(frozen=True, slots=True)
class InitialCandidateArtifacts:
    """Host paths plus the validated/safely rejected first-round snapshot."""

    workspace: Path
    subject_path: Path
    plan_path: Path
    candidate_path: Path
    llm_result_path: Path
    candidate: CandidateRound
    llm_result: LLMGenerationResult

    @property
    def test_path(self) -> Path | None:
        if self.candidate.test_path is None:
            return None
        return self.workspace / self.candidate.test_path

    @property
    def ready_for_execution(self) -> bool:
        return (
            self.candidate.generated is not None
            and self.candidate.generated.code is not None
            and self.candidate.validation_error is None
            and self.test_path is not None
        )


def generate_initial_candidate(
    *,
    source: SourceInfo,
    behavior_specification: str | None,
    oracle_mode: OracleMode,
    workspace: str | Path,
    client: GenerationClient,
    prepared_run_id: str | None = None,
) -> InitialCandidateArtifacts:
    """Request one structured plan/code object, then validate and stage it once."""

    if not isinstance(source, SourceInfo):
        raise TypeError("source must be a SourceInfo")
    if not isinstance(oracle_mode, OracleMode):
        raise TypeError("oracle_mode must be an OracleMode")
    if not callable(getattr(client, "generate_tests", None)):
        raise TypeError("client must provide generate_tests(context)")
    context = build_generation_context(source, behavior_specification, oracle_mode)
    result = client.generate_tests(context)
    if not isinstance(result, LLMGenerationResult):
        raise TypeError("generate_tests must return an LLMGenerationResult")
    return stage_initial_candidate(source=source, llm_result=result, workspace=workspace,
                                   expected_oracle_mode=oracle_mode, prepared_run_id=prepared_run_id)


def stage_initial_candidate(
    *,
    source: SourceInfo,
    llm_result: LLMGenerationResult,
    workspace: str | Path,
    expected_oracle_mode: OracleMode | None = None,
    prepared_run_id: str | None = None,
) -> InitialCandidateArtifacts:
    """Create fixed host paths and save a valid or safely rejected round-one candidate."""

    if not isinstance(source, SourceInfo):
        raise TypeError("source must be a SourceInfo")
    if not isinstance(llm_result, LLMGenerationResult):
        raise TypeError("llm_result must be an LLMGenerationResult")
    root = Path(workspace)
    try:
        if prepared_run_id is None:
            root.mkdir(parents=True, exist_ok=False)
        else:
            import json
            if root.is_symlink() or root.is_junction() or not root.is_dir():
                raise ValueError("Invalid prepared run workspace")
            if {item.name for item in root.iterdir()} != {"run.json", "trace.jsonl"}:
                raise FileExistsError("Prepared workspace contains candidate or unrelated artifacts")
            if json.loads((root / "run.json").read_text(encoding="utf-8"))["run_id"] != prepared_run_id:
                raise ValueError("Prepared workspace run ID differs")
        round_dir = root / ROUND_DIRECTORY
        round_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise FileExistsError(f"候选工作目录已存在，拒绝覆盖：{root}") from None

    subject_path = root / SUBJECT_FILENAME
    plan_path = round_dir / "plan.json"
    candidate_path = round_dir / "candidate.json"
    llm_result_path = round_dir / "llm_result.json"
    _write_source_snapshot(subject_path, source)
    _write_json(plan_path, llm_result.generated.plan.canonical_json(indent=2))
    _write_json(llm_result_path, llm_result.canonical_json(indent=2))

    relative_test_path = ROUND_DIRECTORY / TEST_FILENAME
    validation_error: str | None = None
    test_path: Path | None = None
    try:
        if expected_oracle_mode is not None and llm_result.generated.plan.oracle_mode != expected_oracle_mode:
            raise CandidateValidationError("oracle_mode", "候选改变了原始预期模式。")
        validate_generated_tests(llm_result.generated, source)
    except CandidateValidationError as error:
        validation_error = error.summary
    else:
        if llm_result.generated.code is not None:
            test_path = relative_test_path
            (root / relative_test_path).write_text(llm_result.generated.code, encoding="utf-8")

    candidate = CandidateRound(
        number=1,
        generated=llm_result.generated,
        test_path=test_path,
        validation_error=validation_error,
    )
    _write_json(candidate_path, candidate.canonical_json(indent=2))
    return InitialCandidateArtifacts(
        workspace=root,
        subject_path=subject_path,
        plan_path=plan_path,
        candidate_path=candidate_path,
        llm_result_path=llm_result_path,
        candidate=candidate,
        llm_result=llm_result,
    )


def validate_generated_tests(generated: GeneratedTests, source: SourceInfo) -> None:
    """Apply source-aware and AST-based checks before generated code becomes executable."""

    if not isinstance(generated, GeneratedTests):
        raise TypeError("generated must be GeneratedTests")
    if not isinstance(source, SourceInfo):
        raise TypeError("source must be a SourceInfo")
    if generated.plan.schema_version != "1.1":
        raise CandidateValidationError(
            "schema_version", "首轮候选必须使用包含关系策略的 TestPlan 1.1。",
        )
    source_functions = {item.name for item in source.functions}
    if set(generated.plan.target_functions) != source_functions:
        raise CandidateValidationError(
            "target_functions", "测试计划必须逐一覆盖分析出的全部顶层目标函数。",
        )
    try:
        validate_plan_against_source(generated.plan, source)
    except ValueError as error:
        raise CandidateValidationError("source_evidence", str(error)) from None
    if generated.code is None:
        return

    try:
        tree = ast.parse(generated.code, filename=TEST_FILENAME, type_comments=True)
    except SyntaxError as error:
        raise CandidateValidationError(
            "test_syntax", error.msg or "无效 Python 语法", line=error.lineno,
        ) from None
    _validate_module_shape(tree)
    imported_targets = _validate_imports(tree, source_functions)
    _validate_target_shadowing(tree, source_functions)
    _validate_forbidden_constructs(tree)
    tests = _top_level_tests(tree)
    mapped_names = {item.test_function for item in generated.mappings}
    if set(tests) != mapped_names:
        missing = sorted(mapped_names - set(tests))
        extra = sorted(set(tests) - mapped_names)
        detail = []
        if missing:
            detail.append("缺少映射函数 " + "、".join(missing))
        if extra:
            detail.append("存在未映射测试 " + "、".join(extra))
        raise CandidateValidationError("test_mapping", "；".join(detail))

    scenario_by_id = {item.scenario_id: item for item in generated.plan.scenarios}
    for mapping in generated.mappings:
        function = tests[mapping.test_function]
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function
            for node in ast.walk(function)
        ):
            raise CandidateValidationError(
                "nested_test_function",
                f"{mapping.test_function} 不得把断言或目标调用隐藏在嵌套函数中。",
                line=function.lineno,
            )
        scenarios = [scenario_by_id[item] for item in mapping.scenario_ids]
        required_calls = {scenario.target_function for scenario in scenarios}
        required_calls.update(
            step.function for scenario in scenarios for step in scenario.steps
        )
        calls = {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        missing_calls = required_calls - calls
        if missing_calls:
            raise CandidateValidationError(
                "scenario_not_exercised",
                f"{mapping.test_function} 未调用场景目标：{'、'.join(sorted(missing_calls))}",
                line=function.lineno,
            )
        oracle_targets = _oracle_targets(function, source_functions)
        expected_oracles = {scenario.target_function for scenario in scenarios}
        if not expected_oracles.issubset(oracle_targets):
            raise CandidateValidationError(
                "missing_assertion",
                f"{mapping.test_function} 的 assert/pytest.raises 未验证场景目标："
                f"{'、'.join(sorted(expected_oracles - oracle_targets))}",
                line=function.lineno,
            )
    called_targets = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in source_functions
    }
    if not called_targets.issubset(imported_targets):
        raise CandidateValidationError(
            "target_import", "目标函数必须通过 from subject import <target_function> 导入。",
        )


def preserved_tests(history: tuple[CandidateRound, ...]) -> dict[str, tuple[ast.FunctionDef, GeneratedTests]]:
    """Keep tests observed to pass, excluding every failing parametrized function."""
    protected = {}
    for candidate in history:
        execution, generated = candidate.execution, candidate.generated
        if (execution is None or generated is None or generated.code is None
                or execution.kind not in {"passed", "assertion_failed", "execution_error"}
                or not execution.passed or execution.skipped or execution.xfailed or execution.xpassed):
            continue
        if not execution.all_passed and (not execution.failures or
                len({item.nodeid for item in execution.failures}) != execution.failed + execution.errors):
            continue  # Old counter-only snapshots cannot identify passing tests.
        failed = {item.nodeid.split("::")[-1].split("[")[0] for item in execution.failures}
        for node in ast.parse(generated.code).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_") and node.name not in failed:
                protected[node.name] = (node, generated)
    return protected


def validate_preserved_tests(generated: GeneratedTests, history: tuple[CandidateRound, ...]) -> None:
    """Require previously passing test functions and scenarios to stay intact.

    Add new test functions to extend coverage. This avoids pretending an AST
    matcher can prove arbitrary assertion rewrites or fixture changes equivalent.
    """
    if generated.code is None:
        return
    current = {node.name: node for node in ast.parse(generated.code).body if isinstance(node, ast.FunctionDef)}
    scenario_map = {item.scenario_id: item for item in generated.plan.scenarios}
    mapping_map = {item.test_function: item for item in generated.mappings}
    relation_map = {item.relation_id: item for item in generated.plan.relationships}
    for name, (node, old) in preserved_tests(history).items():
        # A constant/helper/import change can weaken an unchanged assertion.
        old_tree, new_tree = ast.parse(old.code), ast.parse(generated.code)
        old_support = {ast.dump(item) for item in old_tree.body
                       if not isinstance(item, ast.FunctionDef) or not item.name.startswith("test_")}
        new_support = {ast.dump(item) for item in new_tree.body
                       if not isinstance(item, ast.FunctionDef) or not item.name.startswith("test_")}
        if not old_support.issubset(new_support):
            raise CandidateValidationError("preserve_tests", "必须保留已通过测试的原导入、常量及辅助函数，不能间接改写断言。")
        bindings = []
        for item in new_tree.body:
            if isinstance(item, (ast.Import, ast.ImportFrom)):
                bindings.extend(alias.asname or alias.name.split(".")[0] for alias in item.names)
            elif isinstance(item, ast.Assign):
                bindings.extend(target.id for target in item.targets if isinstance(target, ast.Name))
            elif isinstance(item, ast.FunctionDef):
                bindings.append(item.name)
        if len(bindings) != len(set(bindings)):
            raise CandidateValidationError("preserve_tests", "补充候选不得通过重复绑定覆盖已保留的测试依赖。")
        if name not in current or ast.dump(current[name]) != ast.dump(node):
            raise CandidateValidationError("preserve_tests", f"必须保留已通过测试 {name} 的函数体、断言及装饰器；新增场景请另建测试函数。")
        mapping = next(item for item in old.mappings if item.test_function == name)
        if mapping_map.get(name) != mapping:
            raise CandidateValidationError("preserve_tests", f"必须保留已通过测试 {name} 的场景映射。")
        for scenario in old.plan.scenarios:
            if scenario.scenario_id in mapping.scenario_ids:
                if scenario_map.get(scenario.scenario_id) != scenario:
                    raise CandidateValidationError("preserve_tests", f"必须保留已通过场景 {scenario.scenario_id} 的输入与预期依据。")
                for relation in old.plan.relationships:
                    if relation.relation_id in scenario.relationship_ids and relation_map.get(relation.relation_id) != relation:
                        raise CandidateValidationError("preserve_tests", "必须保留已通过协作场景的关系证据。")


def stage_revision_candidate(
    *, source: SourceInfo, llm_result: LLMGenerationResult, workspace: str | Path,
    history: tuple[CandidateRound, ...],
    expected_oracle_mode: OracleMode | None = None,
) -> InitialCandidateArtifacts:
    """Stage rounds 02/03 without overwriting earlier candidates or subject.py."""
    from .tools.test_runner import ExecutionWorkspaceError, _contained_path

    if not history or [item.number for item in history] != list(range(1, len(history) + 1)):
        raise ValueError("revision requires consecutive previous rounds")
    number = len(history) + 1
    relative = round_directory(number)
    root = Path(workspace)
    if root.is_symlink() or root.is_junction():
        raise ExecutionWorkspaceError("Workspace cannot be a symbolic link or junction.")
    root = root.resolve(strict=True)
    subject = _contained_path(root, Path(SUBJECT_FILENAME))
    if subject.read_bytes() != source.source_code.encode(source.encoding):
        raise ExecutionWorkspaceError("Source snapshot changed; revision is stopped.")
    directory = _contained_path(root, relative)
    directory.mkdir(exist_ok=False)
    plan_path, candidate_path, result_path = (directory / name for name in ("plan.json", "candidate.json", "llm_result.json"))
    _write_json(plan_path, llm_result.generated.plan.canonical_json(indent=2))
    _write_json(result_path, llm_result.canonical_json(indent=2))
    error, test_path = None, None
    try:
        if expected_oracle_mode is not None and llm_result.generated.plan.oracle_mode != expected_oracle_mode:
            raise CandidateValidationError("oracle_mode", "候选改变了原始预期模式。")
        validate_generated_tests(llm_result.generated, source)
        validate_preserved_tests(llm_result.generated, history)
        if llm_result.generated.code is not None:
            tree = ast.dump(ast.parse(llm_result.generated.code))
            for old in history:
                if old.generated and old.generated.code and old.validation_error is None:
                    if ast.dump(ast.parse(old.generated.code)) == tree:
                        raise CandidateValidationError("duplicate_candidate", "候选代码与历史有效候选重复，停止无进展迭代。")
    except CandidateValidationError as exc:
        error = exc.summary
    else:
        if llm_result.generated.code is not None:
            test_path = relative / TEST_FILENAME
            (root / test_path).write_text(llm_result.generated.code, encoding="utf-8")
    candidate = CandidateRound(number=number, generated=llm_result.generated, test_path=test_path, validation_error=error)
    _write_json(candidate_path, candidate.canonical_json(indent=2))
    return InitialCandidateArtifacts(workspace=root, subject_path=subject, plan_path=plan_path,
                                    candidate_path=candidate_path, llm_result_path=result_path,
                                    candidate=candidate, llm_result=llm_result)


def _validate_module_shape(tree: ast.Module) -> None:
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if not _call_name(target).endswith(".mark.parametrize"):
                    raise CandidateValidationError(
                        "test_decorator",
                        "生成测试只允许 pytest.mark.parametrize 装饰器。",
                        line=decorator.lineno,
                    )
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, ast.Assign):
            try:
                ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
            else:
                if all(isinstance(target, ast.Name) and target.id.isupper() for target in node.targets):
                    continue
        raise CandidateValidationError(
            "module_side_effect", "测试模块顶层只允许导入、函数、文档字符串和字面量常量。",
            line=getattr(node, "lineno", None),
        )


def _validate_imports(tree: ast.Module, source_functions: set[str]) -> set[str]:
    imported_targets: set[str] = set()
    top_level_imports = {
        id(node) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if id(node) not in top_level_imports:
                raise CandidateValidationError(
                    "local_import", "生成测试的导入必须位于模块顶层。", line=node.lineno,
                )
            for alias in node.names:
                if alias.name == "unittest.mock" or alias.name.startswith("unittest.mock."):
                    raise CandidateValidationError(
                        "mocking", "协作测试必须调用真实同文件依赖，禁止 mock/patch。",
                        line=node.lineno,
                    )
                root = alias.name.split(".", 1)[0]
                if root == "subject":
                    raise CandidateValidationError(
                        "target_import", "禁止 import subject；请直接导入目标函数。", line=node.lineno,
                    )
                _validate_dependency(root, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if id(node) not in top_level_imports:
                raise CandidateValidationError(
                    "local_import", "生成测试的导入必须位于模块顶层。", line=node.lineno,
                )
            if node.level:
                raise CandidateValidationError(
                    "relative_import", "生成测试禁止相对导入。", line=node.lineno,
                )
            root = (node.module or "").split(".", 1)[0]
            if node.module == "unittest.mock" or (node.module or "").startswith("unittest.mock."):
                raise CandidateValidationError(
                    "mocking", "协作测试必须调用真实同文件依赖，禁止 mock/patch。",
                    line=node.lineno,
                )
            if root == "subject":
                for alias in node.names:
                    if alias.name == "*" or alias.asname is not None or alias.name not in source_functions:
                        raise CandidateValidationError(
                            "target_import",
                            "subject 只能无别名地导入计划中的明确目标函数。",
                            line=node.lineno,
                        )
                    imported_targets.add(alias.name)
            else:
                _validate_dependency(root, node.lineno)
    if not imported_targets:
        raise CandidateValidationError(
            "target_import", "生成测试缺少 from subject import <target_function>。",
        )
    return imported_targets


def _validate_target_shadowing(tree: ast.Module, source_functions: set[str]) -> None:
    for node in ast.walk(tree):
        name: str | None = None
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            name = node.id
        elif isinstance(node, ast.arg):
            name = node.arg
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        elif isinstance(node, ast.ExceptHandler):
            name = node.name
        if name in source_functions:
            raise CandidateValidationError(
                "target_shadowing",
                f"生成测试重新绑定了目标函数名称 {name}。",
                line=getattr(node, "lineno", None),
            )


def _validate_dependency(root: str, line: int) -> None:
    if root in _BANNED_MODULES:
        raise CandidateValidationError(
            "unsafe_dependency", f"生成测试不得使用文件、进程或网络模块 {root}。", line=line,
        )
    if root == "mock":
        raise CandidateValidationError(
            "mocking", "协作测试必须调用真实同文件依赖，禁止 mock/patch。", line=line,
        )
    if root != "pytest" and root not in sys.stdlib_module_names:
        raise CandidateValidationError(
            "external_dependency", f"生成测试包含不支持的第三方依赖 {root}。", line=line,
        )


def _validate_forbidden_constructs(tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            raise CandidateValidationError(
                "async_test", "当前只支持同步 pytest 测试。", line=node.lineno,
            )
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant):
            raise CandidateValidationError(
                "unconditional_assertion", "禁止使用常量断言制造无效测试。", line=node.lineno,
            )
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id in {
            "Exception", "BaseException",
        }:
            raise CandidateValidationError(
                "broad_exception", "禁止捕获宽泛异常隐藏断言或执行错误。", line=node.lineno,
            )
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _BANNED_CALLS or name.rsplit(".", 1)[-1] in _BANNED_CALL_SUFFIXES:
                raise CandidateValidationError(
                    "forbidden_call", f"生成测试包含禁止调用 {name}。", line=node.lineno,
                )
        if isinstance(node, ast.Attribute) and node.attr in {"importorskip", "skip", "skipif", "xfail"}:
            raise CandidateValidationError(
                "forbidden_call", f"生成测试包含禁止标记或调用 {_call_name(node)}。", line=node.lineno,
            )
        if isinstance(node, ast.Name) and node.id in {"Mock", "MagicMock", "mocker", "monkeypatch"}:
            raise CandidateValidationError(
                "mocking", "生成测试不得替换真实同文件依赖。", line=node.lineno,
            )


def _top_level_tests(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    tests: dict[str, ast.FunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            if node.name in tests:
                raise CandidateValidationError(
                    "duplicate_test", f"测试函数重复定义：{node.name}", line=node.lineno,
                )
            tests[node.name] = node
        elif isinstance(node, ast.ClassDef) and any(
            isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test_")
            for item in node.body
        ):
            raise CandidateValidationError(
                "test_mapping", "映射测试必须是模块级 pytest 函数。", line=node.lineno,
            )
    return tests


def _oracle_targets(function: ast.FunctionDef, source_functions: set[str]) -> set[str]:
    assigned_results: dict[str, str] = {}
    for node in ast.walk(function):
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in source_functions
        ):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assigned_results[target.id] = node.value.func.id

    covered: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):
            covered.update(
                call.func.id
                for call in ast.walk(node.test)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id in source_functions
            )
            covered.update(
                assigned_results[name.id]
                for name in ast.walk(node.test)
                if isinstance(name, ast.Name) and name.id in assigned_results
            )
        elif isinstance(node, (ast.With, ast.AsyncWith)) and any(
            isinstance(item.context_expr, ast.Call)
            and _call_name(item.context_expr.func) in {"pytest.raises", "raises"}
            for item in node.items
        ):
            for statement in node.body:
                covered.update(
                    call.func.id
                    for call in ast.walk(statement)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in source_functions
                )
    return covered


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return "<dynamic>"


def _write_source_snapshot(path: Path, source: SourceInfo) -> None:
    try:
        content = source.source_code.encode(source.encoding)
    except (LookupError, UnicodeEncodeError):
        raise ValueError("SourceInfo encoding cannot reproduce the analyzed source snapshot") from None
    path.write_bytes(content)


def _write_json(path: Path, content: str) -> None:
    path.write_text(content + ("" if content.endswith("\n") else "\n"), encoding="utf-8")


_BANNED_MODULES = frozenset({
    "ftplib", "http", "os", "pathlib", "shutil", "socket", "subprocess", "tempfile", "urllib",
})
_BANNED_CALLS = frozenset({
    "__import__", "compile", "eval", "exec", "open", "pytest.main",
})
_BANNED_CALL_SUFFIXES = frozenset({
    "MagicMock", "Mock", "open", "patch", "remove", "rename", "setattr",
    "importorskip", "skip", "skipif", "unlink", "write_bytes", "write_text", "xfail",
})
