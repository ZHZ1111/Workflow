"""Public entry point for TestGenerationAgent; load lazily to keep core contracts independent."""

__all__ = ["TestGenerationAgent"]


def __getattr__(name):
    if name == "TestGenerationAgent":
        from .agent import TestGenerationAgent
        return TestGenerationAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
