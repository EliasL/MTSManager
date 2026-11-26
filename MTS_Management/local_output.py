"""Configure and relocate local simulation output roots."""

from contextlib import ExitStack
from dataclasses import asdict, replace
import os
from pathlib import Path
import re
import shutil
import socket
from tempfile import TemporaryDirectory
import time
import traceback

from .archive import archive_root, check_inactive
from .continuation import checkpoint_files, latest_checkpoint
from .data import last_macro_row
from .errors import ExpectedError
from .files import attempt_directories, locked, read_json, write_json
from .provenance import executable_identity
from .run_records import read_run_for_execution
from .runner import ACTIVE, MOVED_OUTPUT, attempt_status, completed_run, stop, validate_execution_build


ROOT_LOCKS = {".inventory.lock", ".prepare.lock"}
INVENTORY_FILES = {".inventory.json", ".inventory.export.lock", ".inventory-refresh",
                   ".inventory.sqlite3", ".inventory.sqlite3-journal", ".inventory.sqlite3-shm", ".inventory.sqlite3-wal"}


def _settings_path(site_file):
    path = Path(site_file).resolve()
    return path.parent / ".state" / f"{path.stem}-local-output.json"


def local_output_settings(site_file):
    path = _settings_path(site_file)
    return read_json(path) if path.is_file() else {"sites": {}, "previous_outputs": {}}


def _locations(site_file, name, destination, *, moving):
    from .cluster import load_sites, paths, require_available_output

    sites = load_sites(site_file, include_disabled=True)
    site = sites.get(name)
    if site is None or site.ssh or site.disabled or site.mode == "slurm":
        raise ExpectedError("Select an enabled local site that runs without Slurm.")
    location = paths(site)
    source = Path(location["output_root"])
    target = Path(destination.strip()).expanduser()
    if not target.is_absolute():
        raise ExpectedError("Enter an absolute destination folder path.")
    target = target.resolve()
    if target.is_mount():
        target = target / "MTS"
    if not target.parent.is_dir():
        raise ExpectedError(f"Mount the destination drive or select an existing parent folder: {target.parent}")

    def overlap(first, second):
        return first.is_relative_to(second) or second.is_relative_to(first)

    protected = [Path(site_file).resolve(), _settings_path(site_file).parent]
    local_roots = []
    for profile in sites.values():
        if not profile.ssh:
            other = paths(profile)
            root = Path(other["output_root"])
            roots = (root, archive_root(root))
            local_roots.extend(roots)
            state = Path(other["state_dir"])
            protected.extend((state, Path(other["source"]), Path(other["executable"]).parent,
                              state.parent / "executables"))
            if moving and profile.name != name and any(overlap(old, reserved)
                    for old in (source, archive_root(source)) for reserved in roots):
                raise ExpectedError(f"The output overlaps site {profile.name}. Choose a separate output directory for that site first.")
    if moving:
        require_available_output(site, location)
        if source.is_mount() or any(overlap(old, reserved) for old in (source, archive_root(source)) for reserved in protected):
            raise ExpectedError("Moving output requires a dedicated output folder separate from workspace and simulator files.")
        if archive_root(source).is_symlink():
            raise ExpectedError("Use a physical directory for the site's archive before moving output.")
        if (source / MOVED_OUTPUT).is_file():
            raise ExpectedError("This output directory moved. Reload the site settings before moving output.")
    for folder in (target, archive_root(target)):
        if any(overlap(folder, reserved) for reserved in protected + local_roots):
            raise ExpectedError(f"Choose a destination separate from existing site, simulator and workspace paths: {folder}")
    return site, source, target


def _check_target(target, *, previous=False):
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise ExpectedError(f"Choose a physical directory: {target}")
    if target.exists():
        allowed = ROOT_LOCKS | {MOVED_OUTPUT} if previous else set()
        if any(entry.name not in allowed for entry in target.iterdir()):
            raise ExpectedError(f"Choose an empty folder or enter a new folder path: {target}")


