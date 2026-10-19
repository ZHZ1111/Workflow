"""Public entry point for TestExecutionAgent; load lazily to keep core contracts independent."""

__all__ = ["TestExecutionAgent"]


def __getattr__(name):
    if name == "TestExecutionAgent":
        from .agent import TestExecutionAgent
        return TestExecutionAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
