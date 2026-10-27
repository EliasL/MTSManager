"""A single execution path for foreground, detached and Slurm simulations."""

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from .config import Config
from .files import digest, file_digest, locked, now, read_json, write_json
from .journal import Journal
from .inventory_cache import invalidate as invalidate_inventory
from .provenance import executable_identity, scripts_identity

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
ACTIVE = {"prepared", "queued", "running", "stopping"}
_PRINT_LOCK = threading.Lock()


def process_token(pid):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def python_command(*args):
    return [sys.executable, "-u", "-m", "MTS_Management", *map(str, args)]


def cpu_allocation():
    if not hasattr(os, "sched_getaffinity"):
        return None
    cpus = sorted(os.sched_getaffinity(0))
    cores = set()
    for cpu in cpus:
        topology = Path(f"/sys/devices/system/cpu/cpu{cpu}/topology")
        cores.add((int((topology/"physical_package_id").read_text()),
                   int((topology/"core_id").read_text())))
    return {"logical_cpus": cpus, "physical_core_ids": sorted(cores),
            "physical_cores": len(cores)}


def prepare(config, *, executable, output_root, state_dir, campaign="AdHoc", label=None,
            checkpoint=None, site="local", environment=None, after=None):
    executable = Path(executable).resolve()
    build = executable_identity(executable)
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    resume_latest = checkpoint == "latest"
    checkpoint = Path(checkpoint).resolve() if checkpoint and not resume_latest else None
    after = Path(after).resolve() if after else None
    if resume_latest and not config["writeDumps"] and not config["checkpointInterval"]:
        raise ValueError("Latest-checkpoint resume requires dumps or periodic checkpoints")
    if checkpoint and not checkpoint.is_file():
        raise FileNotFoundError(checkpoint)
    with locked(output_root / ".prepare.lock"):
        index_path = output_root / ".run-index.json"
        index = read_json(index_path) if index_path.exists() else {}
        folder = output_root / config.folder_name(label)
        matches = [p for h, p in index.items() if h.startswith(config.config_hash[:20])]
        if any(h != config.config_hash and h.startswith(config.config_hash[:20]) for h in index):
            raise ValueError("Truncated hash collision; extend the naming scheme")
        if matches:
            folder = output_root / matches[0]
        manifest_path = folder / "manifest.json"
        if after:
            if not resume_latest or after.parent != folder / "attempts":
                raise ValueError("A continuation must resume the same run's latest checkpoint")
            attempts = sorted((folder / "attempts").iterdir(),
                              key=lambda a: read_json(a / "request.json")["created_at"])
            if after != attempts[-1] or not read_json(after / "status.json").get("scheduler_id"):
                raise ValueError("Chain predecessor must be the latest submitted Slurm attempt")
        identity = {"config_hash": config.config_hash, "settings": config.as_dict(),
                    "executable_sha256": build["executable_sha256"]}
        if manifest_path.exists():
            old = read_json(manifest_path)
            if any(old[k] != v for k, v in identity.items()):
                raise ValueError("Output belongs to different settings/executable; use another output root")
            previous_statuses = [read_json(a / "status.json") for a in (folder / "attempts").iterdir()]
            for status in previous_statuses:
                if status["state"] in ACTIVE and not after:
                    raise RuntimeError(f"An active attempt already exists in {folder / 'attempts'}")
            if checkpoint is None and not resume_latest and (any("started_at" in s for s in previous_statuses) or
                                       (folder / "macroData.csv").exists()):
                raise FileExistsError(f"Run already exists; explicitly resume a checkpoint: {folder}")
            if Config.read(folder / "config.conf", expected_name=folder.name).config_hash != config.config_hash:
                raise ValueError("Saved configuration changed")
        else:
            if folder.exists():
                raise FileExistsError(f"Unmanaged output directory: {folder}")
            if checkpoint is not None or resume_latest:
                raise ValueError("Resume requires the original managed output folder and configuration")
            folder.mkdir()
            (folder / "config.conf").write_text(config.text(folder.name))
            write_json(manifest_path, identity | {"format": 1, "created_at": now(),
                       "campaign": campaign, "build": build})
        if checkpoint and not checkpoint.is_relative_to(folder):
            raise ValueError("Resume checkpoint must belong to this run")
        invalidate_inventory(output_root, config.config_hash)
        attempt_id = uuid.uuid4().hex
        attempt = folder / "attempts" / attempt_id
        attempt.mkdir(parents=True)
        inputs = {"checkpoint": str(checkpoint), "sha256": file_digest(checkpoint)} if checkpoint else None
        run_identity = digest({"config": config.config_hash, "executable": build["executable_sha256"],
                               "input": inputs["sha256"] if inputs else None})
        request = {"attempt_id": attempt_id, "run_identity": run_identity,
                   "config_hash": config.config_hash, "campaign": campaign,
                   "site": site, "host": socket.gethostname(), "output_folder": str(folder),
                   "state_dir": str(Path(state_dir).resolve()), "executable": str(executable),
                   "build": build, "scripts": scripts_identity(), "input": inputs,
                   "resume_latest": resume_latest, "predecessor": str(after) if after else None,
                   "environment": dict(environment or {}),
                   "created_at": now()}
        write_json(attempt / "request.json", request)
        write_json(attempt / "status.json", {"state": "prepared", "updated_at": now()})
        index[config.config_hash] = folder.name
        write_json(index_path, index)
        Journal(state_dir).append("requested", **event_fields(request))
        return attempt


