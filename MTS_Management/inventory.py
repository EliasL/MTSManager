"""Indexed run discovery and bounded progress/storage inspection."""

import csv
from concurrent.futures import CancelledError
from collections import deque
import math
import os
from pathlib import Path
import shutil
import time

from .config import Config

from .files import file_digest, locked, now, read_json, write_json
from .inventory_cache import invalidate, publish, records
from .runner import process_token, attempt_status


def config_metadata_stamp():
    return [[str(p), p.stat().st_mtime_ns] for p in sorted((Path(__file__).parent.parent / "campaigns").rglob("metadata.json"))]


def progress(folder, config, limit=65536):
    path = Path(folder) / "macroData.csv"
    if not path.exists():
        return {"load": None, "fraction": None}
    with path.open("rb") as stream:
        first = stream.readline(limit)
        if not first.endswith(b"\n"):
            raise ValueError(f"Missing/big CSV header: {path}")
        header = next(csv.reader([first.decode().strip()]))
        if "load" not in header or len(set(header)) != len(header):
            raise ValueError(f"Invalid current CSV header: {path}")
        stream.seek(0, 2)
        offset = max(len(first), stream.tell() - limit)
        stream.seek(offset)
        if offset > len(first):
            stream.readline()  # discard the partial first row
        tail = stream.read(limit)
    lines = tail.split(b"\n")[:-1]  # ignore a row still being written
    for line in reversed(lines):
        if not line.strip() or line.startswith(b"#"):
            continue
        row = next(csv.reader([line.decode()]))
        if len(row) != len(header):
            raise ValueError(f"Malformed complete CSV row: {path}")
        values = dict(zip(header, row))
        load = float(values["load"])
        if not math.isfinite(load):
            raise ValueError(f"Non-finite load: {path}")
        fraction = ((load-config["startLoad"])/config["totalLoad"]
                    if config["totalLoad"] else 1.0)
        return {"load": load, "fraction": min(1.0, max(0.0, fraction)),
                "csv_modified": path.stat().st_mtime,
                "estimated_remaining": values.get("est_time_remaining")}
    return {"load": None, "fraction": None}


def estimate_size(folder, *, max_entries=10000, seconds=0.2, samples=5):
    """Count entries, stat a few files per directory/type, never follow symlinks.

    A truncated traversal reports only the covered portion. It is never labelled
    a total, and is not a statistical confidence bound.
    """
    started = time.monotonic()
    directories = deque([Path(folder)])
    groups = {}
    entries = 0
    complete = True
    while directories:
        directory = directories.popleft()
        with os.scandir(directory) as stream:
            for entry in stream:
                if entries >= max_entries or time.monotonic()-started >= seconds:
                    complete = False
                    break
                entries += 1
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    suffixes = Path(entry.name).suffixes
                    kind = "".join(suffixes[-2:]) if suffixes and suffixes[-1] in {".gz", ".xz", ".bz2"} else Path(entry.name).suffix
                    key = (str(directory), kind)
                    count, sizes = groups.setdefault(key, [0, []])
                    groups[key][0] = count + 1
                    if len(sizes) < samples:
                        sizes.append(entry.stat(follow_symlinks=False).st_size)
        if not complete:
            break
    estimate = sum(count * sum(sizes)/len(sizes) for count, sizes in groups.values())
    return {"estimated_bytes": round(estimate), "coverage_complete": complete,
            "entries_visited": entries, "files_sampled": sum(len(s) for _, s in groups.values()),
            "estimated_at": now(), "method": "mean of first files per directory/suffix"}


def rebuild_index(output_root):
    root = Path(output_root).resolve()
    with locked(root / ".prepare.lock"):
        index = {}
        for folder in root.iterdir():
            if not folder.is_dir() or not (folder / "manifest.json").exists():
                continue
            config = Config.read(folder / "config.conf", expected_name=folder.name)
            manifest = read_json(folder / "manifest.json")
            if manifest["config_hash"] != config.config_hash:
                raise ValueError(f"Manifest/config mismatch: {folder}")
            if config.config_hash in index:
                raise ValueError(f"Duplicate configuration directories: {folder}")
            index[config.config_hash] = folder.name
        write_json(root / ".run-index.json", index)
        invalidate(root)
    return index


