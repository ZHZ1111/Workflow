"""Public entry point for TestQualityReviewAgent; load lazily to keep core contracts independent."""

__all__ = ["TestQualityReviewAgent"]


def __getattr__(name):
    if name == "TestQualityReviewAgent":
        from .agent import TestQualityReviewAgent
        return TestQualityReviewAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