def _active_runs(root):
    active, blocked = [], []
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        if folder.is_symlink() or not (folder / "manifest.json").is_file():
            continue
        attempts = folder / "attempts"
        running = []
        for attempt in attempt_directories(attempts) if attempts.is_dir() else ():
            status = attempt_status(attempt)
            if status["state"] in ACTIVE or status.get("process_alive"):
                running.append((attempt, status))
        if not running:
            # A simulator started outside a managed attempt can still own the run lock.
            try:
                with locked(folder / ".run.lock", blocking=False):
                    if attempts.is_dir():
                        check_inactive(folder)
            except (BlockingIOError, ExpectedError):
                blocked.append(f"{folder.name}: wait for the active process to exit.")
            continue
        if len(running) != 1:
            blocked.append(f"{folder.name}: wait for all active attempts to exit.")
            continue
        attempt, status = running[0]
        request = read_json(attempt / "request.json")
        try:
            pid = status.get("launcher_pid")
            if (request["host"] != socket.gethostname() or not status.get("process_alive") or
                    status.get("scheduler_id") or str(folder) != request["output_folder"] or
                    not request.get("command") or os.getpgid(pid) != pid):
                raise ExpectedError("wait for the run to finish or stop it from its original session.")
            config, manifest = read_run_for_execution(folder)
            build = executable_identity(request["executable"])
            if (build.get("execution_interface") != "folder-v1" or
                    manifest["executable_sha256"] != build["executable_sha256"] or
                    not (config["writeDumps"] or config["checkpointInterval"])):
                raise ExpectedError("the saved executable or configuration does not support checkpoint resume.")
            validate_execution_build(config, build)
            row = last_macro_row(folder / "macroData.csv")
            checkpoint, _, _ = latest_checkpoint(checkpoint_files(folder), float(row["load"]) if row else 0)
        except (ExpectedError, ValueError, OSError) as error:
            blocked.append(f"{folder.name}: {error}")
            continue
        active.append({"attempt": str(attempt), "run": folder.name, "load": checkpoint["load"]})
    return active, blocked


def plan_local_output(site_file, site, destination, *, move):
    """Review a destination and report local attempts requiring a stop."""
    profile, source, target = _locations(site_file, site, destination, moving=move)
    previous = local_output_settings(site_file)["previous_outputs"].get(profile.name, [])
    _check_target(target, previous=move and str(target) in previous)
    _check_target(archive_root(target))
    active, blocked = _active_runs(source) if move else ([], [])
    return {"source": str(source), "destination": str(target), "active": active, "blocked": blocked}


def _wait_for_stop(attempts):
    deadline = time.monotonic() + 30
    for attempt in attempts:
        folder = attempt.parents[1]
        while True:
            try:
                with locked(folder / ".run.lock", blocking=False):
                    check_inactive(folder)
                break
            except (BlockingIOError, ExpectedError) as error:
                if time.monotonic() >= deadline:
                    raise ExpectedError(f"The run has not stopped: {folder.name}. Check Monitor data before retrying.") from error
                time.sleep(.1)


def _copy_outputs(source, target, *, previous, settings_path, settings, log):
    pairs = ((source, target), (archive_root(source), archive_root(target)))
    published = []
    with ExitStack() as stack:
        staged = []
        for old, new in pairs:
            if not old.exists():
                continue
            if old.is_symlink() or not old.is_dir():
                raise ExpectedError(f"Output must be a physical directory: {old}")
            temporary = Path(stack.enter_context(TemporaryDirectory(prefix=".mts-output-", dir=new.parent)))
            log(f"Copying {old} to {new}.")

            def ignore(directory, entries):
                if Path(directory) != old or old != source:
                    return ()
                return set(entries) & (ROOT_LOCKS | INVENTORY_FILES)

            shutil.copytree(old, temporary, dirs_exist_ok=True, symlinks=True, ignore=ignore)
            staged.append((temporary, new))
        active, blocked = _active_runs(source)
        if active or blocked:
            raise ExpectedError("Activity was detected during the copy. Check Monitor data and retry the move.")
        _check_target(target, previous=previous)
        _check_target(archive_root(target))
        try:
            for temporary, new in staged:
                new.mkdir(exist_ok=True)
                for entry in temporary.iterdir():
                    path = new / entry.name
                    if path.exists() or path.is_symlink():
                        raise ExpectedError(f"Destination changed during the copy: {path}")
                    entry.rename(path)
                    published.append(path)
            (source / MOVED_OUTPUT).write_text(str(target))
            write_json(settings_path, settings)
        except BaseException:
            (source / MOVED_OUTPUT).unlink(missing_ok=True)
            for path in reversed(published):
                _remove(path)
            raise


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _resume_attempts(profile, root, attempts, log):
    from .cluster import paths
    from .submission import submit_run

    location = paths(profile)
    jobs, errors = [], {}
    for previous in attempts:
        folder = root / previous.parents[1].name
        try:
            request = read_json(folder / "attempts" / previous.name / "request.json")
            config, manifest = read_run_for_execution(folder)
            if completed_run(folder, config):
                continue
            checkpoint_files(folder)
            site = replace(profile, executable=request["executable"], output_root=str(root),
                           mode="detached", environment=request["environment"])
            receipt = submit_run(site, config, state_dir=location["state_dir"],
                                 campaign=manifest["campaign"], checkpoint="latest")
            jobs.extend(receipt["jobs"])
            log(f"Submitted {folder.name} to resume from its latest checkpoint.")
        except Exception as error:
            errors[folder.name] = str(error)
            log(f"Resume failed for {folder.name}: {error}")
            if not isinstance(error, ExpectedError):
                log(traceback.format_exc())
    return jobs, errors


