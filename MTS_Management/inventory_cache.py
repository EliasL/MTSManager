"""Execution-host inventory storage and lifecycle invalidation."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .files import locked, now, read_json, write_json


@contextmanager
def database(output_root):
    connection = sqlite3.connect(Path(output_root) / ".inventory.sqlite3", timeout=30)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS runs "
                               "(hash TEXT PRIMARY KEY, revision INTEGER NOT NULL, data TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        yield connection
    finally:
        connection.close()


def invalidate(output_root, config_hash=None):
    """Invalidate before changing a completed run; refreshes check the revision."""
    with database(output_root) as connection, connection:
        if config_hash is None:
            connection.execute("UPDATE runs SET revision = revision + 1, data = NULL")
        else:
            connection.execute("INSERT INTO runs VALUES (?, 1, NULL) ON CONFLICT(hash) DO UPDATE "
                               "SET revision = runs.revision + 1, data = NULL", (config_hash,))


def records(output_root, *, hashes=None):
    with database(output_root) as connection:
        query = "SELECT hash, revision, data FROM runs"
        rows = (connection.execute(query) if hashes is None else
                (row for h in hashes for row in connection.execute(query + " WHERE hash = ?", (h,))))
        return {h: (revision, json.loads(data) if data is not None else None)
                for h, revision, data in rows}


def publish(output_root, revisions, updates, *, full=False):
    """Publish atomically; a concurrent restart must not resurrect old results."""
    with database(output_root) as connection, connection:
        # Acquire the write lock before checking revisions.
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('updated_at', NULL)")
        current = dict(connection.execute("SELECT hash, revision FROM runs"))
        if any(current.get(h, 0) != revision for h, revision in revisions.items()):
            raise RuntimeError("A run changed during inventory. Refresh again; cached results were not replaced.")
        for row in updates:
            h = row["config_hash"]
            connection.execute("INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(hash) DO UPDATE SET data = excluded.data",
                               (h, revisions[h], json.dumps(row, allow_nan=False)))
        if full:
            connection.execute("INSERT OR REPLACE INTO metadata VALUES ('updated_at', ?)", (now(),))


def snapshot(output_root):
    root = Path(output_root).resolve()
    if not (root / ".inventory.sqlite3").exists():
        return {"output_root": str(root), "updated_at": None, "rows": [], "pending": []}
    with database(root) as connection, connection:
        connection.execute("BEGIN")
        updated = connection.execute("SELECT value FROM metadata WHERE key = 'updated_at'").fetchone()
        entries = list(connection.execute("SELECT hash, data FROM runs ORDER BY hash"))
    index = root / ".run-index.json"
    if index.exists():
        known = read_json(index)
        entries = [(h, data) for h, data in entries if h in known]
    return {"output_root": str(root), "updated_at": updated[0] if updated else None,
            "rows": [json.loads(data) for _, data in entries if data is not None],
            "pending": [h for h, data in entries if data is None]}


def export(output_root):
    """Export a complete document, safe to transfer instead of a live SQLite file."""
    root = Path(output_root).resolve()
    path = root / ".inventory.json"
    with locked(root / ".inventory.export.lock"):
        value = snapshot(root)
        write_json(path, value)
    return {"snapshot": str(path), "updated_at": value["updated_at"],
            "runs": len(value["rows"]), "pending": len(value["pending"])}
