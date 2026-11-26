"""Update a shared manager installation on each execution host."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import json
import shlex
import subprocess

from .cluster import call, load_sites, manager_key, shell_path, deploy
from .errors import ExpectedError
from .files import digest, locked, read_json, write_json
from .provenance import scripts_identity, source_hash, source_identity
from .runner import PACKAGE_ROOT


SSH = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
       "-o", "ControlPath=none", "-o", "ControlMaster=no"]


def deployment_error(error):
    lines = [str(error)]
    if isinstance(error, subprocess.CalledProcessError):
        program = error.cmd[0] if isinstance(error.cmd, (tuple, list)) else error.cmd
        lines = [f"{program} failed (exit {error.returncode})"]
        if detail := (error.stderr or error.stdout or "").strip():
            lines.append(detail)
    return "\n".join(lines + getattr(error, "__notes__", []))


def deployment_expectation(*, source_sha256=None):
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
            "source": source_sha256 or source_hash(PACKAGE_ROOT.parent / "MTS2D"), "campaigns": campaigns}


def check_deployment(profiles, expected):
    """Read manager bytes, installed source record and campaign metadata over SSH."""
    script = r'''import hashlib,json,sys
from pathlib import Path
options=json.loads(sys.argv[1])
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode("ascii")).hexdigest()
base=Path(options["directory"]).expanduser().resolve()
source=(base/Path(options["source"]).expanduser()).resolve()
executable=(base/Path(options["executable"]).expanduser()).resolve() if options["executable"] else (source/"build/release/MTS2D").resolve()
manager=Path(options["manager_directory"]).expanduser() if options["manager_directory"] else base
if not manager.is_absolute(): manager=base/manager
package=manager/Path(options["module"].replace(".","/"))
result={"state":"missing"}
if package.is_dir():
    actual=digest({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(package.iterdir()) if p.suffix in {".py",".json"}})
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
result["build_target"]={"source":str(source), "executable":str(executable),
                        "retained":(executable.parent/".retained").exists()}
print("MTS_DEPLOYMENT\t"+json.dumps(result))
'''
    results = {}
    checked = {}
    for site in profiles:
        module = "MTS_Management" if site.manager_directory else site.management_module
        key = (site.directory, site.manager_directory, module, site.source, site.executable, site.python, site.setup)
        if key not in checked:
            options = {"directory": site.directory, "manager_directory": site.manager_directory,
                       "module": module, "source": site.source, "executable": site.executable, "expected": expected}
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


def build_servers(site_file, *, host=None, log=print, build_log=None):
    """Build working executables on enabled, current SSH deployments."""
    groups = {}
    for site in load_sites(site_file).values():
        if site.ssh and (host is None or site.ssh == host):
            groups.setdefault(site.ssh, []).append(site)
    if not groups:
        raise ExpectedError("Configure an enabled remote execution site in SSH connections.")
    expected = deployment_expectation()

    def build_host(item):
        host, profiles = item
        label = host.upper()
        log(f"Checking deployed MTS version on {label}")
        try:
            status = check_deployment(profiles, expected)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            detail = deployment_error(error)
            log(f"{label}: could not check deployment. {detail}")
            return [], [], {site.name: detail for site in profiles}
        if status["state"] != "current":
            log(f"{label}: build skipped. Update this server in SSH connections first.")
            return [], [site.name for site in profiles], {}
        built, skipped, errors = [], [], {}
        targets = {}
        for site in profiles:
            target = status["profiles"][site.name]["build_target"]
            executable = Path(target["executable"])
            if target["retained"]:
                log(f"{site.name.upper()}: build skipped. This profile uses a retained executable.")
                skipped.append(site.name)
            elif executable.name != "MTS2D":
                detail = "Set the profile executable to a working MTS2D binary."
                errors[site.name] = detail
                log(f"{site.name.upper()}: {detail}")
            else:
                targets.setdefault(executable.parent, []).append((site, target["source"]))
        for directory, target_profiles in targets.items():
            names = [site.name for site, _ in target_profiles]
            labels = ", ".join(name.upper() for name in names)
            if len({(source, site.native_arch) for site, source in target_profiles}) > 1:
                detail = f"Profiles sharing {directory} must agree on the source and native architecture option."
                errors.update(dict.fromkeys(names, detail))
                log(f"{labels}: {detail}")
                continue
            log(f"{labels}: building MTS2D Release with 2 parallel jobs.")
            site = target_profiles[0][0]
            try:
                call(site, "build", timeout=3600, jobs=2,
                     log=(lambda line: build_log(line, host=site.name)) if build_log else log,
                     expected_source_sha256=expected["source"])
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                detail = deployment_error(error).partition("\n")[0] or "See the build output in the terminal."
                errors.update(dict.fromkeys(names, detail))
                log(f"{labels}: build failed. {detail}")
            else:
                built.extend(names)
                log(f"{labels}: MTS2D build complete.")
        return built, skipped, errors

    built, skipped, errors = [], [], {}
    with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
        for host_built, host_skipped, host_errors in pool.map(build_host, groups.items()):
            built.extend(host_built)
            skipped.extend(host_skipped)
            errors.update(host_errors)
    return {"built_sites": built, "skipped_sites": skipped, "errors": errors}


def update_managers(site_file, *, log=print, host=None, force=False):
    """Deploy the installed manager once per SSH destination, then activate profiles."""
    site_file = Path(site_file).resolve()
    sites = load_sites(site_file)
    groups = {}
    for site in sites.values():
        if site.ssh and (host is None or site.ssh == host):
            groups.setdefault(site.ssh, []).append(site)
    if not groups:
        raise ValueError("No remote server profiles are configured")
    local_source = PACKAGE_ROOT.parent / "MTS2D"
    identity = source_identity(local_source)
    expected = scripts_identity()
    expected_deployment = deployment_expectation(source_sha256=identity["source_sha256"]) if not force else None
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
        host_label = host.upper()
        updated, skipped, errors = [], [], {}
        versions = {}
        if not force:
            log(f"Checking deployed MTS version on {host_label}")
            try:
                versions = check_deployment(profiles, expected_deployment)["profiles"]
            except Exception as error:
                detail = "Version check failed: " + deployment_error(error)
                errors.update({site.name: detail for site in profiles})
                return updated, skipped, errors
            current = [site for site in profiles if versions[site.name]["state"] == "current"]
            skipped.extend(site.name for site in current)
            for site in current:
                log(f"{site.name.upper()}: MTS is current; skipped")
            profiles = [site for site in profiles if site not in current]
            if not profiles:
                return updated, skipped, errors
        log(f"Updating manager on {host_label}")
        directories = {site.manager_directory for site in profiles if site.manager_directory}
        if len(directories) > 1:
            detail = "Profiles on this server specify different shared manager directories"
            errors.update({site.name: detail for site in profiles})
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
            detail = "Manager transfer failed: " + deployment_error(error)
            for site in profiles:
                errors[site.name] = detail
            return updated, skipped, errors
        source_updates = {(site.directory, site.source) for site in profiles if changed(site, "source")}
        workspace_results = {}
        for site in profiles:
            workspace = (site.directory, site.source)
            if workspace_results.get(workspace):
                errors[site.name] = workspace_results[workspace]
                continue
            try:
                run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", site.ssh,
                     "umask 077; mkdir -p -- " + shell_path(site.directory)])
                candidate = replace(site, manager_directory=directory)
                info = call(candidate, "info", timeout=30, log=log)
                if info["scripts"] != expected:
                    raise ValueError("Deployed manager does not match the local source version")
                if workspace not in workspace_results:
                    include_source = workspace in source_updates
                    log(f"{site.name.upper()}: " + ("uploading simulator source" if include_source else "simulator source is current; upload skipped"))
                    try:
                        deploy(candidate, include_source=include_source, local_source=local_source)
                    except Exception as error:
                        detail = "Workspace deployment failed: " + deployment_error(error)
                        workspace_results[workspace] = errors[site.name] = detail
                        continue
                    workspace_results[workspace] = None
                with locked(registry_file.with_suffix(".lock")):
                    registry = read_json(registry_file) if registry_file.exists() else {}
                    registry[manager_key(site)] = {"directory": directory, "scripts": expected}
                    write_json(registry_file, registry)
                updated.append(site.name)
                log(f"{site.name.upper()}: shared manager updated and verified")
            except Exception as error:
                errors[site.name] = "Profile activation failed: " + deployment_error(error)
        return updated, skipped, errors

    with ThreadPoolExecutor(max_workers=min(8, len(groups))) as pool:
        for host_updated, host_skipped, host_errors in pool.map(update_host, groups.items()):
            updated.extend(host_updated)
            skipped.extend(host_skipped)
            errors.update(host_errors)
    affected = {}
    for name, detail in errors.items():
        affected.setdefault(detail, []).append(name.upper())
    diagnostics = [f"{', '.join(names)}: {detail}" for detail, names in affected.items()]
    if not updated and not skipped:
        raise ExpectedError("No server managers were updated.\n" + "\n".join(diagnostics))
    for diagnostic in diagnostics:
        log(diagnostic)
    return {"updated_sites": updated, "skipped_sites": skipped, "errors": errors,
            "manager_sha256": expected["sha256"]}
