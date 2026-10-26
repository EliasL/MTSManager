"""Submit a versioned benchmark locally on MAGI; receipts allow safe continuation."""

import argparse
from dataclasses import asdict, replace
import json
import os
from pathlib import Path

from MTS_Management.campaign import campaign_entries
from MTS_Management.cluster import load_sites, paths, serve
from MTS_Management.files import canonical, file_digest, locked, read_json, write_json
from MTS_Management.journal import Journal
from MTS_Management.provenance import executable_identity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--sites", type=Path, default=Path("sites.toml"))
    parser.add_argument("--site", default="local")
    parser.add_argument("--resume", action="store_true", help="Reuse confirmed submissions; never retry uncertain ones")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    name, entries = campaign_entries(args.campaign)
    plan = read_json(args.campaign.with_suffix(".resources.json"))
    if file_digest(args.campaign) != plan["campaign_sha256"]:
        raise ValueError("Campaign differs from its recorded resource plan")
    diagnostics = [label for config, label in entries if config["logDuringMinimization"]]
    if diagnostics != [plan["diagnostic_label"]] or len(entries) != plan["benchmark_jobs"] + 1:
        raise ValueError("Unexpected benchmark/diagnostic job matrix")
    for config, label in entries:
        if str(config["rows"]) not in plan["resources"]:
            raise ValueError(f"Missing resource request for {label}")
    print(json.dumps({"campaign": name, "jobs": len(entries), "resources": plan["resources"]}), flush=True)
    if args.dry_run:
        return
    site = load_sites(args.sites)[args.site]
    if site.ssh or site.mode != "slurm":
        raise ValueError("Run this controller on the deployed cluster with its native Slurm site")
    site = replace(site, name=plan["site"])
    p = paths(site)
    build = executable_identity(p["executable"])
    if build["revision"] != plan["simulator_revision"] or build["build_options"]["ENABLE_NATIVE_ARCH"] != "OFF":
        raise ValueError("Benchmark requires the recorded portable simulator build")
    state = Path(p["state_dir"]) / name
    receipts = state / "submissions.jsonl"
    progress = state / "submission.json"
    journal = Journal(p["state_dir"])
    with locked(state / ".submit.lock", blocking=False):
        if receipts.exists() and not args.resume:
            raise FileExistsError("Submission receipts exist; inspect them and use --resume")
        records = [json.loads(line) for line in receipts.read_text().splitlines()] if receipts.exists() else []
        known = {r["config_hash"]: r for r in records}
        if len(known) != len(records) or known.keys() - {c.config_hash for c, _ in entries}:
            raise ValueError("Duplicate or unexpected submission receipts")
        index_path = Path(p["output_root"]) / ".run-index.json"
        index = read_json(index_path) if index_path.exists() else {}
        if index and not args.resume:
            raise FileExistsError("Output root already contains runs; inspect them and use --resume")
        accepted = 0
        journal.append("campaign_submission_started", campaign=name, site=site.name, pid=os.getpid())
        try:
            with receipts.open("a") as output:
                for config, label in entries:
                    h = config.config_hash
                    if h in index:
                        folder = Path(p["output_root"]) / index[h]
                        manifest = read_json(folder / "manifest.json")
                        if (manifest["settings"] != config.as_dict() or
                                manifest["executable_sha256"] != build["executable_sha256"]):
                            raise ValueError(f"Existing run identity differs: {label}")
                        attempts = list((folder / "attempts").iterdir())
                        if len(attempts) != 1:
                            raise ValueError(f"Inspect unexpected attempts before continuing: {label}")
                        status = read_json(attempts[0] / "status.json")
                        if not status.get("scheduler_id"):
                            raise RuntimeError(f"Uncertain submission; inspect Slurm and logs: {label}")
                        result = {"attempt": str(attempts[0]), "output_folder": str(folder),
                                  "scheduler_id": status["scheduler_id"]}
                        if h in known and known[h]["scheduler_id"] != result["scheduler_id"]:
                            raise ValueError(f"Submission receipt differs: {label}")
                    else:
                        if h in known:
                            raise ValueError(f"Run disappeared after submission: {label}")
                        resources = site.slurm | plan["resources"][str(config["rows"])]
                        result = serve({"site": asdict(replace(site, slurm=resources)), "operation": "run",
                                        "options": {"settings": config.as_dict(), "campaign": name, "label": label}})
                    if h not in known:
                        output.write(canonical({"label": label, "config_hash": h, **result}).decode() + "\n")
                        output.flush()
                        os.fsync(output.fileno())
                    accepted += 1
                    if accepted == 1 or accepted % 20 == 0 or accepted == len(entries):
                        report = {"state": "submitting", "accepted": accepted, "total": len(entries),
                                  "pid": os.getpid(), "last_job_id": result["scheduler_id"]}
                        write_json(progress, report)
                        print(json.dumps(report), flush=True)
            write_json(progress, report | {"state": "submitted"})
            print(json.dumps(report | {"state": "submitted"}), flush=True)
            journal.append("campaign_submission_finished", campaign=name, site=site.name, jobs=accepted)
        except BaseException as error:
            write_json(progress, {"state": "failed", "accepted": accepted, "total": len(entries), "error": str(error)})
            journal.append("campaign_submission_failed", campaign=name, site=site.name, jobs=accepted, error=str(error))
            raise


if __name__ == "__main__":
    main()
