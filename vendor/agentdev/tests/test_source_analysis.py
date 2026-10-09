"""C1 source reading and basic AST analysis stay bounded and execution-free."""

from pathlib import Path

import pytest

from testgen_agent.tools import (
    MAX_SOURCE_BYTES,
    SourceAnalysisError,
    SourceReadError,
    UnsupportedSourceError,
    analyze_source,
    read_source,
)


def write_source(tmp_path: Path, code: str, name: str = "subject.py") -> Path:
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return path


def test_read_source_supports_pep263_encoding_and_chinese(tmp_path):
    path = tmp_path / "legacy.py"
    path.write_bytes(
        '# -*- coding: gb18030 -*-\n\ndef greet(name="世界"):\n    return "你好，" + name\n'
        .encode("gb18030")
    )

    loaded = read_source(path)

    assert loaded.encoding == "gb18030"
    assert loaded.size_bytes == path.stat().st_size
    assert "你好" in loaded.code
    assert analyze_source(path).functions[0].signature == "(name='世界')"


@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        (lambda root: root / "missing.py", "path_not_found"),
        (lambda root: root, "not_a_file"),
        (lambda root: write_source(root, "x = 1\n", "subject.txt"), "invalid_suffix"),
    ],
)
def test_read_source_rejects_invalid_paths(tmp_path, prepare, code):
    path = prepare(tmp_path)

    with pytest.raises(SourceReadError) as captured:
        read_source(path)

    assert captured.value.code == code
    assert captured.value.path == path


def test_read_source_enforces_byte_limit(tmp_path):
    path = write_source(tmp_path, "#" * (MAX_SOURCE_BYTES + 1))

    with pytest.raises(SourceReadError, match="上限") as captured:
        read_source(path)

    assert captured.value.code == "source_too_large"


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_read_source_rejects_invalid_byte_limit(tmp_path, limit):
    path = write_source(tmp_path, "def f():\n    return 1\n")

    with pytest.raises(ValueError, match="positive integer"):
        read_source(path, max_bytes=limit)


def test_read_source_reports_invalid_declared_encoding_without_source_dump(tmp_path):
    path = tmp_path / "subject.py"
    path.write_bytes(b"# coding: no-such-codec\nsecret_value = 123\n")

    with pytest.raises(SourceReadError) as captured:
        read_source(path)

    assert captured.value.code == "invalid_encoding"
    assert "secret_value" not in str(captured.value)


def test_syntax_error_has_line_offset_and_concise_reason(tmp_path):
    path = write_source(tmp_path, "def broken(:\n    return 1\n")

    with pytest.raises(SourceAnalysisError) as captured:
        analyze_source(path)

    error = captured.value
    assert error.code == "syntax_error"
    assert error.line == 1
    assert error.offset is not None
    assert "第 1 行" in str(error)
    assert "broken" not in str(error)


def test_extracts_top_level_functions_signatures_defaults_docs_and_locations(tmp_path):
    path = write_source(
        tmp_path,
        '''"""模块说明。"""

def price(unit: int, quantity=2, /, *, discount: float = 0.0) -> float:
    """计算中文价格。"""
    return unit * quantity * (1 - discount)

def label(value, *parts, suffix="!", **metadata):
    return str(value) + suffix
''',
    )

    info = analyze_source(path)

    assert info.source_path == path
    assert [item.name for item in info.functions] == ["price", "label"]
    assert info.functions[0].signature == "(unit: int, quantity=2, /, *, discount: float=0.0)"
    assert info.functions[0].docstring == "计算中文价格。"
    assert (info.functions[0].start_line, info.functions[0].end_line) == (3, 5)
    assert info.functions[1].signature == "(value, *parts, suffix='!', **metadata)"
    assert info.functions[1].docstring is None


