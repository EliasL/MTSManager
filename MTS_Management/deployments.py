"""Update a shared manager installation on each execution host."""

from dataclasses import replace
from pathlib import Path
import shlex
import subprocess

from .cluster import call, load_sites, manager_key, shell_path
from .files import locked, read_json, write_json
from .provenance import scripts_identity
from .runner import PACKAGE_ROOT


def update_managers(site_file, *, log=print, host=None):
    """Deploy the installed manager once per SSH destination, then activate profiles."""
    site_file = Path(site_file).resolve()
    sites = load_sites(site_file)
    expected = scripts_identity()
    groups = {}
    for site in sites.values():
        if site.ssh and (host is None or site.ssh == host):
            groups.setdefault(site.ssh, []).append(site)
    if not groups:
        raise ValueError("No remote server profiles are configured")
    registry_file = site_file.parent / ".state/manager-deployments.json"
    updated, errors = [], {}
    def run(command):
        log("$ " + shlex.join(command))
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
        if result.stdout.strip():
            log(result.stdout.rstrip())
        if result.stderr.strip():
            log(result.stderr.rstrip())
    for host, profiles in groups.items():
        log(f"Updating manager on {host}")
        directories = {site.manager_directory for site in profiles if site.manager_directory}
        if len(directories) > 1:
            detail = "Profiles on this server specify different shared manager directories"
            errors.update({site.name: detail for site in profiles})
            log(f"{host}: deployment failed: {detail}")
            continue
        directory = next(iter(directories), "~/.local/share/mts/manager")
        try:
            run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                 "umask 077; mkdir -p -- " + shell_path(directory)])
            run(["rsync", "-a", "--delete", "--delete-excluded", "--itemize-changes", "--exclude=__pycache__", "--include=*.py",
                 "--include=*.json", "--exclude=*", "-e", "ssh -T -o BatchMode=yes -o ConnectTimeout=8",
                 str(PACKAGE_ROOT / "MTS_Management") + "/", host + ":" + directory + "/MTS_Management/"])
        except Exception as error:
            detail = (error.stderr.strip() or f"Command failed (exit {error.returncode})") if isinstance(error, subprocess.CalledProcessError) else str(error)
            for site in profiles:
                errors[site.name] = detail
            log(f"{host}: deployment failed: {detail}")
            continue
        for site in profiles:
            try:
                run(["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", site.ssh,
                     "umask 077; mkdir -p -- " + shell_path(site.directory)])
                candidate = replace(site, manager_directory=directory)
                info = call(candidate, "info", timeout=30, log=log)
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
    if not updated:
        raise RuntimeError("No server managers were updated. " + "; ".join(f"{s}: {e}" for s, e in errors.items()))
    return {"updated_sites": updated, "errors": errors, "manager_sha256": expected["sha256"]}
