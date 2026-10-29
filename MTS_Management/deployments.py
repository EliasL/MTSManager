"""Update a shared manager installation on each execution host."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import shlex
import subprocess

from .cluster import call, load_sites, manager_key, shell_path, deploy
from .files import digest, locked, read_json, write_json
from .provenance import scripts_identity, source_hash
from .runner import PACKAGE_ROOT


SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
       "-o", "ControlPath=none", "-o", "ControlMaster=no"]


def deployment_expectation():
    """Version records used by connection checks; no executable is launched."""
    root = PACKAGE_ROOT / "campaigns"
    campaigns = {}
    for path in sorted(root.rglob("*.json")):
        if path.name not in {"simulations.json", "resources.json", "metadata.json"}:
            continue
        data = read_json(path)
        if path.name == "metadata.json":
            data.pop("executables", None)
        campaigns[str(path.relative_to(root))] = digest(data)
    return {"scripts": scripts_identity()["sha256"],
            "source": source_hash(PACKAGE_ROOT.parent / "MTS2D"), "campaigns": campaigns}


def check_deployment(profiles, expected):
    """Read manager bytes, installed source record and campaign metadata over SSH."""
    script = r'''import hashlib,json,sys
from pathlib import Path
options=json.loads(sys.argv[1])
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode("ascii")).hexdigest()
base=Path(options["directory"]).expanduser().resolve()
manager=Path(options["manager_directory"]).expanduser() if options["manager_directory"] else base
if not manager.is_absolute(): manager=base/manager
package=manager/Path(options["module"].replace(".","/"))
result={"state":"missing"}
if package.is_dir():
    actual=digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.iterdir()) if p.suffix in {".py",".json"}})
    source=Path(options["source"]).expanduser()
    if not source.is_absolute(): source=base/source
    record=source/"SOURCE.json"
    source_hash=json.loads(record.read_text()).get("source_sha256") if record.exists() else None
    campaigns={}
    for name in options["expected"]["campaigns"]:
        path=manager/"campaigns"/name
        if not path.exists(): campaigns[name]=None;continue
        data=json.loads(path.read_text())
        if path.name=="metadata.json":data.pop("executables",None)
        campaigns[name]=digest(data)
    found={"scripts":actual,"source":source_hash,"campaigns":campaigns}
    result={"state":"current" if found==options["expected"] else "mismatch", "identity":found,
            "differences":[key for key in found if found[key]!=options["expected"][key]]}
print("MTS_DEPLOYMENT\t"+json.dumps(result))
'''
    results = {}
    checked = {}
    for site in profiles:
        module = "MTS_Management" if site.manager_directory else site.management_module
        key = (site.directory, site.manager_directory, module, site.source, site.python, site.setup)
        if key not in checked:
            options = {"directory": site.directory, "manager_directory": site.manager_directory,
                       "module": module, "source": site.source, "expected": expected}
            command = site.setup + "\n" + shlex.join([site.python, "-c", script, json.dumps(options)])
            response = subprocess.run([*SSH, site.ssh, "bash -lc " + shlex.quote(command)],
                                      capture_output=True, text=True, check=True, timeout=30)
            records = [line.split("\t", 1)[1] for line in response.stdout.splitlines() if line.startswith("MTS_DEPLOYMENT\t")]
            if len(records) != 1:
                raise ValueError("The server did not return a deployment version")
            checked[key] = json.loads(records[0])
        results[site.name] = checked[key]
    states = {r["state"] for r in results.values()}
    state = "current" if states == {"current"} else "missing" if states == {"missing"} else "mismatch"
    labels = {"scripts": "manager code", "source": "simulator source", "campaigns": "campaign files"}
    detail = "; ".join(name.upper() + ": version mismatch in " + ", ".join(labels[key] for key in record["differences"])
                       for name, record in results.items() if record.get("differences"))
    return {"state": state, "profiles": results, "detail": detail}


def update_managers(site_file, *, log=print, host=None, force=False):
    """Deploy the installed manager once per SSH destination, then activate profiles."""
    site_file = Path(site_file).resolve()
    sites = load_sites(site_file)
    expected = scripts_identity()
    expected_deployment = deployment_expectation() if not force else None
    groups = {}
    for site in sites.values():
        if site.ssh and (host is None or site.ssh == host):
            groups.setdefault(site.ssh, []).append(site)
    if not groups:
        raise ValueError("No remote server profiles are configured")
    registry_file = site_file.parent / ".state/manager-deployments.json"
    updated, skipped, errors = [], [], {}
    def run(command):
        log("$ " + shlex.join(command))
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
        if result.stdout.strip():
            log(result.stdout.rstrip())
        if result.stderr.strip():
            log(result.stderr.rstrip())
    def update_host(item):
        host, profiles = item
        updated, skipped, errors = [], [], {}
        versions = {}
        if not force:
            log(f"Checking deployed MTS version on {host}")
            try:
                versions = check_deployment(profiles, expected_deployment)["profiles"]
            except Exception as error:
                detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
                errors.update({site.name: detail for site in profiles})
                log(f"{host}: version check failed: {detail}")
                return updated, skipped, errors
            current = [site for site in profiles if versions[site.name]["state"] == "current"]
            skipped.extend(site.name for site in current)
            for site in current:
                log(f"{site.name}: MTS is current; skipped")
            profiles = [site for site in profiles if site not in current]
            if not profiles:
                return updated, skipped, errors
        log(f"Updating manager on {host}")
        directories = {site.manager_directory for site in profiles if site.manager_directory}
        if len(directories) > 1:
            detail = "Profiles on this server specify different shared manager directories"
            errors.update({site.name: detail for site in profiles})
            log(f"{host}: deployment failed: {detail}")
            return updated, skipped, errors
        directory = next(iter(directories), "~/.local/share/mts/manager")
        def changed(site, component):
            return force or versions[site.name].get("identity", {}).get(component) != expected_deployment[component]
        try:
            run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                 "umask 077; mkdir -p -- " + shell_path(directory)])
            if any(not site.manager_directory or changed(site, "scripts") for site in profiles):
                run(["rsync", "-a", "--delete", "--delete-excluded", "--itemize-changes", "--exclude=__pycache__", "--include=*.py",
                     "--include=*.json", "--exclude=*", "-e", "ssh -T -o BatchMode=yes -o ConnectTimeout=8",
                     str(PACKAGE_ROOT / "MTS_Management") + "/", host + ":" + directory + "/MTS_Management/"])
            if any(not site.manager_directory or changed(site, "campaigns") for site in profiles):
                run(["rsync", "-a", "-e", "ssh -T", str(PACKAGE_ROOT / "campaigns") + "/",
                     host + ":" + directory + "/campaigns/"])
        except Exception as error:
            detail = (error.stderr.strip() or f"Command failed (exit {error.returncode})") if isinstance(error, subprocess.CalledProcessError) else str(error)
            for site in profiles:
                errors[site.name] = detail
            log(f"{host}: deployment failed: {detail}")
            return updated, skipped, errors
        uploaded = set()
        for site in profiles:
            try:
                run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", site.ssh,
                     "umask 077; mkdir -p -- " + shell_path(site.directory)])
                candidate = replace(site, manager_directory=directory)
                info = call(candidate, "info", timeout=30, log=log)
                workspace = (site.directory, site.source)
                if workspace not in uploaded:
                    include_source = changed(site, "source")
                    log(f"{site.name}: " + ("uploading simulator source" if include_source else "simulator source is current; upload skipped"))
                    deploy(candidate, include_source=include_source, local_source=PACKAGE_ROOT.parent / "MTS2D")
                    uploaded.add(workspace)
                if info["scripts"] != expected:
                    raise ValueError("Deployed manager does not match the local source version")
                with locked(registry_file.with_suffix(".lock")):
                    registry = read_json(registry_file) if registry_file.exists() else {}
                    registry[manager_key(site)] = {"directory": directory, "scripts": expected}
                    write_json(registry_file, registry)
                updated.append(site.name)
                log(f"{site.name}: shared manager updated and verified")
            except Exception as error:
                detail = (error.stderr.strip() or f"Command failed (exit {error.returncode})") if isinstance(error, subprocess.CalledProcessError) else str(error)
                errors[site.name] = detail
                log(f"{site.name}: activation failed: {detail}")
        return updated, skipped, errors

    with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
        for host_updated, host_skipped, host_errors in pool.map(update_host, groups.items()):
            updated.extend(host_updated)
            skipped.extend(host_skipped)
            errors.update(host_errors)
    if not updated and not skipped:
        raise RuntimeError("No server managers were updated. " + "; ".join(f"{s}: {e}" for s, e in errors.items()))
    return {"updated_sites": updated, "skipped_sites": skipped, "errors": errors,
            "manager_sha256": expected["sha256"]}
