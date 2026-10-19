"""Build deterministic model-response data for later Workflow integration checks."""
from pathlib import Path

from test_workflow.agents.test_generation.core import models as m


def build(function: str, cases: list[tuple[str, object, object, str | None]], *, binary: bool = False):
    scenarios, mappings, functions = [], [], []
    for name, value, expected, exception in cases:
        args = value if binary else (value,)
        scenarios.append(m.TestScenario(
            scenario_id=name, target_function=function,
            category="exception" if exception else "boundary",
            inputs=m.ScenarioInput(args=args),
            expected_behavior=f"raises {exception}" if exception else f"returns {expected!r}",
            expected_basis=m.ExpectedBasis(kind="specification", detail=f"See examples/{'calculation' if binary else 'discount' if function == 'calculate_discount' else 'validation'}.md"),
            test_kind="unit", involved_functions=(function,)))
        mappings.append(m.TestCaseMapping(test_function=f"test_{name}", scenario_ids=(name,)))
        call = f"{function}({', '.join(repr(arg) for arg in args)})"
        body = f"    with pytest.raises({exception}):\n        {call}\n" if exception else f"    assert {call} == {expected!r}\n"
        functions.append(f"def test_{name}():\n{body}")
    # Keep the same support imports in both feedback rounds to preserve passing tests.
    return m.GeneratedTests(
        plan=m.TestPlan(schema_version="1.1", target_functions=(function,),
            oracle_mode="specification", entry_functions=(function,),
            strategy=(m.StrategyDecision(subject_kind="function", function_name=function,
                                        decision="unit_test", reason="Explicit behavior specification"),),
            scenarios=tuple(scenarios)),
        code=f"import pytest\nfrom subject import {function}\n\n" + "\n".join(functions),
        mappings=tuple(mappings))


def main():
    directory = Path(__file__).resolve().parents[1] / "examples" / "fixtures"
    directory.mkdir(exist_ok=True)
    normal = [("score_pass", 60, "pass", None)]
    complete = normal + [("score_zero", 0, "fail", None), ("score_59", 59, "fail", None),
        ("score_89", 89, "pass", None), ("score_90", 90, "excellent", None),
        ("score_100", 100, "excellent", None), ("score_negative", -1, None, "ValueError"),
        ("score_above", 101, None, "ValueError"), ("score_bool", True, None, "TypeError"),
        ("score_float", 60.0, None, "TypeError"), ("score_string", "60", None, "TypeError"),
        ("score_none", None, None, "TypeError")]
    fixtures = {
        "calculation_complete.json": build("calculate_total", [
            ("total_normal", (12, 3), 36, None), ("total_zero_price", (0, 5), 0, None),
            ("total_zero_quantity", (12, 0), 0, None)], binary=True),
        "validation_round_01.json": build("classify_score", normal),
        "validation_round_02.json": build("classify_score", complete),
        "discount_defect.json": build("calculate_discount", [("discount_at_100", 100, 90, None)]),
    }
    for name, generated in fixtures.items():
        (directory / name).write_text(generated.canonical_json(indent=2) + "\n", encoding="utf-8")
    print(f"Saved {len(fixtures)} offline GeneratedTests fixtures.")


if __name__ == "__main__":
    main()
