"""OpenSSH transport; the same installed package handles work on each host."""

from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
import getpass
import json
import posixpath
from pathlib import Path
import re
import shlex
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib

from .config import Config, DEFAULTS
from .files import canonical, digest, file_digest, locked, now, read_json, write_json
from .inventory import inventory, rebuild_index, resources
from .inventory_cache import export as export_inventory, snapshot as cached_inventory
from .journal import Journal
from .provenance import build, install_source, source_archive, source_files, source_identity, executable_identity
from .runner import PACKAGE_ROOT, launch, prepare, stop


@dataclass
class Site:
    name: str
    ssh: str = ""
    disabled: bool = False
    directory: str = "."
    python: str = "python3"
    management_module: str = "MTS_Management"
    manager_directory: str = ""  # Optional shared package location; simulator paths remain unchanged.
    source: str = "../MTS2D"
    executable: str = ""  # Default: source/build/release/MTS2D on the execution host.
    output_root: str = "output"
    state_dir: str = ".state"
    mode: str = "detached"
    setup: str = ""
    native_arch: bool = False
    environment: dict = field(default_factory=dict)
    slurm_cpus_per_thread: int = 1
    slurm: dict = field(default_factory=dict)

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", self.management_module):
            raise ValueError("management_module must be a Python module name")
        if self.ssh and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", self.ssh):
            raise ValueError("ssh must be an SSH config alias or user@hostname")
        if self.mode not in {"foreground", "detached", "slurm"}:
            raise ValueError("mode must be foreground, detached or slurm")
        if type(self.disabled) is not bool:
            raise TypeError("disabled must be a boolean")
        if type(self.native_arch) is not bool:
            raise TypeError("native_arch must be a boolean")
        if type(self.slurm_cpus_per_thread) is not int or self.slurm_cpus_per_thread < 1:
            raise ValueError("slurm_cpus_per_thread must be a positive integer")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or not isinstance(v, str)
               for k, v in self.environment.items()):
            raise ValueError("Environment keys must be variable names and values must be strings")


def manager_key(site):
    return digest({"ssh": site.ssh, "directory": site.directory})


def load_sites(path, *, include_disabled=False):
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text())
    if data.keys() != {"sites"}:
        raise ValueError("Site configuration must contain only [sites.NAME] sections")
    sites = {}
    deployments_file = path.parent / ".state/manager-deployments.json"
    deployments = read_json(deployments_file) if deployments_file.exists() else {}
    connection_file = path.parent / ".state/ssh-connections.json"
    connections = read_json(connection_file) if connection_file.exists() else {}
    for name, settings in data["sites"].items():
        site = Site(name=name, **settings)
        if not site.ssh:
            site.directory = str((path.parent / site.directory).resolve())
        elif manager_key(site) in deployments:
            site.manager_directory = deployments[manager_key(site)]["directory"]
        site.disabled = connections.get(site.ssh, {}).get("disabled", site.disabled)
        if include_disabled or not site.disabled:
            sites[name] = site
    return sites


def shell_path(path):
    # Tilde expansion without exposing arbitrary path text to the remote shell.
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def paths(site):
    base = Path(site.directory).expanduser().resolve()
    def resolve(value):
        p = Path(value).expanduser()
        return str((p if p.is_absolute() else base / p).resolve())
    result = {k: resolve(getattr(site, k)) for k in ("source", "output_root", "state_dir")}
    result["executable"] = (resolve(site.executable) if site.executable else
                            str(Path(result["source"]) / "build/release/MTS2D"))
    return result


def scheduler_jobs():
    result = subprocess.run(["squeue", "--noheader", "--user", getpass.getuser(),
                             "--format=%i|%T|%j|%M|%R"],
                            capture_output=True, text=True, check=True, timeout=15)
    rows = []
    for line in result.stdout.splitlines():
        job, state, name, elapsed, reason = line.split("|", 4)
        rows.append(dict(job_id=job.strip(), state=state.strip(), name=name.strip(),
                         elapsed=elapsed.strip(), reason=reason.strip()))
    return rows


