"""Host-owned candidate paths shared by generation and execution."""

from pathlib import Path


SUBJECT_FILENAME = "subject.py"
TEST_FILENAME = "test_subject.py"
ROUND_DIRECTORY = Path("rounds/01")


def round_directory(number: int) -> Path:
    if type(number) is not int:
        raise TypeError("round number must be an integer")
    if not 1 <= number <= 3:
        raise ValueError("round number must be in 1..3")
    return Path("rounds") / f"{number:02d}"
