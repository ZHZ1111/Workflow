"""Build deterministic execution fault candidates; no model or subprocess calls."""

from pathlib import Path

from test_workflow.agents.test_generation.core.models import GeneratedTests
from test_workflow.storage import atomic_write

ROOT = Path(__file__).resolve().parents[1]


def main():
    base = GeneratedTests.model_validate_json((ROOT / "examples/fixtures/calculation_complete.json").read_text(encoding="utf-8"))
    codes = {
        "ambiguous_assertion": base.code.replace("== 36", "== 37"),
        "name_error": base.code.replace("== 36", "== missing_expected"),
        "import_error": base.code.replace("import pytest", "import pytest\nfrom pytest import nonexistent_p3_symbol"),
        "collection_error": base.code.replace("def test_total_normal():", '@pytest.mark.parametrize("value", [1])\ndef test_total_normal():'),
        "no_tests_rejected": "# deliberately empty test module\n",
    }
    directory = ROOT / "examples/fixtures/p3"
    directory.mkdir(parents=True, exist_ok=True)
    for name, code in codes.items():
        atomic_write(directory / f"{name}.json", base.updated(code=code).canonical_json(indent=2) + "\n")


if __name__ == "__main__":
    main()