def serve(payload, *, cancel=None):
    """One request per process. Stdout is reserved for the JSON response."""
    site = Site(**payload["site"])
    p = paths(site)
    op = payload["operation"]
    options = payload.get("options", {})
    if op == "info":
        import sys
        from .provenance import scripts_identity
        return {"python": sys.version, "executable": sys.executable,
                "scripts": scripts_identity(), "paths": p}
    if op == "reindex":
        return rebuild_index(p["output_root"])
    if op == "inventory":
        return inventory(p["output_root"], cancel=cancel, **options)
    if op == "inventory_cached":
        if options:
            raise ValueError("Cached inventory reads do not accept refresh options")
        return cached_inventory(p["output_root"])
    if op == "inventory_update":
        if "config_hash" in options or "config_hashes" in options:
            raise ValueError("Inventory database export requires a full refresh")
        Path(p["output_root"]).mkdir(parents=True, exist_ok=True)
        inventory(p["output_root"], cancel=cancel, **options)
        return export_inventory(p["output_root"])
    if op == "resources":
        return resources(p["output_root"]) | {
            "scheduler": scheduler_jobs() if site.mode == "slurm" else None}
    if op == "install_campaign_files":
        from .campaign import install_campaign_files
        install_campaign_files(options["records"])
        return {"installed": len(options["records"])}
    if op == "campaign_files":
        from .campaign import campaign_files
        return campaign_files(options["names"])
    if op == "journal":
        return Journal(p["state_dir"]).since(**options)
    if op == "snapshot":
        return {"resources": serve(payload | {"operation": "resources", "options": {}}),
                "inventory": inventory(p["output_root"], refresh_sizes=options.get("refresh_sizes", False),
                                       hard_refresh=options.get("hard_refresh", False), cancel=cancel),
                "journal": Journal(p["state_dir"]).since(options.get("journal_offset", 0))}
    if op == "install_source":
        return install_source(p["source"], options["staging"], options["expected"])
    if op == "build":
        return build(p["source"], Path(p["executable"]).parent,
                     native_arch=site.native_arch, archive_dir=Path(site.directory).resolve() / "executables", **options)
    if op == "run":
        settings = options.pop("settings")
        config = Config.saved(settings)
        chain = options.pop("chain", 1)
        # Remote sessions must survive loss of the SSH connection.
        mode = "detached" if site.mode == "foreground" else site.mode
        if type(chain) is not int or chain < 1:
            raise ValueError("chain must be a positive integer (total Slurm allocations)")
        if chain > 1 and (mode != "slurm" or not config["writeDumps"]):
            raise ValueError("Job chains require Slurm and writeDumps=true")
        slurm = {"cpus-per-task": config["nrThreads"] * site.slurm_cpus_per_thread} | site.slurm
        jobs = []
        for i in range(chain):
            if i:
                options.update(checkpoint="latest", after=jobs[-1]["attempt"])
            attempt = prepare(config, executable=p["executable"], output_root=p["output_root"],
                              state_dir=p["state_dir"], site=site.name,
                              environment=site.environment, **options)
            jobs.append(launch(attempt, mode=mode, slurm=slurm, setup=site.setup))
        return jobs[0] if chain == 1 else {"jobs": jobs}
    if op in {"stop", "logs"}:
        attempt = Path(options["attempt"]).expanduser().resolve()
        if not attempt.is_relative_to(Path(p["output_root"])):
            raise ValueError("Attempt is outside this site's output root")
        if op == "stop":
            stop(attempt)
            return {"attempt": str(attempt), "state": read_json(attempt / "status.json")["state"]}
        log = attempt / "stdout.log"
        if not log.exists():
            log = attempt / "launcher.log"
        if not log.exists():
            return "No output yet."
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size-65536))
            return stream.read(65536).decode(errors="replace")
    raise ValueError(f"Unknown remote operation: {op}")