def inventory(output_root, *, config_hash=None, config_hashes=None, refresh_sizes=False, hard_refresh=False, cancel=None):
    if config_hashes is not None:
        if config_hash is not None or not isinstance(config_hashes, list) or any(
                not isinstance(h, str) or len(h) != 64 or any(c not in "0123456789abcdef" for c in h)
                for h in config_hashes):
            raise ValueError("config_hashes requires a list of full configuration hashes, without config_hash")
        if not config_hashes:
            return []
    root = Path(output_root).resolve()
    if not root.exists():
        return []
    index_file = root / ".run-index.json"
    if not index_file.exists() or (hard_refresh and config_hash is None and config_hashes is None):
        rebuild_index(root)
    # Serialise refreshes, without blocking lifecycle invalidation or cached readers.
    with locked(root / ".inventory.lock", cancel=cancel):
        return _inventory(root, config_hash=config_hash, config_hashes=config_hashes, refresh_sizes=refresh_sizes,
                          hard_refresh=hard_refresh, cancel=cancel)


def _inventory(root, *, config_hash, config_hashes, refresh_sizes, hard_refresh, cancel):
    index = read_json(root / ".run-index.json")
    if config_hash and (len(config_hash) < 20 or any(c not in "0123456789abcdef" for c in config_hash)):
        raise ValueError("Use at least the complete 20-character configuration hash")
    matches = [h for h in index if not config_hash or h.startswith(config_hash)]
    if config_hash and len(matches) > 1:
        raise ValueError("Ambiguous configuration hash")
    if config_hashes is not None:
        requested = set(config_hashes)
        matches = [h for h in index if h in requested]
    cached = records(root, hashes=matches if config_hash or config_hashes is not None else None)
    result, updates, revisions = [], [], {}
    for h in matches:
        if cancel is not None and cancel.is_set():
            raise CancelledError("Inventory aborted")
        folder = root / index[h]
        revision, row = cached.get(h, (0, None))
        revisions[h] = revision
        if (not hard_refresh and not refresh_sizes and row is not None and row["folder"] == str(folder)
                and row["status"]["state"] == "finished" and not row["queued_continuations"]
                and row.get("settings_metadata") == config_metadata_stamp()):
            result.append(row)
            continue
        config = Config.read(folder / "config.conf", expected_name=folder.name)
        if config.config_hash != h:
            raise ValueError(f"Index/config mismatch: {folder}")
        requests = {attempt: read_json(attempt / "request.json") for attempt in (folder / "attempts").iterdir()}
        attempts = sorted(requests, key=lambda p: requests[p]["created_at"])
        statuses = {a: attempt_status(a) for a in attempts}
        active = [a for a in attempts if statuses[a]["state"] in {"running", "stopping"}]
        queued = [a for a in attempts if statuses[a]["state"] in {"prepared", "queued"}]
        attempt = (active[-1] if active else queued[0] if queued else attempts[-1]) if attempts else None
        request = requests[attempt] if attempt else {}
        status = statuses[attempt] if attempt else {"state": "unknown"}
        if status["state"] == "running" and "scheduler_id" not in status and "pid" in status:
            status["process_alive"] = bool(status.get("process_token")) and (
                process_token(status["pid"]) == status["process_token"])
        cache = folder / ".size-estimate.json"
        if refresh_sizes:
            write_json(cache, estimate_size(folder))
        row = {"config_hash": h, "folder": str(folder), "attempt": str(attempt) if attempt else None,
                       "settings_metadata": config_metadata_stamp(),
                       "executable": request.get("executable"), "build": request.get("build"),
                       "manifest_sha256": file_digest(folder / "manifest.json"),
                       "campaign": request.get("campaign"), "status": status,
                       "queued_continuations": len([a for a in queued if a != attempt]),
                       "attempts": [{"attempt": str(a), "status": statuses[a]} for a in attempts],
                       "termination": read_json(folder / "termination.json")["termination"]
                           if status["state"] == "finished" and (folder / "termination.json").exists() else None,
                       "progress": progress(folder, config),
                       "size": read_json(cache) if cache.exists() else None}
        result.append(row)
        updates.append(row)
    if cancel is not None and cancel.is_set():
        raise CancelledError("Inventory aborted")
    publish(root, revisions, updates, full=config_hash is None and config_hashes is None)
    return result


def resources(output_root):
    path = Path(output_root).resolve()
    while not path.exists():
        path = path.parent
    disk = shutil.disk_usage(path)
    memory = {}
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text().splitlines():
            key, value = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                memory[key] = int(value.split()[0])*1024
    return {"cpus": os.cpu_count(), "load_average": os.getloadavg(),
            "disk_free_bytes": disk.free, "disk_total_bytes": disk.total, "memory": memory}
