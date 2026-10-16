from pathlib import Path

import pytest

from contract_examples import load_example
from test_workflow.workflow import create_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def contract_example(tmp_path):
    """Shared loader for checked-in contracts recorded on another machine."""
    def load(path, model):
        return load_example(path, model, tmp_path)
    return load


@pytest.fixture
def initial(tmp_path):
    return create_run(source=ROOT / "examples/calculation.py",
                      specification=ROOT / "examples/calculation.md", output=tmp_path)
