"""Append-only JSONL job history. One local writer lock, stable merge IDs."""

import json
import os
from pathlib import Path
import socket
import uuid

from .files import canonical, locked, now


class Journal:
    def __init__(self, state_dir):
        self.path = Path(state_dir).resolve() / "job-log.jsonl"

    def records(self):
        with locked(self.path.with_suffix(".lock")):
            return self._read()

    def _read(self):
        if not self.path.exists():
            return []
        # A malformed record is an error, not an invitation to discard history.
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def since(self, offset=0):
        """A byte cursor avoids retransmitting the full history on every refresh."""
        with locked(self.path.with_suffix(".lock")):
            if not self.path.exists():
                if offset:
                    raise ValueError("Journal disappeared after synchronization")
                return {"records": [], "next_offset": 0}
            with self.path.open("rb") as stream:
                if offset < 0 or offset > self.path.stat().st_size:
                    raise ValueError("Journal was truncated or invalid cursor supplied")
                stream.seek(offset)
                records = [json.loads(line) for line in stream]
                return {"records": records, "next_offset": stream.tell()}

    def merge(self, records):
        if not records:
            return
        with locked(self.path.with_suffix(".lock")):
            known = {r["event_id"]: r for r in self._read()}
            with self.path.open("ab") as stream:
                for record in records:
                    existing = known.get(record["event_id"])
                    if existing is not None and existing != record:
                        raise ValueError("Conflicting journal event IDs")
                    if existing is None:
                        stream.write(canonical(record) + b"\n")
                        known[record["event_id"]] = record
                stream.flush()
                os.fsync(stream.fileno())

    def append(self, event, **fields):
        record = fields | {"event": event, "event_id": uuid.uuid4().hex,
                           "timestamp": now(), "host": socket.gethostname()}
        with locked(self.path.with_suffix(".lock")):
            with self.path.open("ab") as stream:
                stream.write(canonical(record) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        return record