def call(site, operation, *, timeout=30, log=None, cancel=None, _program=None, **options):
    if site.disabled:
        raise ValueError(f"Connection {site.ssh or site.name} is disabled")
    settings = asdict(site)
    settings.pop("disabled")
    module = settings.pop("management_module")  # Client transport setting, not remote job data.
    manager_directory = settings.pop("manager_directory")
    if manager_directory:
        module = "MTS_Management"
    payload = {"site": settings, "operation": operation, "options": options}
    if _program is not None:
        payload["program"] = _program
    if cancel is not None and cancel.is_set():
        raise CancelledError("Operation aborted")
    if not site.ssh and not site.setup and _program is None:
        return serve(payload, cancel=cancel)
    python = [site.python, '-u', '-m', module, '_serve']
    if _program is not None:
        python = [site.python, '-u', '-c', "import json,sys; __mts_control_request=json.load(sys.stdin); exec(__mts_control_request.pop('program'))"]
    reader = site.ssh and operation in {"inventory", "inventory_cached", "inventory_update", "logs"}
    if reader:
        payload["reader_timeout"] = max(1, int(timeout))
    if manager_directory:
        # -P prevents a stale package in the worker directory shadowing the shared installation.
        python.insert(2, "-P")
    prefix = ("export PYTHONPATH=" + shell_path(manager_directory) + '${PYTHONPATH:+:"$PYTHONPATH"} && '
              if manager_directory else "")
    cmd = f"cd {shell_path(site.directory)} && {prefix}{shlex.join(python)}"
    if site.setup:
        cmd = site.setup + "\n" + cmd
    args = (["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             site.ssh, "bash -lc " + shlex.quote("set -e\n" + cmd)] if site.ssh else
            ["bash", "-lc", "set -e\n" + site.setup + "\ncd " + shell_path(site.directory) +
             "\n" + shlex.join([sys.executable, *python[1:]] if _program is not None else
                                 [sys.executable, "-u", "-m", module, "_serve"])])
    if log:
        log("$ " + shlex.join(args))
    started = time.monotonic()
    # Polling communicate() with input=None after a timeout can leave a large
    # request only partly written. A file supplies the complete request and EOF.
    with tempfile.TemporaryFile() as request:
        request.write(canonical(payload))
        request.seek(0)
        with subprocess.Popen(args, stdin=request, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise CancelledError("Operation aborted; previous cached inventory retained")
                    if time.monotonic() - started >= timeout:
                        raise subprocess.TimeoutExpired(args, timeout)
                    try:
                        stdout, stderr = process.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pass
            except BaseException as error:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                if isinstance(error, subprocess.TimeoutExpired):
                    error.output, error.stderr = stdout, stderr
                raise
            if log and stderr.strip():
                log(stderr.rstrip())
            if process.returncode:
                if reader and process.returncode in {-14, 142}:
                    raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr)
                if site.ssh and process.returncode == 255:
                    raise ConnectionError(f"{site.ssh}: {stderr.strip() or stdout.strip()}")
                raise RuntimeError(f"{site.ssh or site.name}: {operation} failed (exit {process.returncode}):\n"
                                   + (stderr.strip() or stdout.strip() or "No diagnostic output"))
        if log:
            log(f"Received {len(stdout.encode())} bytes in {time.monotonic() - started:.2f} s")
        return json.loads(stdout)


def across(sites, operation, *, per_site=None, **options):
    """Host failures remain visible, without losing healthy hosts' results."""
    results = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(sites)))) as pool:
        futures = {pool.submit(call, site, operation, **(options | (per_site or {}).get(site.name, {}))):
                   site.name for site in sites}
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = {"data": future.result()}
            except Exception as error:
                results[name] = {"error": str(error), "stderr": getattr(error, "stderr", None)}
    return results


def sync_journals(sites, state_dir):
    return synchronized_snapshot(sites, state_dir, operation="journal")


def synchronized_snapshot(sites, state_dir, *, operation="snapshot", **options):
    """Import events before advancing cursors, so retries cannot lose history."""
    cursor_path = Path(state_dir) / "journal-cursors.json"
    with locked(Path(state_dir) / ".sync.lock"):
        cursors = read_json(cursor_path) if cursor_path.exists() else {}
        keys = {s.name: digest({"ssh": s.ssh, "directory": s.directory, "state": s.state_dir}) for s in sites}
        cursor_name = "offset" if operation == "journal" else "journal_offset"
        result = across(sites, operation, per_site={s.name: {cursor_name: cursors.get(keys[s.name], 0)}
                                                  for s in sites}, **options)
        journal = Journal(state_dir)
        for name, response in result.items():
            if "data" in response:
                batch = response["data"] if operation == "journal" else response["data"]["journal"]
                journal.merge(batch["records"])
                cursors[keys[name]] = batch["next_offset"]
        write_json(cursor_path, cursors)
    return result


def submit(site, config, *, state_dir, campaign="AdHoc", label=None, checkpoint=None, chain=1):
    journal = Journal(state_dir)
    submission = journal.append("submission_requested", site=site.name,
                                config_hash=config.config_hash, campaign=campaign,
                                label=label, requested_output_root=site.output_root, chain=chain)
    try:
        result = call(site, "run", settings=config.as_dict(), campaign=campaign,
                      label=label, checkpoint=checkpoint, chain=chain)
    except Exception as error:
        journal.append("submission_error", submission_id=submission["event_id"],
                       site=site.name, config_hash=config.config_hash, campaign=campaign,
                       error=str(error), outcome="unknown; inspect remote journal before retrying")
        raise
    journal.append("submission_accepted", submission_id=submission["event_id"],
                   site=site.name, config_hash=config.config_hash, campaign=campaign, **result)
    for job in result.get("jobs", [result]):
        if job.get("executable"):
            archive_binary(site, job, campaign, state_dir)
    # Execution events arriving later are imported by sync-log (and status jobs).
    sync_journals([site], state_dir)
    return result


