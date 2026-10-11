"""Atomic snapshots and verified references confined to one task directory."""

import hashlib
import os
from pathlib import Path
import tempfile
import time

from .contracts import ArtifactRef, relative_path


def atomic_write(path: Path, text: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers briefly hold handles that prevent replacement. Web
        # polling must not turn a transient sharing violation into a failed run.
        for attempt in range(8):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 7:
                    raise
                time.sleep(0.025)
    finally:
        Path(temporary).unlink(missing_ok=True)


def task_path(root: Path, reference: str, *, must_exist: bool = True) -> Path:
    relative_path(reference)
    root = root.resolve(strict=True)
    target = (root / reference).resolve(strict=must_exist)
    if not target.is_relative_to(root) or target == root:
        raise ValueError("artifact escapes the task directory")
    if must_exist and not target.is_file():
        raise ValueError("artifact must reference a file")
    return target


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_artifact(root: Path, artifact: ArtifactRef) -> Path:
    path = task_path(root, artifact.path)
    if file_digest(path) != artifact.sha256:
        raise ValueError("artifact content differs from its saved digest")
    return path
