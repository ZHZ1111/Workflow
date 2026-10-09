"""Prepare deterministic invalid/revision candidates without running source code."""

import ast
from pathlib import Path

from testgen_agent.models import GeneratedTests

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples/fixtures"


def main():
    calculation = GeneratedTests.model_validate_json((FIXTURES / "calculation_complete.json").read_text(encoding="utf-8"))
    invalid = calculation.updated(code="from subject import calculate_total\n\ndef test_broken(:\n    pass\n")
    (FIXTURES / "calculation_invalid_syntax.json").write_text(invalid.canonical_json(indent=2) + "\n", encoding="utf-8")
    first = GeneratedTests.model_validate_json((FIXTURES / "validation_round_01.json").read_text(encoding="utf-8"))
    second = GeneratedTests.model_validate_json((FIXTURES / "validation_round_02.json").read_text(encoding="utf-8"))
    removed = first.mappings[0].test_function
    mappings = tuple(item for item in second.mappings if item.test_function != removed)
    scenario_ids = {sid for item in mappings for sid in item.scenario_ids}
    tree = ast.parse(second.code)
    tree.body = [node for node in tree.body if not isinstance(node, ast.FunctionDef) or node.name != removed]
    deleted = second.updated(code=ast.unparse(tree) + "\n", mappings=mappings,
                             plan=second.plan.updated(scenarios=tuple(
                                 item for item in second.plan.scenarios if item.scenario_id in scenario_ids)))
    (FIXTURES / "validation_removed_pass.json").write_text(deleted.canonical_json(indent=2) + "\n", encoding="utf-8")
    tree = ast.parse(second.code)
    target = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == removed)
    assertion = next(node for node in target.body if isinstance(node, ast.Assert))
    assertion.test = ast.Compare(left=assertion.test.left, ops=[ast.In()],
                                 comparators=[ast.List(elts=[ast.Constant("pass"), ast.Constant("fail")], ctx=ast.Load())])
    weakened = second.updated(code=ast.unparse(tree) + "\n")
    (FIXTURES / "validation_weakened_pass.json").write_text(weakened.canonical_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
