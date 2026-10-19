"""Verify original AgentDev provenance and its integrated source locations."""

import hashlib
import json
from pathlib import Path


def audit_vendor(root: Path) -> dict:
    root = root.resolve()
    manifest = json.loads((root / "evaluation/reuse_manifest.json").read_text(encoding="utf-8"))
    patches = json.loads((root / "evaluation/vendor_patches.json").read_text(encoding="utf-8"))
    expected = {item["path"]: item for item in patches["patches"]}
    layout_path = root / "evaluation/agentdev_layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8")) if layout_path.exists() else None
    relocated = {item["path"]: item for item in layout["files"]} if layout else {}
    unchanged = verified = True
    for entry in manifest["files"]:
        override = expected.get(entry["path"])
        allowed = override["sha256"] if override and override["original_sha256"] == entry["sha256"] else entry["sha256"]
        if layout:
            item = relocated.get(entry["path"])
            if item is None:
                verified = unchanged = False
                continue
            target = (root / item["current_path"]).resolve()
            if target == root or not target.is_relative_to(root):
                raise ValueError("relocated source must stay inside the project")
            data = target.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            matches = digest == item["sha256"]
            if not matches and layout.get("hash_normalization") == "utf8-lf":
                canonical = data.decode("utf-8").replace("\r\n", "\n").encode("utf-8")
                matches = hashlib.sha256(canonical).hexdigest() == item.get("normalized_sha256")
            verified = verified and item["before_sha256"] == allowed and matches
        else:
            digest = hashlib.sha256((root / "vendor/agentdev" / entry["path"]).read_bytes()).hexdigest()
            verified = verified and digest == allowed
        unchanged = unchanged and digest == entry["sha256"]
    verified = verified and set(expected).issubset({item["path"] for item in manifest["files"]})
    if layout:
        verified = (verified and len(relocated) == len(layout["files"]) == len(manifest["files"])
                    and set(relocated) == {item["path"] for item in manifest["files"]}
                    and len({item["current_path"] for item in relocated.values()}) == len(relocated))
    return {"unchanged": unchanged, "verified": verified, "files": len(manifest["files"]),
            "patches": list(expected), "layout": "integrated" if layout else "vendor"}
