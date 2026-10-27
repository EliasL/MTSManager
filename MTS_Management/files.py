"""Small file primitives shared by the runner, journal and inventory."""

from contextlib import contextmanager
from concurrent.futures import CancelledError
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def now():
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def canonical(value):
    # Python-only canonical format v1; deliberately not claimed to be RFC 8785.
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    """Atomically publish a complete JSON document, including on shared storage."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def locked(path, *, blocking=True, cancel=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        while True:
            if cancel is not None and cancel.is_set():
                raise CancelledError("Inventory aborted while waiting for another refresh")
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | (fcntl.LOCK_NB if not blocking or cancel is not None else 0))
                break
            except BlockingIOError:
                if cancel is None or not blocking:
                    raise
                cancel.wait(0.1)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
