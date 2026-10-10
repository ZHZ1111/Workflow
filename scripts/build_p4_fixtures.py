"""Build deterministic P4 control-flow responses from existing local fixtures."""

import copy
import json
from pathlib import Path

from testgen_agent.models import GeneratedTests

ROOT = Path(__file__).resolve().parents[1]


def main():
    target = ROOT / "examples/fixtures/p4"
    target.mkdir(parents=True, exist_ok=True)
    base = json.loads((ROOT / "examples/fixtures/validation_round_01.json").read_text(encoding="utf-8"))
    for name, body in (
        ("no_coverage_progress", "assert classify_score(60) == 'pass'"),
        ("later_name_error", "assert classify_score(60) == undefined_expected_score"),
    ):
        value = copy.deepcopy(base)
        scenario = copy.deepcopy(value["plan"]["scenarios"][0])
        scenario["scenario_id"] = "score_pass_again"
        value["plan"]["scenarios"].append(scenario)
        value["mappings"].append({"test_function": "test_score_pass_again", "scenario_ids": ["score_pass_again"]})
        value["code"] += f"\ndef test_score_pass_again():\n    {body}\n"
        generated = GeneratedTests.model_validate(value)
        (target / f"{name}.json").write_text(generated.canonical_json(indent=2) + "\n", encoding="utf-8")
    invalid = json.loads((ROOT / "examples/fixtures/calculation_invalid_syntax.json").read_text(encoding="utf-8"))
    for number in (2, 3):
        value = copy.deepcopy(invalid)
        value["code"] += f"\n# distinct invalid revision {number}\n"
        generated = GeneratedTests.model_validate(value)
        (target / f"invalid_round_{number:02d}.json").write_text(generated.canonical_json(indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
