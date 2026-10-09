"""Validate and decode one Python source file without executing it."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import tokenize


MAX_SOURCE_BYTES = 128 * 1024
"""Default input ceiling for the deliberately small, single-file project scope."""


class SourceReadError(ValueError):
    """A safe, user-facing source input error."""

    def __init__(self, code: str, message: str, *, path: Path) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class LoadedSource:
    """Decoded source plus the encoding selected by Python's tokenizer rules."""

    path: Path
    code: str
    encoding: str
    size_bytes: int


def read_source(
    source_path: str | Path,
    *,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> LoadedSource:
    """Check and decode a ``.py`` file according to PEP 263.

    The byte limit is checked both from metadata and while reading.  The second
    check keeps the bound effective if the file changes between those steps.
    """

    path = Path(source_path)
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not path.exists():
        raise SourceReadError("path_not_found", f"源码文件不存在：{path}", path=path)
    if not path.is_file():
        raise SourceReadError("not_a_file", f"源码路径不是文件：{path}", path=path)
    if path.suffix.casefold() != ".py":
        raise SourceReadError("invalid_suffix", f"源码文件必须使用 .py 后缀：{path}", path=path)

    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise SourceReadError(
                "source_too_large",
                f"源码文件过大：{size} 字节，当前上限为 {max_bytes} 字节。",
                path=path,
            )
        with path.open("rb") as stream:
            raw = stream.read(max_bytes + 1)
    except SourceReadError:
        raise
    except OSError as error:
        raise SourceReadError(
            "read_failed", f"无法读取源码文件：{error.strerror or type(error).__name__}", path=path,
        ) from None

    if len(raw) > max_bytes:
        raise SourceReadError(
            "source_too_large",
            f"源码文件超过当前上限 {max_bytes} 字节。",
            path=path,
        )

    try:
        encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
        code = raw.decode(encoding)
    except (LookupError, SyntaxError, UnicodeDecodeError) as error:
        reason = _encoding_reason(error)
        raise SourceReadError(
            "invalid_encoding", f"无法按 Python 源码编码声明解码：{reason}", path=path,
        ) from None

    return LoadedSource(path=path, code=code, encoding=encoding, size_bytes=len(raw))


def _encoding_reason(error: Exception) -> str:
    """Return a concise reason without dumping source bytes into diagnostics."""

    if isinstance(error, UnicodeDecodeError):
        return f"{error.encoding} 在字节 {error.start} 处包含无效数据"
    if isinstance(error, LookupError):
        return "编码名称不受当前 Python 支持"
    return str(error).splitlines()[0]
