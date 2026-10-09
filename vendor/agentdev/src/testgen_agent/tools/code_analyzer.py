"""AST-only source and relationship inspection for the supported Python subset."""

from __future__ import annotations

import ast
import builtins
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Literal, NoReturn

from testgen_agent.models import (
    CallSite,
    DirectCall,
    FunctionInfo,
    ImportInfo,
    SourceBranch,
    SourceInfo,
    UnresolvedCall,
)

from .source_reader import LoadedSource, MAX_SOURCE_BYTES, read_source


class SourceAnalysisError(ValueError):
    """A safe, classified analysis failure that callers can show directly."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Path,
        line: int | None = None,
        offset: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.line = line
        self.offset = offset


class UnsupportedSourceError(SourceAnalysisError):
    """The file is valid Python but outside this project's stated scope."""


@dataclass(frozen=True, slots=True)
class DataFlowHint:
    """One deliberately narrow, source-backed transfer between confirmed calls."""

    producer_call_site_id: str
    consumer_call_site_id: str
    caller: str
    kind: Literal["nested_call", "simple_assignment"]
    via: str


def analyze_source(
    source_path: str | Path,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> SourceInfo:
    """Read, validate and summarize a source file without importing it."""

    return analyze_loaded_source(read_source(source_path, max_bytes=max_bytes))


def analyze_loaded_source(source: LoadedSource) -> SourceInfo:
    """Parse an already bounded and decoded source file into ``SourceInfo``."""

    try:
        tree = ast.parse(source.code, filename=str(source.path), type_comments=True)
    except SyntaxError as error:
        reason = error.msg or "无效语法"
        line = error.lineno
        location = f"第 {line} 行" if line is not None else "未知行"
        raise SourceAnalysisError(
            "syntax_error",
            f"Python 语法错误（{location}）：{reason}",
            path=source.path,
            line=line,
            offset=error.offset,
        ) from None

    _reject_unsupported(tree, source)
    function_nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if not function_nodes:
        raise SourceAnalysisError(
            "no_target_functions",
            "源码中没有可测试的顶层同步函数；不会进入测试生成循环。",
            path=source.path,
        )
    if len(function_nodes) > 5:
        raise UnsupportedSourceError(
            "too_many_functions",
            f"当前不支持超过 5 个顶层函数（检测到 {len(function_nodes)} 个）。",
            path=source.path,
            line=function_nodes[5].lineno,
        )

    functions = tuple(_function_info(node) for node in function_nodes)
    imports = tuple(_import_info(node, source.code) for node in _import_nodes(tree))
    branches = tuple(
        branch
        for function in function_nodes
        for branch in _branches(function, source.code)
    )
    call_sites, direct_calls, unresolved_calls = _analyze_calls(
        tree, function_nodes, source.code,
    )
    return SourceInfo(
        source_path=source.path,
        source_code=source.code,
        encoding=source.encoding,
        functions=functions,
        imports=imports,
        branches=branches,
        call_sites=call_sites,
        direct_calls=direct_calls,
        unresolved_calls=unresolved_calls,
    )


def _function_info(node: ast.FunctionDef) -> FunctionInfo:
    return FunctionInfo(
        name=node.name,
        signature=f"({ast.unparse(node.args)})",
        start_line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        docstring=ast.get_docstring(node, clean=True),
    )


def _import_nodes(tree: ast.Module) -> list[ast.Import | ast.ImportFrom]:
    return sorted(
        (node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))),
        key=lambda item: (item.lineno, item.col_offset),
    )


def _import_info(node: ast.Import | ast.ImportFrom, source_code: str) -> ImportInfo:
    segment = ast.get_source_segment(source_code, node) or ast.unparse(node)
    module = node.module if isinstance(node, ast.ImportFrom) else None
    return ImportInfo(
        statement=segment,
        line=node.lineno,
        module=module,
        names=tuple(alias.name for alias in node.names),
    )


