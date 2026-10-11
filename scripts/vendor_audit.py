"""Verify the captured dependency against its original hashes and documented patches."""

import hashlib
import json
from pathlib import Path


def audit_vendor(root: Path) -> dict:
    manifest = json.loads((root / "evaluation/reuse_manifest.json").read_text(encoding="utf-8"))
    patches = json.loads((root / "evaluation/vendor_patches.json").read_text(encoding="utf-8"))
    expected = {item["path"]: item for item in patches["patches"]}
    unchanged = verified = True
    for entry in manifest["files"]:
        digest = hashlib.sha256((root / "vendor/agentdev" / entry["path"]).read_bytes()).hexdigest()
        unchanged = unchanged and digest == entry["sha256"]
        override = expected.get(entry["path"])
        allowed = override["sha256"] if override and override["original_sha256"] == entry["sha256"] else entry["sha256"]
        verified = verified and digest == allowed
    verified = verified and set(expected).issubset({item["path"] for item in manifest["files"]})
    return {"unchanged": unchanged, "verified": verified, "files": len(manifest["files"]), "patches": list(expected)}
