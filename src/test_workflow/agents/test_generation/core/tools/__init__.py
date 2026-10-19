"""Bounded source analysis and host-prepared pytest execution tools."""

from .code_analyzer import (
    DataFlowHint,
    SourceAnalysisError,
    UnsupportedSourceError,
    analyze_loaded_source,
    analyze_source,
    build_call_graph,
    candidate_entry_functions,
    find_explicit_data_flows,
    find_call_cycles,
)
from .source_reader import LoadedSource, MAX_SOURCE_BYTES, SourceReadError, read_source
from .test_runner import ExecutionArtifactError, ExecutionWorkspaceError, run_tests
from .coverage_mapper import build_coverage_source_hints
from .coverage_runner import CoverageRunResult, parse_coverage_json, run_tests_with_coverage

__all__ = [
    "LoadedSource",
    "CoverageRunResult",
    "ExecutionArtifactError",
    "ExecutionWorkspaceError",
    "MAX_SOURCE_BYTES",
    "SourceAnalysisError",
    "SourceReadError",
    "UnsupportedSourceError",
    "DataFlowHint",
    "analyze_loaded_source",
    "analyze_source",
    "build_call_graph",
    "build_coverage_source_hints",
    "candidate_entry_functions",
    "find_explicit_data_flows",
    "find_call_cycles",
    "read_source",
    "run_tests",
    "run_tests_with_coverage",
    "parse_coverage_json",
]
