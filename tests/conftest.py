from pathlib import Path

import pytest

from test_workflow.workflow import create_run

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def initial(tmp_path):
    return create_run(source=ROOT / "examples/calculation.py",
                      specification=ROOT / "examples/calculation.md", output=tmp_path)
