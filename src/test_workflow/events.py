"""Single-writer, monotonically numbered JSONL journals.

Events have fixed public fields, never arbitrary exception text or configuration.
Messages use typed public payloads, not authentication or model client objects.
"""

from datetime import datetime
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator
from testgen_agent.models import ContractModel, RoundNumber

from .contracts import Message, StableId, relative_path
from .state import utc_now


class Event(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    seq: Annotated[StrictInt, Field(ge=1)]
    run_id: StableId
    type: Literal["workflow_started", "node_started", "node_output_saved", "message_sent", "message_consumed", "node_finished", "node_failed", "workflow_stopped", "workflow_finished", "workflow_error", "route_selected"]
    node: StableId | None = None
    round: RoundNumber | None = None
    message_id: StableId | None = None
    error_code: StableId | None = None
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    action: StableId | None = None
    stop_reason: StableId | None = None
    candidate_id: str | None = None
    context_path: str | None = None
    result_path: str | None = None
    artifact_path: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("context_path", "result_path", "artifact_path")
    @classmethod
    def task_relative(cls, value):
        return relative_path(value) if value is not None else None


def append_jsonl(path: Path, record: ContractModel) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(record.canonical_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class EventWriter:
    def __init__(self, run_dir: Path, run_id: str):
        self.path = run_dir / "events.jsonl"
        self.run_id = run_id
        self.seq = 0
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                event = Event.model_validate_json(line)
                if event.run_id != run_id or event.seq != self.seq + 1:
                    raise ValueError("event journal has a foreign task or nonconsecutive sequence")
                self.seq = event.seq

    def append(self, event_type: str, **fields) -> Event:
        event = Event(seq=self.seq + 1, run_id=self.run_id, type=event_type, **fields)
        append_jsonl(self.path, event)
        self.seq = event.seq
        return event


class MessageWriter:
    def __init__(self, run_dir: Path):
        self.path = run_dir / "messages.jsonl"
        # Empty/rejected candidates still have an explicit, readable journal.
        if not self.path.exists():
            self.path.touch(exist_ok=False)

    def append(self, message: Message) -> None:
        append_jsonl(self.path, message)