def archive_binary(site, job, campaign, state_dir):
    """Use the ordinary retained-build store for execution and archival copies."""
    from .provenance import retain_executable
    from .campaign import record_executable
    archive = Path(state_dir).resolve().parent / "executables"
    destination = archive / digest(job["build"])
    if not (destination / "MTS2D").exists():
        with tempfile.TemporaryDirectory() as temporary:
            origin = str(Path(job["executable"]).parent) + "/"
            if site.ssh:
                origin = site.ssh + ":" + shlex.quote(origin)
            subprocess.run(["rsync", "-a", "-e", "ssh -T", "--include=MTS2D",
                            "--include=build-manifest.json", "--exclude=*", origin, temporary + "/"], check=True)
            retain_executable(Path(temporary) / "MTS2D", archive)
    if executable_identity(destination / "MTS2D") != job["build"]:
        raise ValueError("Local retained build differs from the execution record")
    record_executable(campaign, site.name, job["executable"], job["build"]["executable_sha256"])
    record_executable(campaign, "local", destination / "MTS2D")


def deploy(site, *, include_source=False, local_source=None):
    if not site.ssh:
        raise ValueError("deploy requires a remote site")
    if site.management_module != "MTS_Management":
        raise ValueError("For a new deployment, select MTS_Management and a new remote directory first")
    directory = site.directory
    if not directory.startswith("/"):
        if not directory.startswith("~/"):
            raise ValueError("Remote directory must be absolute or start with ~/")
        # Resolve HOME using SSH, not the local user's home directory.
        home = subprocess.check_output(["ssh", "-T", site.ssh, "printenv HOME"], text=True).strip()
        directory = str(Path(home) / directory[2:])
    source_record = None
    if include_source:
        if not local_source:
            raise ValueError("--source is required when uploading MTS2D")
        source_record = source_identity(local_source)
        source_path = site.source
        if source_path.startswith("~/"):
            home = subprocess.check_output(["ssh", "-T", site.ssh, "printenv HOME"], text=True).strip()
            source_path = str(Path(home) / source_path[2:])
        remote_source = Path(source_path)
        if not remote_source.is_absolute():
            remote_source = Path(directory) / remote_source
        remote_source = Path(posixpath.normpath(str(remote_source)))
        # Resolve '..' before protecting the shared legacy source directory.
        if str(remote_source).rstrip("/").endswith("/simulation/MTS2D"):
            raise ValueError("Use a dedicated current-source snapshot, not ~/simulation/MTS2D")
    subprocess.run(["ssh", "-T", site.ssh, "mkdir -p -- " + shlex.quote(directory)], check=True)
    exclude = [".git", ".venv", "__pycache__", "*.egg-info", ".state", "output", "build*", "executables", "sites.toml"]
    args = ["rsync", "-a", "-e", "ssh -T"]
    for item in exclude:
        args += ["--exclude", item]
    subprocess.run(args + [str(PACKAGE_ROOT) + "/", f"{site.ssh}:{shlex.quote(directory)}/"], check=True)
    # Native CLI use on the cluster must work too, without the initiating client.
    native = asdict(site)
    native.pop("name")
    native["ssh"] = ""
    native["directory"] = directory
    slurm = native.pop("slurm")
    environment = native.pop("environment")
    text = "[sites.local]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in native.items()) + "\n"
    if slurm:
        text += "[sites.local.slurm]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in slurm.items()) + "\n"
    if environment:
        text += "[sites.local.environment]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in environment.items()) + "\n"
    with tempfile.TemporaryDirectory() as temporary:
        config = Path(temporary) / "sites.toml"
        config.write_text(text)
        subprocess.run(["rsync", "-a", "--ignore-existing", "-e", "ssh -T", str(config),
                        f"{site.ssh}:{shlex.quote(directory)}/"], check=True)
    if include_source:
        staging = remote_source.parent / (".mts-source-upload-" + secrets.token_hex(8))
        target = f"{site.ssh}:{shlex.quote(str(staging))}/"
        subprocess.run(["ssh", "-T", site.ssh, "mkdir -p -- " + shlex.quote(str(staging))], check=True)
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "source.tar.gz"
            if source_archive(local_source, archive) != source_record:
                raise RuntimeError("Source changed during deployment")
            subprocess.run(["rsync", "-a", "-e", "ssh -T", str(archive), target], check=True)
        call(site, "install_source", staging=str(staging), expected=source_record, timeout=120)
    return {"site": site.name, "directory": directory, "source": source_record,
            "remote": call(site, "info")}


