"""Load historical contract fixtures using paths native to the test host.

Recorded drive paths are rebased together, including serialized state snapshots.
Production path validation and relative artifact references remain unchanged.
"""

import json
from pathlib import PureWindowsPath


def rebase_recorded_paths(value, root):
    if isinstance(value, dict):
        return {
            key: json.dumps(rebase_recorded_paths(json.loads(item), root), ensure_ascii=False)
            if key == "snapshot_json" else rebase_recorded_paths(item, root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [rebase_recorded_paths(item, root) for item in value]
    if isinstance(value, str):
        recorded = PureWindowsPath(value)
        if recorded.is_absolute() and recorded.drive.endswith(":"):
            return str((root / recorded.drive[:-1]).joinpath(*recorded.parts[1:]))
    return value


def load_example(path, model, root):
    value = rebase_recorded_paths(json.loads(path.read_text(encoding="utf-8")), root)
    return model.model_validate_json(json.dumps(value, ensure_ascii=False))
