"""Submit validated individual runs and saved campaigns from Python or the CLI."""

from dataclasses import replace
from pathlib import Path

from .campaign import install_campaign_files, validate_campaign
from .cluster import call, paths, require_available_output, submit
from .config import Config
from .errors import ExpectedError
from .files import read_json
from .initial_mesh import mesh_checksum
from .runner import RunConflict, launch, prepare, run_many, set_status


def submission_state_dir(site_file, sites):
    """Locate the local receipt journal for managed submissions."""
    if "local" in sites:
        return Path(paths(sites["local"])["state_dir"])
    return Path(site_file).resolve().parent / ".state"


def _check_chain(site, chain):
    if site.disabled:
        raise ExpectedError(f"Enable {site.name} before submitting a simulation.")
    if type(chain) is not int or chain < 1 or (chain > 1 and site.mode != "slurm"):
        raise ExpectedError("Choose a positive job count and use Slurm for multiple jobs.")


def _prepare(site, config, *, log=None, **options):
    location = paths(site)
    require_available_output(site, location)
    return prepare(config, executable=location["executable"], output_root=location["output_root"],
                   state_dir=location["state_dir"], site=site.name, environment=site.environment,
                   log=log, **options)


def _jobs(result):
    return result["jobs"] if "jobs" in result else [result]


def submit_run(site, config, *, state_dir, campaign="AdHoc", label=None, checkpoint=None,
               chain=1, initial_mesh=None, log=None):
    """Submit one run and return its jobs, including all allocations in a chain."""
    _check_chain(site, chain)
    if initial_mesh is not None:
        checksum = mesh_checksum(initial_mesh)
        if config["initialMeshSha256"] and config["initialMeshSha256"] != checksum:
            raise ExpectedError("The initial mesh checksum differs from config.conf.")
        config = Config(**(config.as_dict() | {"initialMeshSha256": checksum}))
    options = dict(campaign=campaign, label=label, checkpoint=checkpoint, initial_mesh=initial_mesh)
    if not site.ssh and site.mode == "foreground":
        result = launch(_prepare(site, config, log=log, **options))
    else:
        result = submit(site, config, state_dir=state_dir, chain=chain, log=log, **options)
    return {"config_hash": config.config_hash, "jobs": _jobs(result)}


def submit_campaign(site, path, *, state_dir, chain=1, workers=2, log=None):
    """Submit a saved campaign and return jobs, completed skips and conflicts."""
    _check_chain(site, chain)
    if type(workers) is not int or workers < 1:
        raise ExpectedError("Choose a positive number of local campaign workers.")
    path = Path(path)
    records = {"simulations.json": read_json(path)}
    for name in ("resources.json", "metadata.json"):
        companion = path.with_name(name)
        if companion.exists():
            records[name] = read_json(companion)
    campaign, entries = validate_campaign(records["simulations.json"],
                                         records.get("metadata.json", {}).get("missing_settings"))
    if chain > 1 and any(not config["writeDumps"] for config, _ in entries):
        raise ExpectedError("Enable writeDumps for every campaign member in a Slurm chain.")
    install_campaign_files([records])
    if site.ssh:
        call(site, "install_campaign_files", records=[records], log=log)
    resources = records.get("resources.json", {}).get("resources", {})
    result = {"campaign": campaign, "jobs": [], "skipped": [], "conflicts": []}
    if not site.ssh and site.mode == "foreground":
        attempts = []
        try:
            for config, label in entries:
                member = {"label": label, "config_hash": config.config_hash}
                try:
                    attempt = _prepare(site, config, label=label, campaign=campaign,
                                       skip_completed=True, log=log)
                except RunConflict as error:
                    result["conflicts"].append(member | {"conflict": str(error)})
                    continue
                if attempt is None:
                    result["skipped"].append(member)
                else:
                    attempts.append((attempt, member))
        except BaseException:
            for attempt, _ in attempts:
                set_status(attempt, "stopped", reason="Batch preparation failed")
            raise
        jobs = run_many([attempt for attempt, _ in attempts], workers) if attempts else []
        result["jobs"] = [job | member for job, (_, member) in zip(jobs, attempts, strict=True)]
    else:
        for config, label in entries:
            execution_site = replace(site, slurm=site.slurm | resources.get(str(config["rows"]), {}))
            receipt = submit(execution_site, config, state_dir=state_dir, campaign=campaign,
                             label=label, chain=chain, skip_completed=True, log=log)
            member = {"label": label, "config_hash": config.config_hash}
            if receipt.get("skipped"):
                result["skipped"].append(member)
            elif receipt.get("conflict"):
                result["conflicts"].append(member | {"conflict": receipt["conflict"]})
            else:
                result["jobs"].extend(job | member for job in _jobs(receipt))
    return result
