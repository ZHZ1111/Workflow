"""Copy an explicit AgentDev snapshot without credentials or runtime artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agentdev", type=Path, required=True)
    args = parser.parse_args()
    source = args.agentdev.resolve(strict=True)
    root = Path(__file__).resolve().parents[1]
    destination = root / "vendor" / "agentdev"
    if destination.exists():
        raise SystemExit("Snapshot already exists; inspect it before replacing anything.")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    tree = subprocess.check_output(["git", "ls-tree", "-r", "HEAD"], cwd=source, text=True)
    tracked = {line.split("\t", 1)[1]: line.split()[2] for line in tree.splitlines()}
    files = [source / name for name in ("pyproject.toml", "requirements.lock", "README.md", "Design.md", ".env.example")]
    for folder in ("src/testgen_agent", "tests", "examples"):
        files.extend(path for path in (source / folder).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and path.suffix in {".py", ".md", ".html", ".css", ".js"})
    files.extend(path for pattern in ("LICENSE*", "COPYING*", "NOTICE*") for path in source.glob(pattern) if path.is_file())
    manifest = []
    for path in sorted(set(files)):
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        data = target.read_bytes()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        committed_blob = tracked.get(relative.as_posix())
        manifest.append({"path": relative.as_posix(), "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest(),
                         "git_blob": blob, "head_blob": committed_blob,
                         "matches_head": blob == committed_blob if committed_blob else None})
    (root / "examples").mkdir(exist_ok=True)
    for name in ("calculation", "validation", "discount", "relationships"):
        for suffix in (".py", ".md"):
            shutil.copyfile(source / "examples" / (name + suffix), root / "examples" / (name + suffix))
    shutil.copyfile(source / "requirements.lock", root / "requirements.lock")
    shutil.copyfile(source / ".env.example", root / ".env.example")
    (root / "evaluation").mkdir(exist_ok=True)
    (root / "evaluation" / "reuse_manifest.json").write_text(json.dumps({
        "schema_version": "1.0", "captured_on": "2026-10-09", "source_project": "AgentDev",
        "source_repository": "https://github.com/ZHZ1111/AgentDev",
        "source_revision": revision, "snapshot": "vendor/agentdev", "files": manifest,
        "license_files": [item["path"] for item in manifest if Path(item["path"]).name.startswith(("LICENSE", "COPYING", "NOTICE"))],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"revision": revision, "copied_files": len(manifest),
                      "changed_from_head": [item["path"] for item in manifest if item["matches_head"] is False],
                      "untracked_in_head": [item["path"] for item in manifest if item["matches_head"] is None]}))


if __name__ == "__main__":
    main()