def fetch(site, config_hash, destination, *, kind="macro"):
    return fetch_many(site, [config_hash], destination, kind=kind)[0]


def fetch_many(site, config_hashes, destination, *, kind="macro", completed_only=False):
    """Validate one inventory, then transfer selected runs in one rsync call.

    completed_only skips absent/not-yet-terminated campaign members explicitly.
    A receipt records this retrieval; simulation identities are never rewritten.
    """
    if kind not in {"macro", "full"}:
        raise ValueError("Fetch kind must be macro or full")
    hashes = list(config_hashes)
    if len(set(hashes)) != len(hashes) or any(
            not re.fullmatch(r"[0-9a-f]{20,64}", h) for h in hashes):
        raise ValueError("Provide distinct configuration hashes of 20–64 hex characters")
    if not hashes:
        return []
    available = call(site, "inventory")
    selected = {}
    for prefix in hashes:
        matches = [r for r in available if r["config_hash"].startswith(prefix)]
        if len(matches) > 1 or (not matches and not completed_only):
            raise ValueError(f"Expected one run for {prefix}; found {len(matches)}")
        if matches and (not completed_only or matches[0].get("termination") is not None):
            row = matches[0]
            if row["config_hash"] in selected:
                raise ValueError("Multiple requested hashes select the same run")
            selected[row["config_hash"]] = row
    rows = sorted(selected.values(), key=lambda r: r["config_hash"])
    if not rows:
        return []
    from .campaign import install_campaign_files
    names = sorted({r["campaign"] for r in rows if r.get("campaign") not in (None, "AdHoc", "GUI")})
    if names:
        install_campaign_files(call(site, "campaign_files", names=names))
    roots = {Path(r["folder"]).parent for r in rows}
    if len(roots) != 1:
        raise ValueError("Batch fetch requires runs in one output root")
    root = roots.pop()
    destination = Path(destination).resolve()
    for row in rows:
        source = Path(row["folder"])
        if not site.ssh and destination.is_relative_to(source):
            raise ValueError("Fetch destination must be outside the source run")
        target = destination / source.name
        if not site.ssh and target == source.resolve():
            raise ValueError("Fetch destination must differ from the source run")
        if target.exists() and any(target.iterdir()):
            manifest = target / "manifest.json"
            if not manifest.exists() or file_digest(manifest) != row["manifest_sha256"]:
                raise ValueError(f"Download destination contains a different/unmanaged run: {target}")
    destination.mkdir(parents=True, exist_ok=True)
    origin = f"{site.ssh}:{shlex.quote(str(root))}/" if site.ssh else str(root) + "/"
    filters = (["--include=/*/", "--include=macroData.csv", "--include=config.conf",
                "--include=manifest.json", "--include=termination.json", "--exclude=*"]
               if kind == "macro" else [])
    with tempfile.TemporaryDirectory() as temporary:
        listing = Path(temporary) / "runs.txt"
        listing.write_text("".join(Path(r["folder"]).name + "/\n" for r in rows))
        subprocess.run(["rsync", "-ar", "-e", "ssh -T", "--files-from=" + str(listing),
                        *filters, origin, str(destination) + "/"], check=True)
    for row in rows:
        if site.ssh and row.get("executable") and row.get("build"):
            archive_binary(site, row, row.get("campaign"), PACKAGE_ROOT / ".state")
        target = destination / Path(row["folder"]).name
        if file_digest(target / "manifest.json") != row["manifest_sha256"]:
            raise ValueError(f"Transferred manifest differs from inventory: {target}")
        if Config.read(target / "config.conf", expected_name=target.name).config_hash != row["config_hash"]:
            raise ValueError(f"Transferred configuration differs from inventory: {target}")
        if row.get("termination") is None:
            (target / "termination.json").unlink(missing_ok=True)
    write_json(destination / "snapshot.json", dict(fetched_at=now(), site=site.name,
               source_root=str(root), kind=kind, runs=[dict(config_hash=r["config_hash"],
               manifest_sha256=r["manifest_sha256"]) for r in rows]))
    return [str(destination / Path(r["folder"]).name) for r in rows]