class _BranchVisitor(ast.NodeVisitor):
    """Visit one function body while excluding definitions executed only later."""

    def __init__(self, source_code: str) -> None:
        self.source_code = source_code
        self.items: list[SourceBranch] = []

    def inspect(self, function: ast.FunctionDef) -> tuple[SourceBranch, ...]:
        for statement in function.body:
            self.visit(statement)
        return tuple(self.items)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return None

    def visit_If(self, node: ast.If) -> None:  # noqa: N802
        self._add("if", node, f"if {_segment(self.source_code, node.test)}")
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:  # noqa: N802
        self._add("match", node, f"match {_segment(self.source_code, node.subject)}")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self._add(
            "loop", node,
            f"for {_segment(self.source_code, node.target)} in {_segment(self.source_code, node.iter)}",
        )
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:  # noqa: N802
        self._add("loop", node, f"while {_segment(self.source_code, node.test)}")
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:  # noqa: N802
        self._add("try", node, "try")
        self.generic_visit(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:  # noqa: N802
        self._add("try", node, "try")
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802
        self._add("raise", node, _segment(self.source_code, node))
        self.generic_visit(node)

    def _add(self, kind: str, node: ast.AST, source: str) -> None:
        self.items.append(SourceBranch(kind=kind, line=node.lineno, source=source))


def _branches(function: ast.FunctionDef, source_code: str) -> tuple[SourceBranch, ...]:
    return _BranchVisitor(source_code).inspect(function)


def _segment(source_code: str, node: ast.AST) -> str:
    return ast.get_source_segment(source_code, node) or ast.unparse(node)


class _BindingCollector(ast.NodeVisitor):
    """Collect function-scope bindings without descending into child scopes."""

    def __init__(self, function: ast.FunctionDef) -> None:
        self.parameters = _parameter_names(function.args)
        self.locals: set[str] = set(self.parameters)
        self.globals: set[str] = set()
        self.global_writes: set[str] = set()
        self.imported_names: set[str] = set()
        self.non_import_bindings: set[str] = set(self.parameters)
        for statement in function.body:
            self.visit(statement)
        self.locals.difference_update(self.globals)

    def visit_Global(self, node: ast.Global) -> None:  # noqa: N802
        self.globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802
        return None

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._bind_name(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._bind_name(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._bind_name(node.name)

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return None

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            self._bind_target(target)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self._bind_target(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        self._bind_target(node.target)
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802
        self._bind_target(node.target)
        self.visit(node.value)

    def visit_For(self, node: ast.For) -> None:  # noqa: N802
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802
        self._bind_target(node.target)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:  # noqa: N802
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        for item in node.items:
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.name:
            self._bind_name(node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            name = alias.asname or alias.name.split(".", 1)[0]
            self._bind_import(name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for alias in node.names:
            name = alias.asname or alias.name
            self._bind_import(name)

    def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802
        for target in node.targets:
            self._bind_target(target)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:  # noqa: N802
        if node.name:
            self._bind_name(node.name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:  # noqa: N802
        if node.name:
            self._bind_name(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:  # noqa: N802
        if node.rest:
            self._bind_name(node.rest)
        self.generic_visit(node)

    def _bind_target(self, target: ast.AST) -> None:
        for name in _target_names(target):
            self._bind_name(name)

    def _bind_name(self, name: str) -> None:
        if name in self.globals:
            self.global_writes.add(name)
        else:
            self.locals.add(name)
            self.non_import_bindings.add(name)

    def _bind_import(self, name: str) -> None:
        if name in self.globals:
            self.global_writes.add(name)
        else:
            self.imported_names.add(name)
            self.locals.add(name)


class _ModuleBindingCollector(_BindingCollector):
    """Find non-canonical writes that can replace a target function binding."""

    def __init__(self, tree: ast.Module) -> None:
        self.parameters = set()
        self.locals = set()
        self.globals = set()
        self.global_writes = set()
        self.imported_names = set()
        self.non_import_bindings = set()
        self.decorated_functions: set[str] = set()
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.decorator_list:
                    self.decorated_functions.add(statement.name)
                continue
            self.visit(statement)

    def _bind_name(self, name: str) -> None:
        self.locals.add(name)
        self.non_import_bindings.add(name)


class _CallVisitor(ast.NodeVisitor):
    """Record calls in one function while respecting child and comprehension scopes."""

    def __init__(
        self,
        function: ast.FunctionDef,
        source_code: str,
        target_names: set[str],
        module_uncertain: set[str],
        module_imports: set[str],
        module_dynamic_names: set[str],
        bindings: _BindingCollector,
    ) -> None:
        self.caller = function.name
        self.source_code = source_code
        self.source_lines = source_code.splitlines()
        self.target_names = target_names
        self.module_uncertain = module_uncertain
        self.module_imports = module_imports
        self.module_dynamic_names = module_dynamic_names
        self.bindings = bindings
        self.temporary_shadows: list[set[str]] = []
        self.call_sites: list[CallSite] = []
        self.direct_calls: list[DirectCall] = []
        self.unresolved_calls: list[UnresolvedCall] = []

    def inspect(self, function: ast.FunctionDef) -> None:
        for statement in function.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return None

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        call_id = _call_id(self.caller, node)
        expression = _segment(self.source_code, node)
        source_line = self.source_lines[node.lineno - 1].strip() or expression
        site = CallSite(
            call_id=call_id,
            caller=self.caller,
            expression=expression,
            line=node.lineno,
            source=source_line,
            argument_expressions=_argument_expressions(node, self.source_code),
        )
        self.call_sites.append(site)
        direct_callee, unresolved_reason = self._classify(node.func)
        if direct_callee is not None:
            self.direct_calls.append(DirectCall(
                call_site_id=call_id, caller=self.caller, callee=direct_callee,
            ))
        elif unresolved_reason is not None:
            self.unresolved_calls.append(UnresolvedCall(
                call_site_id=call_id, caller=self.caller, reason=unresolved_reason,
            ))
        self.generic_visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.elt,))

    def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802
        self._visit_comprehension(node.generators, (node.key, node.value))

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        result_nodes: tuple[ast.AST, ...],
    ) -> None:
        shadows: set[str] = set()
        if generators:
            self.visit(generators[0].iter)
        self.temporary_shadows.append(shadows)
        try:
            for index, generator in enumerate(generators):
                # The first iterable is evaluated outside the comprehension's
                # target scope; later iterables can see earlier targets.
                if index > 0:
                    self.visit(generator.iter)
                shadows.update(_target_names(generator.target))
                for condition in generator.ifs:
                    self.visit(condition)
            for result in result_nodes:
                self.visit(result)
        finally:
            self.temporary_shadows.pop()

    def _classify(self, callable_node: ast.expr) -> tuple[str | None, str | None]:
        if isinstance(callable_node, ast.Name):
            name = callable_node.id
            temporarily_shadowed = any(name in scope for scope in self.temporary_shadows)
            if name in self.target_names:
                if temporarily_shadowed:
                    return None, "同名调用在推导式作用域中被迭代变量遮蔽"
                if name in self.bindings.locals:
                    return None, "同名调用被函数参数或局部绑定遮蔽"
                if name in self.module_uncertain:
                    return None, "同文件函数名在模块或 global 作用域中可能被重新绑定"
                return name, None
            if temporarily_shadowed:
                return None, "调用目标来自推导式局部绑定，无法静态确认"
            if (
                name in self.bindings.imported_names
                and name not in self.bindings.non_import_bindings
            ):
                return None, None
            if name in self.bindings.locals:
                return None, "调用目标来自参数或局部绑定，无法静态确认"
            if name in self.module_imports and name not in self.module_dynamic_names:
                return None, None
            if name in self.module_dynamic_names:
                return None, "模块名称由运行时值绑定，无法静态确认"
            if name in _BUILTIN_NAMES:
                return None, None
            return None, "名称调用无法静态确认到标准库或同文件目标函数"
        if isinstance(callable_node, ast.Attribute):
            root = _attribute_root(callable_node)
            if root is not None:
                if (
                    root in self.bindings.imported_names
                    and root not in self.bindings.non_import_bindings
                ):
                    return None, None
                if root in self.bindings.locals:
                    return None, "属性调用的根名称来自局部绑定，无法静态确认"
                if root in self.module_imports and root not in self.module_dynamic_names:
                    return None, None
                if root in self.module_dynamic_names:
                    return None, "属性调用的模块根名称可能被重新绑定，无法静态确认"
            if isinstance(callable_node.value, ast.Constant):
                return None, None
            return None, "属性调用可能依赖对象运行时类型，无法静态确认"
        if isinstance(callable_node, ast.Lambda):
            return None, None
        return None, "动态调用表达式无法静态确认"


_BUILTIN_NAMES = frozenset(dir(builtins))


def _analyze_calls(
    tree: ast.Module,
    functions: list[ast.FunctionDef],
    source_code: str,
) -> tuple[tuple[CallSite, ...], tuple[DirectCall, ...], tuple[UnresolvedCall, ...]]:
    target_names = {function.name for function in functions}
    module_bindings = _ModuleBindingCollector(tree)
    function_bindings = {function.name: _BindingCollector(function) for function in functions}
    global_writes = {
        name
        for bindings in function_bindings.values()
        for name in bindings.global_writes
    }
    module_uncertain = (
        (module_bindings.locals | module_bindings.decorated_functions | global_writes)
        & target_names
    )
    module_imports = module_bindings.imported_names
    module_dynamic_names = module_bindings.non_import_bindings
    call_sites: list[CallSite] = []
    direct_calls: list[DirectCall] = []
    unresolved_calls: list[UnresolvedCall] = []
    for function in functions:
        visitor = _CallVisitor(
            function,
            source_code,
            target_names,
            module_uncertain,
            module_imports,
            module_dynamic_names,
            function_bindings[function.name],
        )
        visitor.inspect(function)
        call_sites.extend(visitor.call_sites)
        direct_calls.extend(visitor.direct_calls)
        unresolved_calls.extend(visitor.unresolved_calls)
    return tuple(call_sites), tuple(direct_calls), tuple(unresolved_calls)


def build_call_graph(source: SourceInfo) -> dict[str, tuple[str, ...]]:
    """Derive a deterministic adjacency list from tool-confirmed direct calls."""

    if not isinstance(source, SourceInfo):
        raise TypeError("source must be a SourceInfo")
    graph: dict[str, list[str]] = {function.name: [] for function in source.functions}
    for call in source.direct_calls:
        if call.callee not in graph[call.caller]:
            graph[call.caller].append(call.callee)
    return {name: tuple(callees) for name, callees in graph.items()}


def find_call_cycles(source: SourceInfo) -> tuple[tuple[str, ...], ...]:
    """Return recursive strongly connected groups without expanding call paths."""

    graph = build_call_graph(source)
    components = _strongly_connected_components(graph)
    return tuple(
        component
        for component in components
        if len(component) > 1 or component[0] in graph[component[0]]
    )


def candidate_entry_functions(source: SourceInfo) -> tuple[str, ...]:
    """Return call-graph root components as hints, never as business truth."""

    graph = build_call_graph(source)
    components = _strongly_connected_components(graph)
    component_by_name = {
        name: index for index, component in enumerate(components) for name in component
    }
    incoming = [False] * len(components)
    for caller, callees in graph.items():
        caller_component = component_by_name[caller]
        for callee in callees:
            callee_component = component_by_name[callee]
            if caller_component != callee_component:
                incoming[callee_component] = True
    roots = {
        name
        for index, component in enumerate(components)
        if not incoming[index]
        for name in component
    }
    return tuple(function.name for function in source.functions if function.name in roots)


def find_explicit_data_flows(source: SourceInfo) -> tuple[DataFlowHint, ...]:
    """Find only nested-call and straight-line single-name result transfers."""

    if not isinstance(source, SourceInfo):
        raise TypeError("source must be a SourceInfo")
    tree = ast.parse(source.source_code, filename=str(source.source_path), type_comments=True)
    function_nodes = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    direct_ids = {item.call_site_id for item in source.direct_calls}
    call_nodes = _source_call_nodes(function_nodes.values())
    hints: list[DataFlowHint] = []
    seen: set[tuple[str, str, str, str]] = set()

    def add(producer_id: str, consumer_id: str, kind: str, via: str) -> None:
        if producer_id == consumer_id or producer_id not in direct_ids or consumer_id not in direct_ids:
            return
        key = (producer_id, consumer_id, kind, via)
        if key in seen:
            return
        seen.add(key)
        caller = next(
            item.caller for item in source.call_sites if item.call_id == consumer_id
        )
        hints.append(DataFlowHint(
            producer_call_site_id=producer_id,
            consumer_call_site_id=consumer_id,
            caller=caller,
            kind=kind,
            via=via,
        ))

    for direct_call in source.direct_calls:
        consumer_id = direct_call.call_site_id
        consumer = call_nodes.get(consumer_id)
        if consumer is None:
            continue
        values: list[ast.expr] = [
            argument.value if isinstance(argument, ast.Starred) else argument
            for argument in consumer.args
        ] + [keyword.value for keyword in consumer.keywords]
        for value in values:
            if isinstance(value, ast.Call):
                producer_id = _call_id(_caller_from_id(consumer_id), value)
                add(producer_id, consumer_id, "nested_call", _segment(source.source_code, value))

    for caller, function in function_nodes.items():
        available: dict[str, str] = {}
        for statement in function.body:
            if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return, ast.Raise)):
                available.clear()
                continue
            for consumer in _statement_calls(statement):
                consumer_id = _call_id(caller, consumer)
                if consumer_id not in direct_ids:
                    continue
                for value in (*consumer.args, *(keyword.value for keyword in consumer.keywords)):
                    actual = value.value if isinstance(value, ast.Starred) else value
                    if isinstance(actual, ast.Name) and actual.id in available:
                        add(
                            available[actual.id], consumer_id,
                            "simple_assignment", actual.id,
                        )
            assigned_names: set[str] = set()
            assigned_call: ast.Call | None = None
            if isinstance(statement, ast.Assign):
                assigned_names = {
                    name for target in statement.targets for name in _target_names(target)
                }
                if len(assigned_names) == 1 and isinstance(statement.value, ast.Call):
                    assigned_call = statement.value
            elif isinstance(statement, ast.AnnAssign):
                assigned_names = _target_names(statement.target)
                if len(assigned_names) == 1 and isinstance(statement.value, ast.Call):
                    assigned_call = statement.value
            for name in assigned_names:
                available.pop(name, None)
            if assigned_call is not None:
                producer_id = _call_id(caller, assigned_call)
                if producer_id in direct_ids:
                    available[next(iter(assigned_names))] = producer_id
    return tuple(hints)


class _CallNodeCollector(ast.NodeVisitor):
    def __init__(self, caller: str) -> None:
        self.caller = caller
        self.nodes: dict[str, ast.Call] = {}

    def inspect(self, function: ast.FunctionDef) -> None:
        for statement in function.body:
            self.visit(statement)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802
        return None

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        self.nodes[_call_id(self.caller, node)] = node
        self.generic_visit(node)


def _source_call_nodes(functions: Iterable[ast.FunctionDef]) -> dict[str, ast.Call]:
    nodes: dict[str, ast.Call] = {}
    for function in functions:
        collector = _CallNodeCollector(function.name)
        collector.inspect(function)
        nodes.update(collector.nodes)
    return nodes


def _statement_calls(statement: ast.stmt) -> tuple[ast.Call, ...]:
    collector = _CallNodeCollector("_statement")
    collector.visit(statement)
    return tuple(collector.nodes.values())


def _caller_from_id(call_id: str) -> str:
    return call_id.split(":", 2)[1]


def _strongly_connected_components(
    graph: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    """Tarjan traversal with explicit visited/index state for recursive graphs."""

    order = {name: position for position, name in enumerate(graph)}
    next_index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(name: str) -> None:
        nonlocal next_index
        indices[name] = next_index
        lowlinks[name] = next_index
        next_index += 1
        stack.append(name)
        on_stack.add(name)
        for callee in graph[name]:
            if callee not in indices:
                visit(callee)
                lowlinks[name] = min(lowlinks[name], lowlinks[callee])
            elif callee in on_stack:
                lowlinks[name] = min(lowlinks[name], indices[callee])
        if lowlinks[name] == indices[name]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == name:
                    break
            components.append(tuple(sorted(component, key=order.__getitem__)))

    for name in graph:
        if name not in indices:
            visit(name)
    return tuple(sorted(components, key=lambda item: min(order[name] for name in item)))


def _parameter_names(arguments: ast.arguments) -> set[str]:
    names = {
        argument.arg
        for argument in (*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs)
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)
    return names


def _target_names(target: ast.AST) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, (ast.Tuple, ast.List)):
        return {name for item in target.elts for name in _target_names(item)}
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return set()


def _call_id(caller: str, node: ast.Call) -> str:
    return (
        f"call:{caller}:{node.lineno}:{node.col_offset}:"
        f"{node.end_lineno or node.lineno}:{node.end_col_offset or node.col_offset}"
    )


def _argument_expressions(node: ast.Call, source_code: str) -> tuple[str, ...]:
    values: list[tuple[int, int, str]] = []
    for argument in node.args:
        values.append((argument.lineno, argument.col_offset, _segment(source_code, argument)))
    for keyword in node.keywords:
        expression = _segment(source_code, keyword.value)
        rendered = f"{keyword.arg}={expression}" if keyword.arg is not None else f"**{expression}"
        values.append((keyword.value.lineno, keyword.value.col_offset, rendered))
    return tuple(value for _, _, value in sorted(values))


def _attribute_root(node: ast.Attribute) -> str | None:
    value: ast.expr = node.value
    while isinstance(value, ast.Attribute):
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def _reject_unsupported(tree: ast.Module, source: LoadedSource) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            _unsupported(source, "async_function", node.lineno, "异步函数")
        if isinstance(node, (ast.AsyncFor, ast.AsyncWith, ast.Await)):
            _unsupported(source, "async_syntax", node.lineno, "异步语法")
        if isinstance(node, ast.ImportFrom) and node.level:
            _unsupported(source, "relative_import", node.lineno, "相对导入")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(alias.name == "*" for alias in node.names):
                _unsupported(source, "wildcard_import", node.lineno, "通配符导入")
            external = _external_modules(node)
            if external:
                joined = "、".join(external)
                _unsupported(source, "external_dependency", node.lineno, f"外部依赖 {joined}")

    top_level_classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if top_level_classes:
        _unsupported(source, "top_level_class", top_level_classes[0].lineno, "顶层类")


def _external_modules(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    if isinstance(node, ast.ImportFrom):
        modules = () if node.module is None else (node.module.split(".", 1)[0],)
    else:
        modules = tuple(alias.name.split(".", 1)[0] for alias in node.names)
    return tuple(dict.fromkeys(module for module in modules if module not in sys.stdlib_module_names))


def _unsupported(source: LoadedSource, code: str, line: int, structure: str) -> NoReturn:
    raise UnsupportedSourceError(
        code,
        f"第 {line} 行包含当前不支持的结构：{structure}。",
        path=source.path,
        line=line,
    )
