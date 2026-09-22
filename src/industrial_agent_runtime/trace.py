"""Durable per-run JSONL events and content-addressed projection artifacts.

This is a single-writer recorder, not a checkpoint/resume framework. The caller
owns its run directory. Existing artifacts are verified and never overwritten.
"""

from pathlib import Path
import hashlib
import json
import os

from .contracts import ContextProjection, InformationRef, TraceEvent, Visibility
from .serialization import canonical_json


class TraceRecorder:
    def __init__(self, run_directory: str | Path) -> None:
        self.directory = Path(run_directory)
        self.artifacts = self.directory / "projections"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.events_path = self.directory / "events.jsonl"

    def persist_projection(self, projection: ContextProjection,
                           created_at: str) -> InformationRef:
        """Persist the complete immutable projection, not a display summary."""
        payload = canonical_json(projection).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        path = self.artifacts / f"{digest}.json"
        try:
            with path.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != payload:
                raise ValueError("Existing projection artifact is corrupted")
        return InformationRef(
            ref_id=f"projections/{digest}.json", kind="ContextProjection",
            owner="industrial-agent-runtime", version="v0",
            checksum=digest, visibility=Visibility.INTERNAL, created_at=created_at,
        )

    def read_projection(self, ref: InformationRef) -> dict:
        """Resolve only this recorder's content-addressed projection references."""
        digest = ref.checksum
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or ref.ref_id != f"projections/{digest}.json"
                or ref.kind != "ContextProjection"
                or ref.owner != "industrial-agent-runtime"):
            raise ValueError("Invalid projection reference")
        payload = (self.artifacts / f"{digest}.json").read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("Projection checksum mismatch")
        return json.loads(payload)

    def append(self, event: TraceEvent) -> None:
        if event.type == "MODEL_TURN":
            if event.context_projection_ref is None:
                raise ValueError("Missing projection reference")
            projection = self.read_projection(event.context_projection_ref)
            if projection["task_id"] != event.task_id:
                raise ValueError("Projection belongs to a different task")
        with self.events_path.open("ab") as stream:
            stream.write((canonical_json(event) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())

    def read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text(
            encoding="utf-8").splitlines()]
