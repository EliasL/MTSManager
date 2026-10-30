"""Review and control queued MTS jobs by validated simulation settings."""

import argparse
import getpass
import json
import math
import operator
from pathlib import Path
import shlex
import subprocess
import sys

from MTS_Management.config import Config, DEFAULTS
from MTS_Management.files import read_json

ACTIONS = {"hold": "Hold queued jobs", "release": "Release user-held jobs", "cancel": "Cancel queued jobs"}
CHOICES = {"reconnectionMethod": ["none", "edgeFlip", "delaunay"], "preconditioner": ["none", "exactTangent"],
           "tangentBackend": ["eigen", "cholmod"], "boundary": ["fixed", "periodic", "anchored", "periodicAnchored"],
           "loading": ["relaxation", "reversibility", "noRelaxation"]}
LABELS = {"L": "System size L", "nrThreads": "Threads", "reconnectionMethod": "Reconnection", "preconditioner": "Preconditioner"}
OPERATORS = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge,
             "contains": lambda a, b: b in a}


def fields():
    defaults = {"L": 100, "campaign": "", **DEFAULTS}
    return {k: {"label": LABELS.get(k, k), "type": type(v).__name__, "choices": CHOICES.get(k, [False, True] if type(v) is bool else [])}
            for k, v in defaults.items()}


def validate_rule(rule):
    if rule.get("action") not in ACTIONS or rule.get("match") not in {"all", "any"} or not rule.get("criteria"):
        raise ValueError("Choose an action, all/any matching, and at least one criterion.")
    definitions = fields()
    for c in rule["criteria"]:
        if c.keys() != {"field", "operator", "value"} or c["field"] not in definitions or c["operator"] not in OPERATORS:
            raise ValueError("Invalid Slurm selection criterion.")
        kind = definitions[c["field"]]["type"]
        allowed = {"int": (int,), "float": (int, float), "bool": (bool,), "str": (str,)}[kind]
        if type(c["value"]) not in allowed or (c["operator"] == "contains" and kind != "str"):
            raise ValueError(f"Invalid value/operator for {c['field']}.")
        if type(c["value"]) is float and not math.isfinite(c["value"]):
            raise ValueError("Numeric criteria must be finite.")
        if kind == "bool" and c["operator"] not in {"eq", "ne"}:
            raise ValueError("Boolean criteria require equality or inequality.")
    return rule


def matches(config, manifest, rule):
    values = dict(config.as_dict(), campaign=manifest.get("campaign", ""))
    results = []
    for c in rule["criteria"]:
        if c["field"] == "L":
            result = config["rows"] == config["cols"] and OPERATORS[c["operator"]](config["rows"], c["value"])
        else:
            value = values[c["field"]] if c["field"] == "campaign" else config[c["field"]]
            result = OPERATORS[c["operator"]](value, c["value"])
        results.append(result)
    return (all if rule["match"] == "all" else any)(results)


def queue():
    result = subprocess.run(["squeue", "--noheader", "--user", getpass.getuser(), "--format=%i|%T|%200j|%R"],
                            capture_output=True, text=True, check=True, timeout=20)
    jobs = {}
    for line in result.stdout.splitlines():
        job_id, state, name, reason = [v.strip() for v in line.split("|", 3)]
        if reason.startswith("(") and reason.endswith(")"):
            reason = reason[1:-1]
        jobs[job_id] = dict(job_id=job_id, state=state, name=name, reason=reason)
    return jobs


def command(action, ids):
    if action not in ACTIONS or any(not str(i).isdigit() for i in ids):
        raise ValueError("Invalid scheduler action or job IDs.")
    return (["scancel", "--state=PENDING", "--user", getpass.getuser(), *ids] if action == "cancel" else
            ["scontrol", action, *ids])


