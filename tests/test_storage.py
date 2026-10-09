import json
from unittest.mock import patch

import pytest

from test_workflow.events import Event, EventWriter
from test_workflow.state import StateStore
from test_workflow.storage import atomic_write, task_path


def test_atomic_save_interruption_preserves_readable_state(initial):
    store = StateStore(initial.run_dir)
    before = store.path.read_bytes()
    with patch("test_workflow.storage.os.replace", side_effect=OSError("replace interrupted")):
        with pytest.raises(OSError):
            store.save(initial.updated(current_round=1))
    assert store.path.read_bytes() == before
    assert store.load() == initial
    assert not list(initial.run_dir.glob(".state.json.*.tmp"))


def test_event_sequence_survives_writer_reopen(initial):
    first = EventWriter(initial.run_dir, initial.run_id)
    first.append("workflow_started")
    first.append("node_started", node="test_generation", round=1)
    reopened = EventWriter(initial.run_dir, initial.run_id)
    reopened.append("node_finished", node="test_generation", round=1)
    rows = [Event.model_validate_json(line) for line in first.path.read_text(encoding="utf-8").splitlines()]
    assert [row.seq for row in rows] == [1, 2, 3]
    assert {row.run_id for row in rows} == {initial.run_id}
    with pytest.raises(ValueError):
        EventWriter(initial.run_dir, "foreign-run")
    row = json.loads(rows[-1].canonical_json())
    row["seq"] = 9
    atomic_write(first.path, json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        EventWriter(initial.run_dir, initial.run_id)


def test_path_resolver_rejects_junction_escape(initial, tmp_path):
    # Windows directory junctions do not require symlink creation privileges.
    import os
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.json").write_text("{}", encoding="utf-8")
    link = initial.run_dir / "linked"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        task_path(initial.run_dir, "linked/file.json")


def test_load_rejects_foreign_state_directory(initial, tmp_path):
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    atomic_write(wrong / "state.json", initial.canonical_json())
    with pytest.raises(ValueError):
        StateStore(wrong).load()
