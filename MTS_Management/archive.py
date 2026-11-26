"""Move inactive run directories within their data volume."""

from contextlib import ExitStack
import errno
import re
from pathlib import Path

from .errors import ExpectedError
from .files import attempt_directories, file_digest, locked, read_json, write_json
from .inventory_cache import invalidate
from .journal import Journal
from .runner import attempt_status, process_token, scheduler_state, SCHEDULER_TERMINAL


ARCHIVE_MARKER = ".mts-archive"


def archive_root(root):
    return root.with_name(root.name + "-archive")


def archived_runs(root, hashes=None):
    archive = archive_root(root)
    if not (archive / ARCHIVE_MARKER).is_file():
        return []
    prefixes = None if hashes is None else {h[:20] for h in hashes}
    rows = []
    for folder in sorted(archive.iterdir()):
        match = re.search(r"\[([0-9a-f]{20})\]$", folder.name)
        if not match or (prefixes is not None and match[1] not in prefixes):
            continue
        manifest = read_json(folder / "manifest.json")
        identity = manifest["config_hash"]
        if not identity.startswith(match[1]):
            raise ValueError(f"Archived run has inconsistent identity: {folder}")
        if hashes is None or identity in hashes:
            rows.append({"config_hash": identity, "folder": str(folder), "archived": True,
                         "campaign": manifest.get("campaign")})
    return rows


def move_folder(source, destination):
    if destination.parent.is_symlink():
        raise ExpectedError(f"Archive requires a physical directory: {destination.parent}")
    if source.stat().st_dev != destination.parent.parent.stat().st_dev:
        raise ExpectedError(f"Archive would leave the data volume: {destination}")
    if destination.parent.exists() and not (destination.parent / ARCHIVE_MARKER).is_file() and any(destination.parent.iterdir()):
        raise ExpectedError(f"Archive path already exists as an unmanaged directory: {destination.parent}")
    destination.parent.mkdir(exist_ok=True)
    (destination.parent / ARCHIVE_MARKER).touch()
    rename_folder(source, destination)


def rename_folder(source, destination):
    if source.is_symlink() or source.parent.is_symlink() or destination.parent.is_symlink():
        raise ExpectedError(f"Move requires physical directories: {source}")
    if not source.is_dir():
        raise ExpectedError(f"Folder is unavailable: {source}")
    if destination.exists() or destination.is_symlink():
        raise ExpectedError(f"Destination already exists: {destination}")
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise ExpectedError(f"Move would leave the data volume: {destination}")
    try:
        source.rename(destination)
    except OSError as error:
        if error.errno == errno.EXDEV:
            raise ExpectedError(f"Move would leave the data volume: {destination}") from error
        raise


def check_inactive(folder):
    scheduler_ids = set()
    for attempt in attempt_directories(folder / "attempts"):
        status = attempt_status(attempt)
        if status.get("scheduler_id"):
            scheduler_ids.add(status["scheduler_id"])
            # Historical worker records do not always expose live accounting.
            if (status.get("scheduler_state") or scheduler_state(status["scheduler_id"])) not in SCHEDULER_TERMINAL:
                raise ExpectedError(f"Wait until the scheduler confirms the run has stopped: {folder.name}")
        if status["state"] not in {"finished", "failed", "stopped"} or any(
                status.get(token) and process_token(status[pid]) == status[token]
                for pid, token in (("launcher_pid", "launcher_token"), ("runner_pid", "runner_token"),
                                   ("pid", "process_token"))):
            raise ExpectedError(f"Wait until all attempts have stopped: {folder.name}")
    return scheduler_ids


def archive_simulations(output_root, state_dir, selections, *, downloaded=False):
    root = Path(output_root).resolve()
    if not root.is_dir():
        raise ExpectedError("Output location is unavailable. Refresh Monitor data.")
    archive = archive_root(root)
    index_file = root / ".run-index.json"
    moved = []
    with locked(root / ".inventory.lock"), locked(root / ".prepare.lock"), ExitStack() as locks:
        index = read_json(index_file) if not downloaded and index_file.exists() else {}
        pairs = []
        scheduler_ids = set()
        for row in selections:
            source = Path(row["folder"])
            manifest = source / "manifest.json"
            identity = row["config_hash"]
            if (source.parent != root or source.is_symlink() or not manifest.is_file() or
                    file_digest(manifest) != row["manifest_sha256"] or read_json(manifest)["config_hash"] != identity or
                    (not downloaded and index.get(identity) != source.name)):
                raise ExpectedError("The run identity changed. Refresh Monitor data and select the run again.")
            if not downloaded:
                try:
                    locks.enter_context(locked(source / ".run.lock", blocking=False))
                except BlockingIOError as error:
                    raise ExpectedError(f"Wait until the run stops: {source.name}") from error
                scheduler_ids.update(check_inactive(source))
            target = archive / source.name
            if target.exists() or target.is_symlink():
                raise ExpectedError(f"Archive destination already exists: {target}")
            pairs.append((identity, source, target))
        if scheduler_ids:
            from .cluster import scheduler_jobs
            if any(job["job_id"] in scheduler_ids for job in scheduler_jobs()):
                raise ExpectedError("Wait until the selected allocations leave the scheduler queue.")
        try:
            for identity, source, target in pairs:
                if not downloaded:
                    invalidate(root, identity)
                move_folder(source, target)
                moved.append((source, target))
                if not downloaded:
                    del index[identity]
            if not downloaded:
                write_json(index_file, index)
        except Exception:
            for source, target in reversed(moved):
                target.rename(source)
            raise
        if not downloaded:
            journal = Journal(state_dir)
            for row, (identity, source, target) in zip(selections, pairs, strict=True):
                journal.append("archived", config_hash=identity, output_folder=str(source),
                               archive_folder=str(target), manifest_sha256=row["manifest_sha256"])
    return [str(target) for _, target in moved]


def restore_simulation(output_root, state_dir, row, *, downloaded=False):
    root = Path(output_root).resolve()
    original, archived = Path(row["output_folder"]), Path(row["archive_folder"])
    identity = row["config_hash"]
    if original.parent != root or archived != archive_root(root) / original.name:
        raise ExpectedError("The archive entry belongs to a different output location.")
    if not root.is_dir():
        raise ExpectedError(f"Restore requires the original output directory: {root}")
    manifest = archived / "manifest.json"
    index_file = root / ".run-index.json"
    if not downloaded and not index_file.exists():
        from .inventory import rebuild_index
        rebuild_index(root)
    with locked(root / ".inventory.lock"), locked(root / ".prepare.lock"):
        if (not manifest.is_file() or read_json(manifest)["config_hash"] != identity or
                (row.get("manifest_sha256") and file_digest(manifest) != row["manifest_sha256"])):
            raise ExpectedError(f"The archived run is unavailable or its identity changed: {archived}")
        if not downloaded:
            from .run_records import read_run
            config, _ = read_run(archived, expected_name=original.name)
            if config.config_hash != identity:
                raise ValueError(f"Archived configuration identity changed: {archived}")
            index = read_json(index_file) if index_file.exists() else {}
            if any(h.startswith(identity[:20]) and (h != identity or name != original.name)
                   for h, name in index.items()):
                raise ExpectedError("A run with this configuration identity is already registered in the output directory.")
            invalidate(root, identity)
        rename_folder(archived, original)
        if not downloaded:
            try:
                write_json(index_file, index | {identity: original.name})
            except Exception:
                original.rename(archived)
                raise
            Journal(state_dir).append("restored", config_hash=identity, output_folder=str(original),
                                      archive_folder=str(archived))
    return str(original)
