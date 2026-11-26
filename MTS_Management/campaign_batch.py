"""Submit a campaign on its execution host and collect durable job receipts."""

from dataclasses import asdict
import fcntl
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4

from .campaign import install_campaign_files
from .cluster import Site, archive_binary, call, paths, require_available_output, sync_journals
from .errors import ExpectedError
from .files import digest, locked, read_json, write_json
from .journal import Journal
from .submission import campaign_plan, submit_member


def _directory(site, batch):
    if not re.fullmatch(r"[0-9a-f]{32}", batch):
        raise ValueError("Invalid campaign batch ID")
    return Path(paths(site)["output_root"]) / ".campaign-submissions" / batch


def batch_status(site, batch):
    directory = _directory(site, batch)
    path = directory / "status.json"
    if not path.is_file():
        return {"state": "missing"}
    status = read_json(path)
    if status["state"] == "running":
        try:
            with locked(directory / "worker.lock", blocking=False):
                status = read_json(path)
                if status["state"] == "running":
                    status.update(state="failed", error="The remote submission worker stopped unexpectedly.")
                    write_json(path, status)
        except BlockingIOError:
            return status
    return status | {"receipts": Journal(directory).records(), "log": str(directory / "worker.log")}


def start_batch(site, batch, records, chain):
    require_available_output(site, paths(site))
    directory = _directory(site, batch)
    # Serialize start requests, including retries from a client that missed its reply.
    with locked(directory.parent / "start.lock"):
        status = batch_status(site, batch)
        if status["state"] != "missing":
            return status
        campaign, entries, _ = campaign_plan(site, records, chain)
        with (directory.parent / (digest(campaign) + ".lock")).open("a") as campaign_lease:
            try:
                fcntl.flock(campaign_lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ExpectedError("A remote worker is already submitting this campaign.") from error
            directory.mkdir(exist_ok=True)
            with (directory / "worker.lock").open("a") as worker_lease:
                fcntl.flock(worker_lease, fcntl.LOCK_EX)
                write_json(directory / "request.json", {"site": asdict(site), "records": records, "chain": chain})
                status = {"state": "running", "total": len(entries), "checked": 0, "accepted": 0,
                          "skipped": 0, "conflicts": 0}
                write_json(directory / "status.json", status)
                root = str(Path(__file__).resolve().parent.parent)
                python_path = os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH"))))
                try:
                    with (directory / "worker.log").open("w") as log:
                        process = subprocess.Popen(
                            [sys.executable, "-P", "-u", "-m", __name__, str(directory)],
                            env=os.environ | {"PYTHONPATH": python_path}, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            pass_fds=(campaign_lease.fileno(), worker_lease.fileno()),
                        )
                except OSError as error:
                    status.update(state="failed", error=str(error))
                    write_json(directory / "status.json", status)
                    return status | {"receipts": [], "log": str(directory / "worker.log")}
                threading.Thread(target=process.wait, daemon=True).start()
    return status


def _run(directory):
    path = directory / "status.json"
    status = read_json(path)
    try:
        request = read_json(directory / "request.json")
        site, records, chain = Site(**request["site"]), request["records"], request["chain"]
        campaign, entries, resources = campaign_plan(site, records, chain)
        install_campaign_files([records])
        for config, label in entries:
            status["label"] = label
            receipt = submit_member(site, campaign, config, label, resources,
                                    state_dir=directory, chain=chain, log=print, on_site=True)
            status["checked"] += 1
            if receipt.get("skipped"):
                status["skipped"] += 1
            elif receipt.get("conflict"):
                status["conflicts"] += 1
            else:
                status["accepted"] += len(receipt.get("jobs", [receipt]))
            write_json(path, status)
        status["state"] = "complete"
    except Exception as error:
        traceback.print_exc()
        status.update(state="failed", error=str(error))
    write_json(path, status)


def submit_remote_campaign(site, records, campaign, *, state_dir, chain, log=None, report_progress=None):
    journal = Journal(state_dir)
    scope = {"campaign": campaign, "site": site.name, "requested_output_root": site.output_root}
    with locked(Path(state_dir) / ".campaign-client.lock"):
        history = journal.records()
        finished = {entry["submission_id"] for entry in history if entry["event"] == "campaign_batch_finished"}
        request = next((entry for entry in reversed(history)
                        if entry["event"] == "submission_requested" and entry.get("batch")
                        and entry["event_id"] not in finished
                        and all(entry.get(key) == value for key, value in scope.items())), None)
        if request is None:
            request = journal.append("submission_requested", **scope, batch=uuid4().hex)
    batch = request["batch"]
    try:
        status = call(site, "campaign_batch_start", batch=batch, records=records, chain=chain)
    except (ConnectionError, subprocess.TimeoutExpired) as error:
        raise ExpectedError("Could not confirm that the remote batch started. "
                            "Use Run campaign again to reconnect to the same batch.") from error
    except (ExpectedError, RuntimeError) as error:
        if isinstance(error, ExpectedError) or "Unknown remote operation: campaign_batch_start" in str(error):
            journal.append("submission_rejected", **scope, submission_id=request["event_id"])
            journal.append("campaign_batch_finished", **scope, submission_id=request["event_id"])
            if not isinstance(error, ExpectedError):
                raise ExpectedError(f"Deploy the current manager for {site.ssh.upper()} in SSH connections before submitting.") from error
        raise
    failures = 0
    previous = None
    while status["state"] == "running":
        progress = (status["checked"], status["accepted"], status["skipped"], status["conflicts"])
        if report_progress and progress != previous:
            report_progress(_progress_message(site.name, status["total"], *progress))
            previous = progress
        time.sleep(8)
        try:
            status = call(site, "campaign_batch_status", batch=batch)
            failures = 0
        except (ConnectionError, subprocess.TimeoutExpired) as error:
            failures += 1
            if failures == 3:
                raise ExpectedError("Lost contact with the remote batch. It can continue submitting on the server. "
                                    "Use Run campaign again to reconnect to the same batch.") from error
    if status["state"] == "missing":
        raise ExpectedError("The remote batch record is unavailable. Check the execution site before retrying.")
    receipts = status["receipts"]
    journal.merge(receipts)
    result = _submission_result(campaign, receipts)
    if not result["jobs"] and not any(entry["event"] == "submission_error" for entry in receipts):
        # A worker may stop between logging a request and recording its outcome.
        resolved = {entry.get("submission_id") for entry in receipts if entry["event"] != "submission_requested"}
        if all(entry["event_id"] in resolved for entry in receipts if entry["event"] == "submission_requested"):
            journal.append("submission_rejected", **scope, submission_id=request["event_id"])
    journal.append("campaign_batch_finished", **scope, submission_id=request["event_id"])
    if report_progress:
        accepted, skipped, conflicts = (len(result[key]) for key in ("jobs", "skipped", "conflicts"))
        report_progress(_progress_message(site.name, status["total"], status["checked"],
                                          accepted, skipped, conflicts))
    if result["jobs"]:
        sync_journals([site], state_dir)
        # Many jobs refer to the same retained executable. Archive it once.
        binaries = {job["executable"]: job for job in result["jobs"] if job.get("executable")}
        for job in binaries.values():
            try:
                archive_binary(site, job, campaign, state_dir)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                detail = f"Could not archive the retained executable locally. {error}"
                result.setdefault("errors", {})[job["executable"]] = detail
                if log:
                    log(detail)
    if status["state"] == "failed":
        raise ExpectedError(f"{site.name.upper()}: remote submission stopped after accepting {len(result['jobs']):,} jobs. "
                            f"{status['error']} See {status['log']} and verify any uncertain jobs before retrying.")
    return result


def _progress_message(name, total, checked, accepted, skipped, conflicts):
    return (f"{name.upper()}: processed {checked:,}/{total:,} runs. "
            f"Accepted jobs: {accepted:,}. Already complete: {skipped:,}. Conflicts: {conflicts:,}.")


def _submission_result(campaign, receipts):
    result = {"campaign": campaign, "jobs": [], "skipped": [], "conflicts": []}
    requests = {entry["event_id"]: entry for entry in receipts if entry["event"] == "submission_requested"}
    for entry in receipts:
        event = entry["event"]
        if event not in {"submission_accepted", "submission_skipped", "submission_conflict"}:
            continue
        request = requests[entry["submission_id"]]
        member = {"label": request["label"], "config_hash": request["config_hash"]}
        if event == "submission_accepted":
            job = {key: value for key, value in entry.items()
                   if key not in {"event", "event_id", "timestamp", "host", "submission_id", "site", "campaign"}}
            result["jobs"].extend(item | member for item in job.get("jobs", [job]))
        elif event == "submission_skipped":
            result["skipped"].append(member)
        else:
            result["conflicts"].append(member | {"conflict": entry["conflict"]})
    return result


if __name__ == "__main__":
    _run(Path(sys.argv[1]))