def remote(payload):
    from MTS_Management.cluster import Site, paths
    from MTS_Management.journal import Journal
    site = Site(**payload["site"])
    if site.mode != "slurm":
        raise ValueError("This profile does not use Slurm.")
    options = payload["options"]
    rule = validate_rule(options["rule"])
    p = paths(site)
    root = Path(p["output_root"])
    index_path = root / ".run-index.json"
    if not index_path.exists():
        if any(root.glob("*/manifest.json")):
            raise ValueError("Missing run index; refresh or reindex this site's inventory first.")
        return {"jobs": [], "running": [], "already": [], "applied": [], "command": ""}
    index = read_json(index_path)
    by_name = {name: h for h, name in index.items()}
    live = queue()
    jobs, running, already = [], [], []
    for job in live.values():
        if job["name"] not in by_name:
            continue
        folder = root / job["name"]
        if folder.parent != root:
            raise ValueError("Run index contains an invalid folder path.")
        config = Config.read(folder / "config.conf", expected_name=folder.name)
        manifest = read_json(folder / "manifest.json")
        if (config.config_hash != by_name[folder.name] or manifest["config_hash"] != config.config_hash
                or manifest["settings"] != config.as_dict()):
            raise ValueError(f"Configuration identity mismatch: {folder}")
        if not matches(config, manifest, rule):
            continue
        owned = any(str(read_json(a / "status.json").get("scheduler_id")) == job["job_id"] for a in (folder / "attempts").iterdir())
        if not owned:
            continue
        row = job | {"config_hash": config.config_hash, "folder": str(folder)}
        if job["state"] != "PENDING":
            running.append(row)
        elif rule["action"] == "hold" and job["reason"] == "JobHeldUser":
            already.append(row)
        elif rule["action"] == "release" and job["reason"] != "JobHeldUser":
            already.append(row)
        else:
            jobs.append(row)
    expected = options.get("job_ids")
    if expected is not None:
        if any(not isinstance(i, str) or not i.isdigit() for i in expected):
            raise ValueError("Invalid reviewed scheduler IDs.")
        jobs = [j for j in jobs if j["job_id"] in set(expected)]
    ids = sorted((j["job_id"] for j in jobs), key=int)
    cmd = command(rule["action"], ids) if ids else []
    applied, skipped = [], []
    verification = []
    if options.get("apply") and ids:
        # Recheck the live queue immediately before changing the reviewed jobs.
        fresh = queue()
        skipped = [i for i in ids if fresh.get(i, {}).get("state") != "PENDING"]
        ids = [i for i in ids if fresh.get(i, {}).get("state") == "PENDING"]
        cmd = command(rule["action"], ids) if ids else []
        if ids:
            journal = Journal(p["state_dir"])
            journal.append("slurm_control_requested", site=site.name, action=rule["action"], rule=rule, job_ids=ids, command=cmd)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=30)
            after = queue()
            if rule["action"] == "hold":
                applied = [i for i in ids if after.get(i, {}).get("state") == "PENDING" and after[i]["reason"] == "JobHeldUser"]
            elif rule["action"] == "release":
                applied = [i for i in ids if i in after and after[i]["reason"] != "JobHeldUser"]
            else:
                applied = [i for i in ids if i not in after]
            verification = [i for i in ids if i not in applied]
            journal.append("slurm_control_result", site=site.name, action=rule["action"], job_ids=ids,
                           applied=applied, unverified=verification, returncode=result.returncode, stderr=result.stderr)
            if result.returncode:
                raise RuntimeError(f"{shlex.join(cmd)}: {result.stderr.strip()}; verified {len(applied)}/{len(ids)} jobs. Inspect journal and queue before retrying.")
    return {"jobs": jobs, "running": running, "already": already, "applied": applied, "skipped": skipped,
            "unverified": verification, "command": shlex.join(cmd) if cmd else ""}


def run_sites(site_file, selection, rule, *, apply=False, reviewed=None, log=None):
    from MTS_Management.cluster import load_sites, across
    from MTS_Management.journal import Journal
    validate_rule(rule)
    profiles = load_sites(site_file)
    if selection != "*" and selection not in profiles:
        raise ValueError("Unknown site profile.")
    sites = [s for s in profiles.values() if s.mode == "slurm" and (selection == "*" or s.name == selection)]
    if not sites:
        raise ValueError("No enabled Slurm sites match the selection.")
    per_site = {s.name: {"job_ids": reviewed[s.name]} for s in sites if reviewed is not None and s.name in reviewed}
    if reviewed is not None:
        sites = [s for s in sites if s.name in reviewed]
    # The program is sent with the request; installed workers and packages are unchanged.
    result = across(sites, "slurm_control", _program=Path(__file__).read_text(), timeout=90,
                    rule=rule, apply=apply, per_site=per_site)
    for name, response in result.items():
        if log:
            log(f"{name}: {response['error']}" if "error" in response else
                f"{name}: {len(response['data']['jobs'])} queued matches; {len(response['data']['applied'])} verified changes; {len(response['data']['running'])} active jobs untouched")
            if "data" in response and response["data"]["command"]:
                log(f"{name}: {response['data']['command']}")
        if apply and "data" in response:
            Journal(Path(site_file).resolve().parent / ".state").append("slurm_control", site=name, rule=rule,
                applied=response["data"]["applied"], unverified=response["data"].get("unverified", []), skipped=response["data"].get("skipped", []))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sites", type=Path, required=True)
    parser.add_argument("--site", default="*")
    parser.add_argument("--action", choices=ACTIONS, required=True)
    parser.add_argument("--match", choices=("all", "any"), default="all")
    parser.add_argument("--criteria", required=True, help="JSON criteria with field, operator and value")
    parser.add_argument("--apply", action="store_true", help="Execute; the default only reviews live matches")
    args = parser.parse_args()
    rule = {"action": args.action, "match": args.match, "criteria": json.loads(args.criteria)}
    result = run_sites(args.sites, args.site, rule, apply=args.apply, log=lambda s: print(s, file=sys.stderr))
    print(json.dumps(result, indent=2))
    if any("error" in r or r.get("data", {}).get("unverified") for r in result.values()):
        raise SystemExit(1)


if "__mts_control_request" in globals():
    import signal
    signal.alarm(85)
    print(json.dumps(remote(__mts_control_request)))
elif __name__ == "__main__":
    main()
