"""Behavior tests use isolated fake executables; integration smoke uses real MTS2D."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from MTS_Management.cluster import Site, call, deploy, fetch_many, paths, serve, submit, sync_journals
from MTS_Management.config import Config, DEFAULTS
from MTS_Management.data import read_macros
from MTS_Management.files import digest, file_digest, read_json, write_json
from MTS_Management.inventory import estimate_size, inventory, progress, rebuild_index
from MTS_Management.inventory_cache import snapshot as cached_inventory
from MTS_Management.journal import Journal
from MTS_Management.provenance import build
from MTS_Management.runner import PACKAGE_ROOT, execute, launch, prepare, run_many, stop


class ConfigTests(unittest.TestCase):
    def test_hash_complete_stable_and_changes_with_settings(self):
        a = Config(rows=10, totalLoad=1)
        b = Config(**dict(reversed(list(a.as_dict().items()))))
        self.assertEqual(a.config_hash, b.config_hash)
        self.assertNotEqual(a.config_hash, Config(rows=10, totalLoad=2).config_hash)
        self.assertEqual(a.config_hash, Config(rows=10, totalLoad=1.0).config_hash)
        self.assertEqual(a.folder_name("First").split("[")[1], a.folder_name("Second").split("[")[1])

    def test_current_schema_and_source_archive_include_cholmod_inputs(self):
        from MTS_Management.provenance import source_archive, verify_schema
        import tarfile
        source = PACKAGE_ROOT.parent / "MTS2D"
        if not source.exists():
            self.skipTest("Simulator source is not present")
        verify_schema(source)
        self.assertEqual(Config()["tangentBackend"], "cholmod")
        self.assertNotEqual(Config().config_hash, Config(tangentBackend="eigen").config_hash)
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "source.tar.gz"
            source_archive(source, archive)
            with tarfile.open(archive) as snapshot:
                self.assertIn("cmake/cholmod/CMakeLists.txt", snapshot.getnames())

    def test_strict_complete_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.conf"
            config = Config(directions="horizontal,-vertical", segmentLoad=.2, repeat=True)
            config.write(path, "Campaign")
            self.assertEqual(Config.read(path).as_dict(), config.as_dict())
            path.write_text(path.read_text().replace("seed = 0\n", ""))
            with self.assertRaises(ValueError):
                Config.read(path)
        for settings in ({"tangentBackend": "unknown"}, {"experiment": "simpleShear"}, {"rows": True}, {"epsR": float("nan")},
                         {"totalLoad": -1}, {"loadIncrement": 0}, {"directions": "x"},
                         {"loading": "noRelaxation", "stepNoise": 1}):
            with self.assertRaises((ValueError, TypeError)):
                Config(**settings)
        Config(boundary="periodicAnchored", deformation="directDisplacement")


class DeploymentTests(unittest.TestCase):
    def test_manager_update_preserves_worker_paths_and_activates_only_verified_profiles(self):
        from MTS_Management.cluster import load_sites
        from MTS_Management.deployments import update_managers
        from MTS_Management.provenance import scripts_identity
        expected = scripts_identity()
        with tempfile.TemporaryDirectory() as temporary:
            sites_file = Path(temporary) / "sites.toml"
            text = '\n'.join(f'[sites.{name}]\nssh = "fixture"\ndirectory = "~/workers/{name}"\n'
                             'management_module = "mtscripts"\n' for name in ("first", "second"))
            sites_file.write_text(text)
            def verify(site, operation, **options):
                self.assertEqual(site.directory, "~/workers/" + site.name)
                self.assertEqual(site.manager_directory, "~/.local/share/mts/manager")
                if site.name == "second":
                    raise ValueError("verification failed")
                return {"scripts": expected}
            with patch("MTS_Management.deployments.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, "", "")), \
                    patch("MTS_Management.deployments.call", side_effect=verify):
                result = update_managers(sites_file, log=lambda _: None)
                update_managers(sites_file, log=lambda _: None)
            loaded = load_sites(sites_file)
            self.assertTrue(loaded["first"].manager_directory)
            self.assertEqual(loaded["second"].manager_directory, "")
            self.assertEqual(loaded["first"].management_module, "mtscripts")
            self.assertEqual(result["updated_sites"], ["first"])
            self.assertIn("second", result["errors"])
            self.assertEqual(sites_file.read_text(), text)

    def test_manager_update_retains_custom_work_storage_location(self):
        from MTS_Management.cluster import load_sites
        from MTS_Management.deployments import update_managers
        from MTS_Management.provenance import scripts_identity
        with tempfile.TemporaryDirectory() as temporary:
            sites = Path(temporary) / "sites.toml"
            sites.write_text('[sites.cluster]\nssh="cluster"\nmanager_directory="/work/MTS/manager"\n')
            def verify(site, *args, **kwargs):
                self.assertEqual(site.manager_directory, "/work/MTS/manager")
                return {"scripts": scripts_identity()}
            with patch("MTS_Management.deployments.subprocess.run", return_value=
                       subprocess.CompletedProcess([], 0, "", "")), \
                    patch("MTS_Management.deployments.call", side_effect=verify):
                result = update_managers(sites, log=lambda _: None)
            self.assertEqual(result["updated_sites"], ["cluster"])
            self.assertEqual(load_sites(sites)["cluster"].manager_directory, "/work/MTS/manager")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.exe = self.root / "build" / "MTS2D"
        self.exe.parent.mkdir()
        self.exe.write_text(f'''#!{sys.executable}
import os,sys,time
from pathlib import Path
settings=dict(line.strip().split(" = ",1) for line in Path(sys.argv[sys.argv.index("-c")+1]).read_text().splitlines())
folder=Path(sys.argv[sys.argv.index("-o")+1])/settings["name"]
print("initial\\rnext",flush=True)
if int(settings["seed"]) == 99:
    sys.exit(7)
if int(settings["seed"]) == 98:
    time.sleep(30)
(folder/"macroData.csv").write_text("load,value\\n0,1\\n0.5,2\\n")
(folder/"dumps").mkdir(exist_ok=True)
(folder/"dumps"/"state.xml.gz").write_text("checkpoint")
print("finished",flush=True)
''')
        self.exe.chmod(0o755)
        write_json(self.exe.parent / "build-manifest.json", {"branch": "main", "revision": "test",
                   "configuration_keys_sha256": digest(sorted(DEFAULTS)),
                   "executable": str(self.exe), "executable_sha256": file_digest(self.exe)})
        self.state = self.root / "state"
        self.output = self.root / "output"

    def prepare(self, config=None, **kwargs):
        return prepare(config or Config(), executable=self.exe, output_root=self.output,
                       state_dir=self.state, campaign="Test", **kwargs)

    def test_manager_update_allows_prepared_job_and_records_execution_version(self):
        attempt = self.prepare()
        submitted = read_json(attempt / "request.json")["scripts"]
        changed = {"version": "updated", "sha256": "different-manager-source"}
        with patch("MTS_Management.runner.scripts_identity", return_value=changed), \
                patch("MTS_Management.runner.process_token", return_value="fixture"), \
                redirect_stdout(io.StringIO()):
            execute(attempt)
        status = read_json(attempt / "status.json")
        self.assertEqual(status["state"], "finished")
        self.assertEqual(status["execution_scripts"], changed)
        self.assertEqual(read_json(attempt / "request.json")["scripts"], submitted)

    def test_inventory_cache_reuses_finished_runs_until_resume_or_hard_refresh(self):
        attempt = self.prepare()
        with redirect_stdout(io.StringIO()):
            execute(attempt)
        with patch("MTS_Management.inventory.read_json", wraps=read_json) as read:
            first = inventory(self.output)
        names = [Path(call.args[0]).name for call in read.call_args_list]
        self.assertEqual(names.count("request.json"), 1)
        self.assertEqual(names.count("status.json"), 1)
        folder = attempt.parents[1]
        (folder / "macroData.csv").write_text("load,value\n0,1\n0.75,2\n")
        with patch("MTS_Management.inventory.Config.read", side_effect=AssertionError("Finished run reread")):
            self.assertEqual(inventory(self.output), first)
        self.assertEqual(inventory(self.output, hard_refresh=True)[0]["progress"]["fraction"], .75)
        self.assertIsNotNone(cached_inventory(self.output)["updated_at"])
        resumed = self.prepare(checkpoint=folder / "dumps/state.xml.gz")
        cached = cached_inventory(self.output)
        self.assertEqual(cached["rows"], [])
        self.assertEqual(cached["pending"], [Config().config_hash])
        self.assertEqual(inventory(self.output)[0]["status"]["state"], "prepared")
        site = Site(name="fixture", directory=str(self.root), output_root=str(self.output))
        exported = serve({"site": asdict(site), "operation": "inventory_update"})
        self.assertEqual(read_json(exported["snapshot"])["rows"][0]["attempt"], str(resumed))
        shutil.rmtree(folder)
        self.assertEqual(inventory(self.output, hard_refresh=True), [])
        self.assertEqual(cached_inventory(self.output)["pending"], [])

    def test_inventory_cache_cannot_resurrect_finished_run_during_restart(self):
        attempt = self.prepare()
        with redirect_stdout(io.StringIO()):
            execute(attempt)
        inventory(self.output)
        original_progress = progress
        def restart_during_read(folder, config):
            self.prepare(checkpoint=Path(folder) / "dumps/state.xml.gz")
            return original_progress(folder, config)
        with patch("MTS_Management.inventory.progress", side_effect=restart_during_read):
            with self.assertRaisesRegex(RuntimeError, "changed during inventory"):
                inventory(self.output, config_hash=Config().config_hash, hard_refresh=True)
        self.assertEqual(cached_inventory(self.output)["rows"], [])
        self.assertEqual(inventory(self.output)[0]["status"]["state"], "prepared")

    def test_site_build_setting_and_cache_override(self):
        cache = self.exe.parent / "CMakeCache.txt"
        cache.write_text("ENABLE_NATIVE_ARCH:BOOL=ON\n")

        def cmake(args, **kwargs):
            if "-S" in args:
                option = next(a for a in args if a.startswith("-DENABLE_NATIVE_ARCH="))
                cache.write_text("ENABLE_NATIVE_ARCH:BOOL=" + option.split("=", 1)[1] + "\n")

        with patch("MTS_Management.provenance.source_identity", return_value={"branch": "main"}), \
             patch("MTS_Management.provenance.verify_schema"), \
             patch("MTS_Management.provenance.subprocess.run", side_effect=cmake):
            # The portable default must replace an existing native build's cache setting.
            record = build(self.root, self.exe.parent)
            self.assertEqual(record["build_options"]["ENABLE_NATIVE_ARCH"], "OFF")
            record = build(self.root, self.exe.parent, native_arch=True)
            self.assertEqual(record["build_options"]["ENABLE_NATIVE_ARCH"], "ON")

        cache.write_text("CMAKE_HOME_DIRECTORY:INTERNAL=/old/source\n")
        with self.assertRaisesRegex(ValueError, "moved"):
            build(self.root, self.exe.parent)

    def test_old_build_schema_is_rejected_before_creating_output(self):
        manifest = self.exe.parent/"build-manifest.json"
        record = read_json(manifest)
        record.pop("configuration_keys_sha256")
        write_json(manifest, record)
        with self.assertRaisesRegex(ValueError, "schema"):
            self.prepare()
        self.assertFalse(self.output.exists())

    def test_site_environment_and_physical_core_allocation(self):
        site = Site(name="magi", slurm_cpus_per_thread=2,
                    environment={"OMP_PLACES": "cores", "OMP_PROC_BIND": "close"})
        settings = Config(nrThreads=2).as_dict()
        with patch("MTS_Management.cluster.prepare", return_value=self.root/"attempt") as prepared, \
             patch("MTS_Management.cluster.launch") as launched:
            serve({"site": asdict(site), "operation": "run", "options": {"settings": settings}})
            self.assertEqual(prepared.call_args.kwargs["environment"], site.environment)
            self.assertEqual(launched.call_args.kwargs["slurm"]["cpus-per-task"], 4)
        attempt = self.prepare(Config(nrThreads=2), environment=site.environment)
        with patch("MTS_Management.runner.cpu_allocation", return_value={"physical_cores": 1}), \
             patch("MTS_Management.runner.subprocess.Popen") as child:
            with self.assertRaisesRegex(RuntimeError, "physical cores"):
                execute(attempt)
            child.assert_not_called()

    def test_execution_logging_resume_and_duplicate_guard(self):
        attempt = self.prepare(label="Readable")
        with self.assertRaises(RuntimeError):
            self.prepare(label="OtherName")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            execute(attempt)
        self.assertIn(f"[Readable:{Config().config_hash[:8]}] initial", stdout.getvalue())
        self.assertIn(f"[Readable:{Config().config_hash[:8]}] next", stdout.getvalue())
        self.assertEqual(read_json(attempt / "status.json")["state"], "finished")
        self.assertEqual([r["event"] for r in Journal(self.state).records()],
                         ["requested", "running", "finished"])
        with self.assertRaises(FileExistsError):
            self.prepare()
        folder = attempt.parent.parent
        resumed = self.prepare(checkpoint=folder / "dumps/state.xml.gz")
        self.assertEqual(resumed.parent, attempt.parent)
        self.assertNotEqual(read_json(resumed / "request.json")["run_identity"],
                            read_json(attempt / "request.json")["run_identity"])
        with redirect_stdout(io.StringIO()):
            execute(resumed)
        info = inventory(self.output, config_hash=Config().config_hash[:20], refresh_sizes=True)
        self.assertEqual(len(info), 1)
        self.assertEqual(info[0]["progress"]["fraction"], .5)
        self.assertTrue(info[0]["size"]["coverage_complete"])
        self.assertEqual(rebuild_index(self.output)[Config().config_hash], folder.name)

    def test_failure_and_changed_executable(self):
        attempt = self.prepare(Config(seed=99))
        with redirect_stdout(io.StringIO()), self.assertRaises(subprocess.CalledProcessError):
            execute(attempt)
        self.assertEqual(read_json(attempt / "status.json")["returncode"], 7)
        attempt = self.prepare(Config(seed=2))
        retained = Path(read_json(attempt / "request.json")["executable"])
        retained.chmod(0o755)
        retained.write_text(retained.read_text()+"\n# changed\n")
        with self.assertRaises(ValueError):
            execute(attempt)
        self.assertEqual(read_json(attempt / "status.json")["state"], "failed")

    def test_prepared_job_retains_original_executable_after_working_build_replacement(self):
        attempt = self.prepare()
        retained = Path(read_json(attempt / "request.json")["executable"])
        original = file_digest(self.exe)
        self.exe.write_text("replacement build")
        self.assertEqual(file_digest(retained), original)
        with redirect_stdout(io.StringIO()):
            execute(attempt)
        self.assertEqual(read_json(attempt / "status.json")["state"], "finished")
        from MTS_Management.provenance import build
        with self.assertRaisesRegex(ValueError, "cannot be rebuilt"):
            build(self.root, retained.parent)

    def test_changed_config_and_checkpoint(self):
        attempt = self.prepare()
        with redirect_stdout(io.StringIO()):
            execute(attempt)
        checkpoint = attempt.parent.parent / "dumps/state.xml.gz"
        resumed = self.prepare(checkpoint=checkpoint)
        checkpoint.write_text("changed")
        with self.assertRaises(ValueError):
            execute(resumed)
        attempt = self.prepare(Config(seed=1))
        config_path = attempt.parent.parent / "config.conf"
        config_path.write_text(config_path.read_text().replace("seed = 1", "seed = 2"))
        with self.assertRaises(ValueError):
            execute(attempt)

    def test_detached_survives_and_stop(self):
        attempt = self.prepare(Config(seed=98))
        result = launch(attempt, mode="detached")
        deadline = time.monotonic()+8
        while read_json(attempt / "status.json")["state"] != "running":
            if time.monotonic() > deadline:
                self.fail((attempt / "launcher.log").read_text())
            time.sleep(.02)
        stop(attempt)
        while time.monotonic() < deadline:
            status = read_json(attempt / "status.json")
            if "finished_at" in status:
                break
            time.sleep(.02)
        self.assertEqual(status["state"], "stopped")
        self.assertIn("finished_at", status)

    def test_stop_before_start_and_batch(self):
        attempt = self.prepare()
        stop(attempt)
        with self.assertRaises(RuntimeError):
            execute(attempt)
        self.assertEqual(read_json(attempt / "status.json")["state"], "stopped")
        attempts = [self.prepare(Config(seed=i)) for i in (10, 11, 12)]
        run_many(attempts, workers=2)
        self.assertTrue(all(read_json(a / "status.json")["state"] == "finished" for a in attempts))

    def test_slurm_submission_race_and_failure(self):
        attempt = self.prepare()
        def sbatch(args, **kwargs):
            # Simulate a worker starting before sbatch returns its ID.
            write_json(attempt / "status.json", {"state": "running"})
            return subprocess.CompletedProcess(args, 0, "123;cluster\n", "")
        with patch("MTS_Management.runner.subprocess.run", side_effect=sbatch):
            result = launch(attempt, mode="slurm", slurm={"time": "00:01:00"})
        self.assertEqual(result["scheduler_id"], "123")
        self.assertEqual(read_json(attempt / "status.json")["state"], "running")
        script = (attempt / "submit.sh").read_text()
        self.assertIn("set -euo pipefail", script)
        self.assertIn("_execute", script)
        attempt = self.prepare(Config(seed=1))
        with patch("MTS_Management.runner.subprocess.run", side_effect=subprocess.CalledProcessError(1, "sbatch")):
            with self.assertRaises(subprocess.CalledProcessError):
                launch(attempt, mode="slurm")
        self.assertEqual(read_json(attempt / "status.json")["state"], "failed")

    def test_slurm_chain_dependency_and_late_checkpoint_selection(self):
        site = Site(name="fixture", directory=str(self.root), executable=str(self.exe),
                    output_root=str(self.output), state_dir=str(self.state), mode="slurm")
        def sbatch(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, str(123 + submitter.call_count), "")
        with patch("MTS_Management.runner.subprocess.run", side_effect=sbatch) as submitter:
            result = serve({"site": asdict(site), "operation": "run", "options": {
                "settings": Config().as_dict(), "chain": 2}})
            self.assertEqual(submitter.call_count, 2)
            self.assertIn("--dependency=afterany:124", submitter.call_args.args[0])
        first, second = [Path(j["attempt"]) for j in result["jobs"]]
        self.assertIsNone(read_json(second / "request.json")["input"])
        with self.assertRaises(RuntimeError):
            self.prepare()
        with redirect_stdout(io.StringIO()):
            execute(first)
        folder = first.parent.parent
        newer = folder / "dumps/newest.xml.gz"
        newer.write_text("created after continuation was submitted")
        old = folder / "dumps/state.xml.gz"
        os.utime(old, ns=(1, 1))
        # A genuinely running attempt must remain visible ahead of queued continuations.
        status = read_json(first / "status.json")
        write_json(first / "status.json", status | {"state": "running"})
        self.assertEqual(inventory(self.output)[0]["attempt"], str(first))
        self.assertEqual(inventory(self.output)[0]["queued_continuations"], 1)
        original = subprocess.run
        def accounting(args, **kwargs):
            if args[0] == "sacct":
                return subprocess.CompletedProcess(args, 0, "124|TIMEOUT|\n", "")
            return original(args, **kwargs)
        with patch("MTS_Management.runner.subprocess.run", side_effect=accounting), redirect_stdout(io.StringIO()):
            execute(second)
        resumed = read_json(second / "request.json")
        self.assertEqual(resumed["input"], {"checkpoint": str(newer), "sha256": file_digest(newer)})
        self.assertEqual(read_json(second / "status.json")["command"][-2:], ["-d", str(newer)])
        self.assertEqual(read_json(first / "status.json")["scheduler_terminal_state"], "TIMEOUT")
        self.assertIn("checkpoint_selected", [r["event"] for r in Journal(self.state).records()])

    def test_latest_checkpoint_missing_and_chain_cancellation_fail_explicitly(self):
        first = self.prepare()
        write_json(first / "status.json", {"state": "queued", "scheduler_id": "123"})
        second = self.prepare(checkpoint="latest", after=first)
        with patch("MTS_Management.runner.subprocess.run", return_value=
                   subprocess.CompletedProcess([], 0, "123|CANCELLED|\n", "")):
            with self.assertRaisesRegex(RuntimeError, "CANCELLED"):
                execute(second)
        self.assertEqual(read_json(second / "status.json")["state"], "failed")
        # Explicit latest resume also fails instead of silently starting from scratch.
        write_json(first / "status.json", {"state": "finished", "scheduler_id": "123"})
        latest = self.prepare(checkpoint="latest")
        with self.assertRaisesRegex(FileNotFoundError, "No checkpoint"):
            execute(latest)
        self.assertEqual(read_json(latest / "status.json")["state"], "failed")

    def test_json_remote_endpoint_and_journal_merge(self):
        site = Site(name="fixture", directory=str(self.root), executable=str(self.exe),
                    output_root=str(self.output), state_dir=str(self.state))
        payload = {"site": asdict(site), "operation": "resources"}
        result = subprocess.run([sys.executable, "-m", "MTS_Management", "_serve"],
                                input=json.dumps(payload), capture_output=True, text=True, check=True)
        self.assertIn("disk_free_bytes", json.loads(result.stdout))
        remote = Site(name="existing", ssh="magi", management_module="mtscripts")
        with patch("MTS_Management.cluster.subprocess.Popen") as transport:
            process = transport.return_value.__enter__.return_value
            process.communicate.return_value = (result.stdout, result.stderr)
            process.returncode = 0
            call(remote, "resources")
        args, kwargs = transport.call_args
        self.assertIn("mtscripts _serve", args[0][-1])
        self.assertNotIn("management_module", json.loads(process.communicate.call_args.args[0])["site"])
        with self.assertRaisesRegex(ValueError, "new deployment"):
            deploy(remote)
        journal = Journal(self.state)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: journal.append("test", number=i), range(40)))
        destination = self.root / "merged"
        sync_journals([site], destination)
        sync_journals([site], destination)
        self.assertEqual(len(Journal(destination).records()), 40)
        conflicting = journal.records()[0] | {"event": "changed"}
        with self.assertRaises(ValueError):
            journal.merge([conflicting])
        cursor = journal.since()["next_offset"]
        event = journal.append("new")
        self.assertEqual(journal.since(cursor)["records"], [event])

    def test_batch_fetch_uses_one_inventory_and_one_transfer(self):
        configs = [Config(seed=seed) for seed in range(3)]
        for config in configs:
            attempt = self.prepare(config)
            if config["seed"] < 2:
                with redirect_stdout(io.StringIO()):
                    execute(attempt)
                write_json(attempt.parent.parent / "termination.json", {"termination": {
                    "steps": 1, "stopReason": "sample_targets", "targetsMet": True}})
        site = Site(name="local", directory=str(self.root), output_root=str(self.output))
        with patch("MTS_Management.cluster.call", wraps=call) as remote, \
             patch("MTS_Management.cluster.subprocess.run", wraps=subprocess.run) as transfer:
            fetched = fetch_many(site, [c.config_hash for c in configs], self.root/"batch",
                                 completed_only=True)
        self.assertEqual(remote.call_count, 1)
        self.assertEqual(transfer.call_count, 1)
        self.assertEqual(len(fetched), 2)
        for folder in map(Path, fetched):
            self.assertTrue((folder/"macroData.csv").exists())
            self.assertFalse((folder/"dumps").exists())
        self.assertEqual(len(read_json(self.root/"batch/snapshot.json")["runs"]), 2)
        (Path(fetched[0])/"manifest.json").write_text("{}")
        with patch("MTS_Management.cluster.subprocess.run") as transfer:
            with self.assertRaisesRegex(ValueError, "different/unmanaged"):
                fetch_many(site, [c.config_hash for c in configs], self.root/"batch")
            transfer.assert_not_called()
        full = fetch_many(site, [configs[0].config_hash], self.root/"full", kind="full")
        self.assertTrue((Path(full[0])/"dumps/state.xml.gz").is_file())

    def test_management_runs_without_legacy_or_third_party_packages(self):
        shutil.copytree(PACKAGE_ROOT/"MTS_Management", self.root/"MTS_Management",
                        ignore=shutil.ignore_patterns("__pycache__"))
        environment = os.environ | {"PYTHONPATH": str(self.root)}
        subprocess.run([sys.executable, "-S", "-m", "MTS_Management", "config", str(self.root/"new.conf")],
                       cwd=self.root, env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(Config.read(self.root/"new.conf").config_hash, Config().config_hash)

    def test_submission_uncertainty_and_shared_source_guard(self):
        site = Site(name="remote", ssh="pascal", directory="/home/user/simulation/NewSimulationScripts")
        with patch("MTS_Management.cluster.call", side_effect=subprocess.TimeoutExpired("ssh", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                submit(site, Config(), state_dir=self.state, campaign="TimeoutTest")
        records = Journal(self.state).records()
        self.assertEqual([r["event"] for r in records], ["submission_requested", "submission_error"])
        self.assertIn("unknown", records[-1]["outcome"])
        with patch("MTS_Management.cluster.source_identity", return_value={"revision": "test"}):
            with self.assertRaisesRegex(ValueError, "dedicated"):
                deploy(site, include_source=True, local_source=self.root)

    def test_stale_slurm_attempt_can_be_stopped_without_inventing_success(self):
        attempt = self.prepare()
        write_json(attempt/"status.json", {"state": "running", "scheduler_id": "123"})
        def scheduler(args, **kwargs):
            if args[0] == "scancel":
                return subprocess.CompletedProcess(args, 1, "", "Invalid job id")
            return subprocess.CompletedProcess(args, 0, "123|TIMEOUT|\n", "")
        with patch("MTS_Management.runner.subprocess.run", side_effect=scheduler):
            stop(attempt)
        status = read_json(attempt/"status.json")
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["scheduler_terminal_state"], "TIMEOUT")


    def test_benchmark_controller_resumes_confirmed_jobs_and_refuses_uncertainty(self):
        from campaigns.magi_benchmark_oct26.launch import main as submit_campaign
        from MTS_Management.files import file_digest
        build = read_json(self.exe.parent/"build-manifest.json")
        write_json(self.exe.parent/"build-manifest.json", build | {"build_options": {"ENABLE_NATIVE_ARCH": "OFF"}})
        site = Site(name="local", directory=str(self.root), executable=str(self.exe),
                    output_root=str(self.output), state_dir=str(self.state), mode="slurm")
        sites = self.root/"sites.toml"
        settings = asdict(site); settings.pop("name")
        sites.write_text("[sites.local]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k,v in settings.items()))
        campaign = self.root/"campaign.json"
        entries = [{"label": "Bench", "settings": Config().as_dict()},
                   {"label": "Diagnostic", "settings": Config(logDuringMinimization=True).as_dict()}]
        write_json(campaign, {"name": "ControllerTest", "runs": entries})
        write_json(campaign.with_suffix(".resources.json"), {"campaign_sha256": file_digest(campaign),
            "diagnostic_label": "Diagnostic", "benchmark_jobs": 1, "site": "fixture",
            "simulator_revision": "test", "resources": {"3": {"time": "01:00:00", "mem": "4G"}}})
        args = [str(campaign), "--sites", str(sites)]
        def sbatch(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, str(700 + scheduler.call_count), "")
        with patch("MTS_Management.runner.subprocess.run", side_effect=sbatch) as scheduler, redirect_stdout(io.StringIO()):
            submit_campaign(args)
            self.assertEqual(scheduler.call_count, 2)
            submit_campaign(args + ["--resume"])
            self.assertEqual(scheduler.call_count, 2)
            with self.assertRaises(FileExistsError):
                submit_campaign(args)
        folder = self.state/"ControllerTest"
        self.assertEqual(read_json(folder/"submission.json")["accepted"], 2)
        receipts = [json.loads(line) for line in (folder/"submissions.jsonl").read_text().splitlines()]
        self.assertEqual(len(receipts), 2)
        attempt = Path(receipts[-1]["attempt"])
        write_json(attempt/"status.json", {"state": "queued"})
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, "Uncertain submission"):
            submit_campaign(args + ["--resume"])


class DataTests(unittest.TestCase):
    def test_macro_readers_accept_extra_named_columns_without_changing_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"macroData.csv"
            path.write_text("future_metric,tangent_preparation_time_s,load\n17,0.25,0.1\n18,0.25,0.2\n")
            rows = read_macros(path)
            self.assertEqual(rows[-1]["future_metric"], "18")
            self.assertEqual(rows[-1]["tangent_preparation_time_s"], "0.25")
            self.assertAlmostEqual(progress(folder, Config(totalLoad=1))["fraction"], .2)
            path.write_text(path.read_text() + "#HEADER:load,different_metric\n")
            with self.assertRaises(ValueError):
                read_macros(path)

    def test_bounded_size_nested_files_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/"data"/"minimization"/"step1").mkdir(parents=True)
            for i in range(20):
                (root/"data"/"minimization"/"step1"/f"{i}.vtu").write_bytes(b"x"*100)
            (root/"loop").symlink_to(root, target_is_directory=True)
            estimate = estimate_size(root, seconds=5)
            self.assertTrue(estimate["coverage_complete"])
            self.assertEqual(estimate["estimated_bytes"], 2000)
            partial = estimate_size(root, seconds=5, max_entries=5)
            self.assertFalse(partial["coverage_complete"])
            self.assertEqual(partial["entries_visited"], 5)

    def test_progress_ignores_incomplete_last_row_and_counts_path_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"macroData.csv"
            path.write_text("load,x\n0.7,1\n1.2,2\n1.3,")
            config = Config(startLoad=.7, totalLoad=1, directions="horizontal,-horizontal", segmentLoad=.1)
            self.assertAlmostEqual(progress(folder, config)["fraction"], .5)
            path.write_text("load,x\n0.7,1\n1.2\n")
            with self.assertRaises(ValueError):
                progress(folder, config)


if __name__ == "__main__":
    unittest.main()