def test_extracts_main_branches_and_exception_paths_in_source_order(tmp_path):
    path = write_source(
        tmp_path,
        """def classify(value):
    if value is None:
        raise TypeError("value")
    for item in value:
        if item < 0:
            break
    while value:
        value = value[1:]
    match len(value):
        case 0:
            pass
    try:
        return value[0]
    except IndexError:
        raise ValueError("empty")
""",
    )

    info = analyze_source(path)

    assert [(item.kind, item.line) for item in info.branches] == [
        ("if", 2), ("raise", 3), ("loop", 4), ("if", 5),
        ("loop", 7), ("match", 9), ("try", 12), ("raise", 15),
    ]
    assert info.branches[0].source == "if value is None"
    assert info.branches[2].source == "for item in value"
    assert info.branches[-1].source == 'raise ValueError("empty")'


def test_nested_definition_is_not_a_target_and_its_branches_are_ignored(tmp_path):
    path = write_source(
        tmp_path,
        """def outer(value):
    def helper():
        if value:
            return 1
        return 0
    return helper()
""",
    )

    info = analyze_source(path)

    assert [item.name for item in info.functions] == ["outer"]
    assert info.branches == ()


def test_extracts_standard_library_imports_without_importing_or_executing_source(tmp_path):
    marker = tmp_path / "must-not-exist.txt"
    path = write_source(
        tmp_path,
        f"""import math as mathematics
from collections import Counter as Counts
raise RuntimeError("module body must not run")

def area(radius=1):
    open({str(marker)!r}, "w").write("executed")
    return mathematics.pi * radius ** 2
""",
    )

    info = analyze_source(path)

    assert [(item.module, item.names) for item in info.imports] == [
        (None, ("math",)), ("collections", ("Counter",)),
    ]
    assert not marker.exists()


@pytest.mark.parametrize(
    ("source", "code", "label"),
    [
        ("async def f():\n    return 1\n", "async_function", "异步函数"),
        ("from .helpers import value\n\ndef f():\n    return value\n", "relative_import", "相对导入"),
        ("import pandas\n\ndef f():\n    return 1\n", "external_dependency", "外部依赖 pandas"),
        ("from math import *\n\ndef f():\n    return pi\n", "wildcard_import", "通配符导入"),
        ("class Subject:\n    pass\n\ndef f():\n    return 1\n", "top_level_class", "顶层类"),
    ],
)
def test_reports_out_of_scope_structures(tmp_path, source, code, label):
    path = write_source(tmp_path, source)

    with pytest.raises(UnsupportedSourceError) as captured:
        analyze_source(path)

    assert captured.value.code == code
    assert captured.value.line == 1
    assert "当前不支持" in str(captured.value)
    assert label in str(captured.value)


def test_reports_no_target_function_instead_of_starting_generation(tmp_path):
    path = write_source(tmp_path, "VALUE = 42\n")

    with pytest.raises(SourceAnalysisError, match="不会进入测试生成循环") as captured:
        analyze_source(path)

    assert captured.value.code == "no_target_functions"


def test_rejects_more_than_five_target_functions(tmp_path):
    path = write_source(
        tmp_path,
        "\n".join(f"def f{number}():\n    return {number}\n" for number in range(6)),
    )

    with pytest.raises(UnsupportedSourceError, match="超过 5 个") as captured:
        analyze_source(path)

    assert captured.value.code == "too_many_functions"
    assert captured.value.line == 16


def test_project_examples_are_accepted():
    root = Path(__file__).parents[1]

    calculation = analyze_source(root / "examples" / "calculation.py")
    validation = analyze_source(root / "examples" / "validation.py")
    relationships = analyze_source(root / "examples" / "relationships.py")

    assert [item.name for item in calculation.functions] == ["calculate_total"]
    assert [(item.kind, item.line) for item in validation.branches] == [
        ("if", 10), ("raise", 11), ("if", 12), ("raise", 13), ("if", 14), ("if", 16),
    ]
    assert [item.name for item in relationships.functions] == ["calculate_total", "checkout"]
    assert [item.expression for item in relationships.call_sites] == [
        "calculate_total(unit_price, quantity)",
    ]
    assert [(item.caller, item.callee) for item in relationships.direct_calls] == [
        ("checkout", "calculate_total"),
    ]
    assert relationships.unresolved_calls == ()