def change_local_output(site_file, site, destination, *, move, source=None, approved_attempts=(),
                        new_site=None, log=print):
    """Create a local output site or move a reviewed output root and resume its runs."""
    from .cluster import load_sites

    path = _settings_path(site_file)
    with locked(path.with_suffix(".lock")):
        profile, root, target = _locations(site_file, site, destination, moving=move)
        settings = local_output_settings(site_file)
        previous = settings["previous_outputs"].get(site, [])
        _check_target(target, previous=move and str(target) in previous)
        _check_target(archive_root(target))
        if not move:
            if (not isinstance(new_site, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,39}", new_site) or
                    new_site in load_sites(site_file, include_disabled=True)):
                raise ExpectedError("Enter a unique site name starting with a letter, using letters, digits, hyphens or underscores.")
            definition = asdict(replace(profile, name=new_site, output_root=str(target), require_output_root=True))
            definition.pop("name")
            settings["sites"][new_site] = definition
            try:
                target.mkdir(parents=True, exist_ok=True)
                write_json(path, settings)
            except OSError as error:
                raise ExpectedError(f"Cannot create the local output site at {target}: {error}") from error
            return {"site": new_site, "output_root": str(target), "resumed": [], "errors": {}}
        if source != str(root):
            raise ExpectedError("The site's output changed. Review the move again.")
        if not isinstance(approved_attempts, (list, tuple)) or any(not isinstance(attempt, str) for attempt in approved_attempts):
            raise ExpectedError("Review and approve the active attempts before moving output.")

        stopped = []
        moved = False
        errors = {}
        try:
            target.mkdir(parents=True, exist_ok=True)
            with locked(root / ".inventory.lock"), locked(root / ".prepare.lock"):
                active, blocked = _active_runs(root)
                if blocked:
                    raise ExpectedError("Wait for these runs before moving output. " + " ".join(blocked))
                if any(run["attempt"] not in approved_attempts for run in active):
                    raise ExpectedError("Active runs changed. Review the move and approve the current attempts.")
                for run in active:
                    attempt = Path(run["attempt"])
                    try:
                        stop(attempt)
                    except (ValueError, ProcessLookupError):
                        if attempt_status(attempt)["state"] in ACTIVE:
                            raise
                    else:
                        stopped.append(attempt)
                        log(f"Stopping {run['run']} for the move.")
                _wait_for_stop(stopped)
                settings["previous_outputs"][site] = list(dict.fromkeys([*previous, str(root)]))
                settings["sites"].setdefault(site, {}).update(output_root=str(target), require_output_root=True)
                _copy_outputs(root, target, previous=str(target) in previous,
                              settings_path=path, settings=settings, log=log)
                moved = True
                (target / MOVED_OUTPUT).unlink(missing_ok=True)
                for old in (root, archive_root(root)):
                    try:
                        for entry in old.iterdir() if old.is_dir() else ():
                            if entry.name not in ROOT_LOCKS | {MOVED_OUTPUT}:
                                _remove(entry)
                        if old != root and old.is_dir():
                            old.rmdir()
                    except OSError as error:
                        errors[str(old)] = f"Output moved. Could not remove all original files: {error}"
                        log(errors[str(old)])
        except (OSError, ExpectedError) as error:
            if not stopped:
                if isinstance(error, ExpectedError):
                    raise
                raise ExpectedError(f"Cannot move output to {target}: {error}") from error
            errors["move"] = str(error)
            log(f"Output move failed: {error}")
        finally:
            location = target if moved else root
            resumed, failures = _resume_attempts(profile, location, stopped, log)
            errors.update(failures)
        return {"site": site, "output_root": str(location), "resumed": resumed, "errors": errors}
