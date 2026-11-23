"""Request or collect a complete monitor snapshot on the execution host."""

import fcntl
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4

from .files import digest, read_json, write_json
from .inventory import inventory, validate_config_hashes
from .inventory_cache import records


def _read(path):
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def _workload(root, hashes, snapshot):
    index = _read(root / ".run-index.json")
    if index is None:
        return snapshot["workload"] if snapshot else {}
    selected = list(index) if hashes is None else [h for h in hashes if h in index]
    cached = records(root, hashes=selected if hashes is not None else None)
    sized = []
    for h in selected:
        row = cached.get(h, (0, None))[1] or {}
        size = row.get("size")
        if size and size.get("coverage_complete") and not row.get("missing"):
            sized.append(size["estimated_bytes"])
    return {"simulations": len(selected), "sized_simulations": len(sized), "size_bytes": sum(sized)}


def _estimate(workload, snapshot, hard_refresh):
    count = workload.get("simulations")
    if snapshot and snapshot["hard_refresh"] == hard_refresh:
        previous_count = snapshot["workload"].get("simulations")
        scale = count / previous_count if count is not None and previous_count else 1
        return max(10, snapshot["elapsed_seconds"] * scale * 1.25)
    # MAGI's 2,001-run scans took 26–47 s on success and later exceeded 60 s.
    return 10 + (count if count is not None else 2000) * (0.10 if hard_refresh else 0.05)


def request_refresh(output_root, *, known_revision=None, known_failure=None, minimum_scan_start=None,
                    config_hashes=None, hard_refresh=False):
    """Collect a newer snapshot or start one scan for this output root."""
    if config_hashes is not None:
        validate_config_hashes(config_hashes)
        config_hashes = sorted(set(config_hashes))
    scope = "all" if config_hashes is None else digest(config_hashes)
    root = Path(output_root)
    directory = root / ".inventory-refresh"
    directory.mkdir(parents=True, exist_ok=True)
    status_path = directory / "worker.json"
    # The child inherits this open file description. Closing the parent's copy
    # leaves the lease held until the child exits, including after SSH exits.
    with (directory / "worker.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            busy = True
        else:
            busy = False
        snapshot = _read(directory / f"{scope}.json")
        if (snapshot and snapshot["revision"] != known_revision
                and (not hard_refresh or snapshot["hard_refresh"])
                and (minimum_scan_start is None or snapshot["started_at"] >= minimum_scan_start)):
            return {"state": "ready", "snapshot": snapshot}
        worker = _read(status_path)
        if busy:
            return _receipt("running", worker, scope)
        if (worker and worker["scope"] == scope and worker["revision"] != known_failure
                and worker["state"] in {"running", "failed"}
                and (snapshot is None or worker["revision"] != snapshot["revision"])):
            error = worker.get("error", "The inventory worker exited before publishing its snapshot.")
            return {"state": "failed", "revision": worker["revision"],
                    "error": f"{error} See {directory / (scope + '.log')} on the execution host."}

        workload = _workload(root, config_hashes, snapshot)
        worker = {"revision": uuid4().hex, "scope": scope, "config_hashes": config_hashes,
                  "hard_refresh": hard_refresh, "state": "running", "started_at": time.time(),
                  "workload": workload, "estimated_seconds": _estimate(workload, snapshot, hard_refresh),
                  "phase": "Preparing run index"}
        package_root = str(Path(__file__).resolve().parent.parent)
        python_path = os.pathsep.join(filter(None, (package_root, os.environ.get("PYTHONPATH"))))
        with (directory / f"{scope}.log").open("w") as log:
            write_json(status_path, worker)
            try:
                process = subprocess.Popen(
                    [sys.executable, "-P", "-u", "-m", __name__, str(root)],
                    env=os.environ | {"PYTHONPATH": python_path}, pass_fds=(lease.fileno(),),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                )
            except OSError as error:
                write_json(status_path, worker | {"state": "failed", "error": str(error)})
                raise
        threading.Thread(target=process.wait, daemon=True).start()
        return _receipt("started", worker, scope)


def _receipt(state, worker, scope):
    if worker is None or worker["state"] != "running":
        return {"state": state, "phase": "Preparing refresh"}
    elapsed = max(0, time.time() - worker["started_at"])
    return {"state": state, "same_scope": worker["scope"] == scope,
            "workload": worker["workload"], "phase": worker["phase"],
            "checked": worker.get("checked"), "total": worker.get("total"),
            "remaining_seconds": max(0, worker["estimated_seconds"] - elapsed)}


def _run(output_root):
    directory = Path(output_root) / ".inventory-refresh"
    status_path = directory / "worker.json"
    worker = read_json(status_path)
    started = time.monotonic()
    last_report = started

    def report(phase, checked=None, total=None):
        nonlocal last_report
        current = time.monotonic()
        if phase != worker["phase"] or current - last_report >= 1:
            worker.update(phase=phase, checked=checked, total=total)
            write_json(status_path, worker)
            last_report = current

    try:
        rows = inventory(output_root, config_hashes=worker["config_hashes"],
                         hard_refresh=worker["hard_refresh"], include_archived=True, report=report)
        report("Saving snapshot")
        active = [row for row in rows if not row.get("archived") and not row.get("missing")]
        sized = [row["size"]["estimated_bytes"] for row in active
                 if row.get("size") and row["size"].get("coverage_complete")]
        snapshot = {key: worker[key] for key in ("revision", "started_at", "hard_refresh")}
        snapshot.update(rows=rows, updated_at=time.time(), elapsed_seconds=time.monotonic() - started,
                        workload={"simulations": len(active), "sized_simulations": len(sized),
                                  "size_bytes": sum(sized)})
        write_json(directory / f"{worker['scope']}.json", snapshot)
        write_json(status_path, worker | {"state": "complete"})
    except Exception as error:
        traceback.print_exc()
        write_json(status_path, worker | {"state": "failed", "error": str(error)})
        raise SystemExit(1) from error


if __name__ == "__main__":
    _run(sys.argv[1])
