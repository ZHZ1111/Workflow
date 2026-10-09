"""Attach measured missing positions to source text without executing the source."""

from __future__ import annotations

import ast
from typing import Any

from ..models import CoverageResult


def build_coverage_source_hints(source_code: str, coverage: CoverageResult) -> dict[str, Any]:
    """Map missing lines/arcs, retaining signed code-object exit endpoints.

    Missing locations outside this exact snapshot raise ValueError rather than
    creating guessed snippets. Negative endpoints refer to code-object exits,
    anchored to the absolute line number, not to a negative source index.
    """
    if not isinstance(source_code, str) or not isinstance(coverage, CoverageResult):
        raise TypeError("source_code must be text and coverage must be CoverageResult")
    hints: dict[str, Any] = {
        "status": coverage.status, "target_file": str(coverage.target_file),
        "missing_lines": [], "missing_branches": [],
    }
    if coverage.status == "unavailable":
        hints["error"] = coverage.error
        return hints
    lines = source_code.splitlines()
    tree = ast.parse(source_code)
    functions = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}.{child.name}" if prefix else child.name
                start = min([child.lineno, *(decorator.lineno for decorator in child.decorator_list)])
                if not isinstance(child, ast.ClassDef):
                    functions.append((start, child.end_lineno, name))
                visit(child, name)
            else:
                visit(child, prefix)

    visit(tree)

    def location(number: int) -> dict[str, Any]:
        line = abs(number)
        if not 1 <= line <= len(lines):
            raise ValueError("coverage location is outside the source snapshot")
        owners = [(end - start, name) for start, end, name in functions if start <= line <= end]
        return {
            "line": line,
            "kind": "code_object_exit" if number < 0 else "source_line",
            "source": lines[line - 1],
            "function": min(owners)[1] if owners else None,
        }

    hints["missing_lines"] = [location(line) for line in coverage.missing_lines]
    hints["missing_branches"] = [
        {"start": arc.start, "end": arc.end,
         "start_location": location(arc.start), "end_location": location(arc.end)}
        for arc in coverage.missing_branches
    ]
    return hints