def event_fields(request):
    return {k: request[k] for k in ("attempt_id", "run_identity", "config_hash", "campaign",
                                   "site", "output_folder")}


def set_status(attempt, state, **fields):
    attempt = Path(attempt)
    with locked(attempt / ".status.lock"):
        status = read_json(attempt / "status.json")
        if status["state"] == "finished" and state != "finished":
            request = read_json(attempt / "request.json")
            invalidate_inventory(attempt.parents[2], request["config_hash"])
        status.update(fields, state=state, updated_at=now())
        write_json(attempt / "status.json", status)
    request = read_json(attempt / "request.json")
    Journal(request["state_dir"]).append(state, **event_fields(request), **fields)


def launch(attempt, *, mode="foreground", slurm=None, setup=""):
    attempt = Path(attempt).resolve()
    request = read_json(attempt / "request.json")
    result_fields = {"attempt": str(attempt), "attempt_id": request["attempt_id"],
                     "output_folder": request["output_folder"]}
    cmd = python_command("_execute", attempt)
    try:
        if mode == "foreground":
            execute(attempt)
            return result_fields | {"state": "finished"}
        if mode == "detached":
            with (attempt / "launcher.log").open("ab") as log:
                process = subprocess.Popen(cmd, cwd=PACKAGE_ROOT, stdout=log, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
            # Reap it if this caller stays alive; detachment still survives caller exit.
            threading.Thread(target=process.wait, daemon=True).start()
            with locked(attempt / ".status.lock"):
                status = read_json(attempt / "status.json")
                write_json(attempt / "status.json", status | {
                    "launcher_pid": process.pid, "launcher_token": process_token(process.pid)})
            return result_fields | {"pid": process.pid}
        if mode != "slurm":
            raise ValueError(f"Unknown execution mode: {mode}")
        slurm = slurm or {}
        allowed = {"time", "partition", "account", "qos", "mem", "cpus-per-task"}
        if slurm.keys() - allowed:
            raise ValueError(f"Unknown Slurm options: {slurm.keys() - allowed}")
        request = read_json(attempt / "request.json")
        threads = Config.read(Path(request["output_folder"]) / "config.conf")["nrThreads"]
        if int(slurm.get("cpus-per-task", threads)) < threads:
            raise ValueError("Slurm CPU allocation is smaller than nrThreads")
        args = ["sbatch", "--parsable", f"--job-name={Path(request['output_folder']).name}",
                f"--output={attempt / 'launcher.log'}", "--nodes=1", "--ntasks=1"]
        if request.get("predecessor"):
            predecessor = read_json(Path(request["predecessor"]) / "status.json")["scheduler_id"]
            args += [f"--dependency=afterany:{predecessor}"]
        args += [f"--{k}={v}" for k, v in ({"cpus-per-task": threads} | slurm).items()]
        script = attempt / "submit.sh"
        script.write_text("#!/bin/bash\nset -euo pipefail\n" + setup + "\ncd " +
                          shlex.quote(str(PACKAGE_ROOT)) + "\nexec " + shlex.join(cmd) + "\n")
        # Mark pending before sbatch: a fast scheduler may start the worker immediately.
        set_status(attempt, "queued", slurm={"cpus-per-task": threads} | slurm,
                   predecessor=request.get("predecessor"))
        result = subprocess.run(args + [str(script)], capture_output=True, text=True, check=True)
        job_id = result.stdout.strip().split(";", 1)[0]
        if not job_id.isdigit():
            raise RuntimeError(f"Unrecognized sbatch response: {result.stdout!r}")
        # Preserve a worker state that may already have advanced past queued.
        with locked(attempt / ".status.lock"):
            status = read_json(attempt / "status.json")
            write_json(attempt / "status.json", status | {"scheduler_id": job_id})
        Journal(request["state_dir"]).append("submitted", **event_fields(request), scheduler_id=job_id)
        return result_fields | {"scheduler_id": job_id}
    except BaseException as error:
        if read_json(attempt / "status.json")["state"] in {"prepared", "queued"}:
            set_status(attempt, "failed", error=str(error))
        raise


def execute(attempt):
    attempt = Path(attempt).resolve()
    request = read_json(attempt / "request.json")
    folder = Path(request["output_folder"])
    with locked(folder / ".run.lock", blocking=False):
        if read_json(attempt / "status.json")["state"] not in {"prepared", "queued"}:
            raise RuntimeError("Attempt has already executed or was cancelled")
        child = None
        previous_handlers = {}
        try:
            if read_json(attempt / "status.json")["state"] not in {"prepared", "queued"}:
                raise RuntimeError("Attempt has already executed or was cancelled")
            config = Config.read(folder / "config.conf", expected_name=folder.name)
            if config.config_hash != request["config_hash"]:
                raise ValueError("Configuration changed after submission")
            if executable_identity(request["executable"]) != request["build"]:
                raise ValueError("Executable/build manifest changed after submission")
            execution_scripts = scripts_identity()
            if request.get("predecessor"):
                previous = Path(request["predecessor"])
                job = read_json(previous / "status.json")["scheduler_id"]
                accounting = subprocess.run(["sacct", "-X", "-n", "-P", "-j", job,
                    "--format=JobIDRaw,State"], capture_output=True, text=True, check=True)
                rows = [line.split("|") for line in accounting.stdout.splitlines()]
                state = next((r[1].split()[0] for r in rows if r[0] == job), None)
                if state not in {"COMPLETED", "TIMEOUT", "PREEMPTED", "NODE_FAIL"}:
                    raise RuntimeError(f"Cannot continue after Slurm job {job}: {state}")
                if read_json(previous / "status.json")["state"] in ACTIVE:
                    set_status(previous, "stopped", scheduler_terminal_state=state,
                               reason="Previous allocation ended before its worker recorded an outcome")
            if request.get("resume_latest"):
                dumps = list((folder / "dumps").glob("*.xml.gz"))
                if not dumps:
                    raise FileNotFoundError(f"No checkpoint available to resume in {folder / 'dumps'}")
                checkpoint = max(dumps, key=lambda p: p.stat().st_mtime_ns).resolve()
                if not checkpoint.is_relative_to(folder):
                    raise ValueError("Latest checkpoint must belong to this run")
                request["input"] = {"checkpoint": str(checkpoint), "sha256": file_digest(checkpoint)}
                request["run_identity"] = digest({"config": config.config_hash,
                    "executable": request["build"]["executable_sha256"],
                    "input": request["input"]["sha256"]})
                write_json(attempt / "request.json", request)
                Journal(request["state_dir"]).append("checkpoint_selected", **event_fields(request),
                                                    input=request["input"])
            cmd = [request["executable"], "-c", str(folder / "config.conf"), "-o", str(folder.parent)]
            if request["input"]:
                item = request["input"]
                if file_digest(item["checkpoint"]) != item["sha256"]:
                    raise ValueError("Checkpoint changed after submission")
                cmd += ["-d", item["checkpoint"]]
            environment = os.environ | request["environment"] | {"OMP_NUM_THREADS": str(config["nrThreads"])}
            allocation = cpu_allocation()
            if (environment.get("OMP_PLACES") == "cores" and allocation is not None
                    and allocation["physical_cores"] < config["nrThreads"]):
                raise RuntimeError("Allocated physical cores are fewer than nrThreads")
            def interrupted(signum, frame):
                raise InterruptedError(f"Received signal {signum}")

            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGTERM, signal.SIGINT):
                    previous_handlers[sig] = signal.signal(sig, interrupted)
            with locked(attempt / ".status.lock"):
                status = read_json(attempt / "status.json")
                if status["state"] not in {"prepared", "queued"}:
                    raise RuntimeError("Attempt was cancelled before execution")
                child = subprocess.Popen(cmd, cwd=folder, env=environment, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                fields = {"pid": child.pid, "process_token": process_token(child.pid),
                          "runner_pid": os.getpid(), "runner_token": process_token(os.getpid()),
                          "command": cmd, "started_at": now(), "execution_scripts": execution_scripts}
                fields.update(cpu_allocation=allocation,
                              environment={k: v for k, v in environment.items()
                                           if k.startswith(("OMP_", "GOMP_", "KMP_"))})
                write_json(attempt / "status.json", status | fields | {"state": "running", "updated_at": now()})
                Journal(request["state_dir"]).append("running", **event_fields(request), **fields)
            tag = folder.name.split("[")[0] + ":" + config.config_hash[:8]
            buffer = bytearray()
            with (attempt / "stdout.log").open("ab", buffering=0) as log:
                while block := os.read(child.stdout.fileno(), 4096):
                    log.write(block)
                    for ch in block:
                        buffer.append(ch)
                        if ch in (10, 13):
                            with _PRINT_LOCK:
                                print(f"[{tag}] {buffer.decode(errors='replace').rstrip()}", flush=True)
                            buffer.clear()
                if buffer:
                    with _PRINT_LOCK:
                        print(f"[{tag}] {buffer.decode(errors='replace')}", flush=True)
            code = child.wait()
            if code:
                raise subprocess.CalledProcessError(code, cmd)
            set_status(attempt, "finished", returncode=code, finished_at=now())
        except BaseException as error:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            state = "stopped" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "failed"
            if child is not None or read_json(attempt / "status.json")["state"] != "stopped":
                set_status(attempt, state, error=str(error),
                           returncode=child.returncode if child else None, finished_at=now())
            raise
        finally:
            if child is not None and child.stdout is not None:
                child.stdout.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def run(config, *, mode="foreground", slurm=None, **options):
    """Prepare and execute one local job; remote callers use cluster.submit."""
    return launch(prepare(config, **options), mode=mode, slurm=slurm)


def stop(attempt):
    attempt = Path(attempt).resolve()
    with locked(attempt / ".status.lock"):
        status = read_json(attempt / "status.json")
        state = "stopped"
        if status["state"] not in ACTIVE:
            raise ValueError(f"Attempt is already {status['state']}")
        if "scheduler_id" in status:
            cancelled = subprocess.run(["scancel", status["scheduler_id"]], capture_output=True, text=True)
            if cancelled.returncode == 0 and status["state"] in {"running", "stopping"}:
                state = "stopping"
            elif cancelled.returncode:
                # A worker killed by the scheduler may have no final journal event.
                accounting = subprocess.run(["sacct", "-X", "-n", "-P", "-j", status["scheduler_id"],
                    "--format=JobIDRaw,State"], capture_output=True, text=True, check=True)
                rows = [row.split("|") for row in accounting.stdout.splitlines()]
                observed = next((r[1].split()[0] for r in rows if r[0] == status["scheduler_id"]), None)
                terminal = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY",
                            "PREEMPTED", "NODE_FAIL", "BOOT_FAIL", "DEADLINE", "REVOKED"}
                if observed not in terminal:
                    raise RuntimeError(f"Cannot cancel/confirm job state: {cancelled.stderr}")
                status["scheduler_terminal_state"] = observed
        elif status["state"] == "running":
            if (not status.get("runner_token") or
                    process_token(status["runner_pid"]) != status["runner_token"]):
                # Recover a run whose wrapper was killed; never signal a reused PID.
                if status.get("process_token") and process_token(status["pid"]) == status["process_token"]:
                    os.killpg(status["pid"], signal.SIGTERM)
                    deadline = time.monotonic()+5
                    while process_token(status["pid"]) == status["process_token"]:
                        if time.monotonic() >= deadline:
                            os.killpg(status["pid"], signal.SIGKILL)
                            break
                        time.sleep(.05)
            else:
                os.kill(status["runner_pid"], signal.SIGTERM)
                state = "stopping"
        elif status["state"] == "prepared":
            pid = status.get("launcher_pid")
            if pid and status.get("launcher_token") and process_token(pid) == status["launcher_token"]:
                os.kill(pid, signal.SIGTERM)
        else:
            raise RuntimeError("Queued submission has no scheduler ID; inspect launcher and journal")
        write_json(attempt / "status.json", status | {"state": state, "updated_at": now()})
    request = read_json(attempt / "request.json")
    Journal(request["state_dir"]).append(state, **event_fields(request), reason="Stop requested")


def run_many(attempts, workers=2):
    if workers < 1:
        raise ValueError("workers must be positive")
    processes = []
    guard = threading.Lock()
    cancelled = threading.Event()

    def run(attempt):
        with guard:
            if cancelled.is_set():
                set_status(attempt, "stopped", reason="Batch interrupted")
                return
            process = subprocess.Popen(python_command("_execute", attempt), cwd=PACKAGE_ROOT,
                                       start_new_session=True)
            processes.append(process)
        code = process.wait()
        if code:
            raise subprocess.CalledProcessError(code, process.args)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            futures = [pool.submit(run, attempt) for attempt in attempts]
            for future in as_completed(futures):
                future.result()
        except BaseException:
            with guard:
                cancelled.set()
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
            raise
