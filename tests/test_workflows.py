"""Behavior tests use isolated fake executables; integration smoke uses real MTS2D."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from mtscripts.cluster import Site, call, deploy, fetch, load_sites, paths, serve, submit, sync_journals
from mtscripts.config import Config, DEFAULTS, sweep
from mtscripts.files import digest, file_digest, read_json, write_json
from mtscripts.inventory import estimate_size, inventory, progress, rebuild_index
from mtscripts.journal import Journal
from mtscripts.provenance import build
from mtscripts.runner import PACKAGE_ROOT, execute, launch, prepare, run_many, stop


class ConfigTests(unittest.TestCase):
    def test_unified_termination_settings(self):
        config = Config(stopAfterPlasticSteps=20, stopAfterSteps=100,
                        stopWhen="any", loadIncrement=1e-5, epsR=1e-6)
        self.assertEqual(config["stopAfterPlasticSteps"], 20)
        self.assertNotEqual(config.config_hash,
                            Config(**(config.as_dict() | {"stopWhen": "all"})).config_hash)
        for settings in ({"stopAfterSteps": -1}, {"stopAfterSteps": True},
                         {"stopWhen": "sometimes"}, {"stepEnergyTolerance": -1},
                         {"stepEnergyTolerance": float("nan")},
                         {"stopAfterPlasticSteps": 1, "loading": "noRelaxation"},
                         {"stopAfterElasticSteps": 1, "energyFunction": "contiTriangular"},
                         {"stopOnEnergyDrop": True}, {"FIREEpsx": 1e-6}):
            with self.assertRaises((TypeError, ValueError)):
                Config(**settings)

    def test_hash_complete_stable_and_changes_with_settings(self):
        a = Config(rows=10, totalLoad=1)
        b = Config(**dict(reversed(list(a.as_dict().items()))))
        self.assertEqual(a.config_hash, b.config_hash)
        self.assertNotEqual(a.config_hash, Config(rows=10, totalLoad=2).config_hash)
        self.assertEqual(a.config_hash, Config(rows=10, totalLoad=1.0).config_hash)
        self.assertEqual(a.folder_name("First").split("[")[1], a.folder_name("Second").split("[")[1])

    def test_strict_complete_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.conf"
            config = Config(directions="horizontal,-vertical", segmentLoad=.2, repeat=True)
            config.write(path, "Campaign")
            self.assertEqual(Config.read(path).as_dict(), config.as_dict())
            path.write_text(path.read_text().replace("seed = 0\n", ""))
            with self.assertRaises(ValueError):
                Config.read(path)
        for settings in ({"experiment": "simpleShear"}, {"rows": True}, {"epsR": float("nan")},
                         {"totalLoad": -1}, {"loadIncrement": 0}, {"directions": "x"},
                         {"loading": "noRelaxation", "stepNoise": 1}):
            with self.assertRaises((ValueError, TypeError)):
                Config(**settings)
        Config(boundary="periodicAnchored", deformation="directDisplacement")

    def test_sweep(self):
        configs = sweep(Config(), rows=[100, 200], seed=[0, 1])
        self.assertEqual(len({c.config_hash for c in configs}), 4)
        self.assertEqual(sweep(Config(), L=[100])[0]["cols"], 100)
        with self.assertRaises(ValueError):
            sweep(Config(), L=[100], rows=[100])


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

    def test_site_build_setting_and_cache_override(self):
        config = self.root / "sites.toml"
        config.write_text('[sites.magi]\ndirectory = "."\nnative_arch = false\n')
        site = load_sites(config)["magi"]
        with patch("mtscripts.cluster.build") as mocked:
            call(site, "build", jobs=3)
            self.assertEqual(mocked.call_args.kwargs, {"native_arch": False, "jobs": 3})
        self.assertFalse(Site(name="default").native_arch)
        with self.assertRaises(TypeError):
            Site(name="invalid", native_arch="false")

        cache = self.exe.parent / "CMakeCache.txt"
        cache.write_text("ENABLE_NATIVE_ARCH:BOOL=ON\n")

        def cmake(args, **kwargs):
            if "-S" in args:
                option = next(a for a in args if a.startswith("-DENABLE_NATIVE_ARCH="))
                cache.write_text("ENABLE_NATIVE_ARCH:BOOL=" + option.split("=", 1)[1] + "\n")

        with patch("mtscripts.provenance.source_identity", return_value={"branch": "main"}), \
             patch("mtscripts.provenance.verify_schema"), \
             patch("mtscripts.provenance.subprocess.run", side_effect=cmake):
            # The portable default must replace an existing native build's cache setting.
            record = build(self.root, self.exe.parent)
            self.assertEqual(record["build_options"]["ENABLE_NATIVE_ARCH"], "OFF")
            record = build(self.root, self.exe.parent, native_arch=True)
            self.assertEqual(record["build_options"]["ENABLE_NATIVE_ARCH"], "ON")
            with self.assertRaises(TypeError):
                build(self.root, self.exe.parent, native_arch="false")

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
        with patch("mtscripts.cluster.prepare", return_value=self.root/"attempt") as prepared, \
             patch("mtscripts.cluster.launch") as launched:
            serve({"site": asdict(site), "operation": "run", "options": {"settings": settings}})
            self.assertEqual(prepared.call_args.kwargs["environment"], site.environment)
            self.assertEqual(launched.call_args.kwargs["slurm"]["cpus-per-task"], 4)
        attempt = self.prepare(Config(nrThreads=2), environment=site.environment)
        with patch("mtscripts.runner.cpu_allocation", return_value={"physical_cores": 1}), \
             patch("mtscripts.runner.subprocess.Popen") as child:
            with self.assertRaisesRegex(RuntimeError, "physical cores"):
                execute(attempt)
            child.assert_not_called()
        with self.assertRaises(ValueError):
            Site(name="bad", slurm_cpus_per_thread=0)

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
        self.exe.write_text(self.exe.read_text()+"\n# changed\n")
        with self.assertRaises(ValueError):
            execute(attempt)
        self.assertEqual(read_json(attempt / "status.json")["state"], "failed")

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
        with patch("mtscripts.runner.subprocess.run", side_effect=sbatch):
            result = launch(attempt, mode="slurm", slurm={"time": "00:01:00"})
        self.assertEqual(result["scheduler_id"], "123")
        self.assertEqual(read_json(attempt / "status.json")["state"], "running")
        script = (attempt / "submit.sh").read_text()
        self.assertIn("set -euo pipefail", script)
        self.assertIn("_execute", script)
        attempt = self.prepare(Config(seed=1))
        with patch("mtscripts.runner.subprocess.run", side_effect=subprocess.CalledProcessError(1, "sbatch")):
            with self.assertRaises(subprocess.CalledProcessError):
                launch(attempt, mode="slurm")
        self.assertEqual(read_json(attempt / "status.json")["state"], "failed")

    def test_json_remote_endpoint_and_journal_merge(self):
        site = Site(name="fixture", directory=str(self.root), executable=str(self.exe),
                    output_root=str(self.output), state_dir=str(self.state))
        payload = {"site": asdict(site), "operation": "resources"}
        result = subprocess.run([sys.executable, "-m", "mtscripts", "_serve"],
                                input=json.dumps(payload), capture_output=True, text=True, check=True)
        self.assertIn("disk_free_bytes", json.loads(result.stdout))
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

    def test_selective_fetch_keeps_identity_and_refuses_different_data(self):
        attempt = self.prepare()
        with redirect_stdout(io.StringIO()):
            execute(attempt)
        write_json(attempt.parent.parent/"termination.json", {"termination": {
            "steps": 1, "stopReason": "sample_targets", "targetsMet": True}})
        self.assertEqual(inventory(self.output)[0]["termination"]["stopReason"], "sample_targets")
        site = Site(name="local", directory=str(self.root), output_root=str(self.output))
        destination = Path(fetch(site, Config().config_hash, self.root/"downloads"))
        self.assertTrue((destination/"macroData.csv").is_file())
        self.assertTrue((destination/"config.conf").is_file())
        self.assertTrue((destination/"manifest.json").is_file())
        self.assertEqual(read_json(destination/"termination.json")["termination"]["steps"], 1)
        self.assertFalse((destination/"dumps").exists())
        fetch(site, Config().config_hash, self.root/"downloads")
        (destination/"manifest.json").write_text("{}")
        with self.assertRaises(ValueError):
            fetch(site, Config().config_hash, self.root/"downloads")

    def test_management_runs_without_legacy_or_third_party_packages(self):
        shutil.copytree(PACKAGE_ROOT/"mtscripts", self.root/"mtscripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        environment = os.environ | {"PYTHONPATH": str(self.root)}
        subprocess.run([sys.executable, "-S", "-m", "mtscripts", "config", str(self.root/"new.conf")],
                       cwd=self.root, env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(Config.read(self.root/"new.conf").config_hash, Config().config_hash)

    def test_submission_uncertainty_and_shared_source_guard(self):
        site = Site(name="remote", ssh="pascal", directory="/home/user/simulation/NewSimulationScripts")
        with patch("mtscripts.cluster.call", side_effect=subprocess.TimeoutExpired("ssh", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                submit(site, Config(), state_dir=self.state, campaign="TimeoutTest")
        records = Journal(self.state).records()
        self.assertEqual([r["event"] for r in records], ["submission_requested", "submission_error"])
        self.assertIn("unknown", records[-1]["outcome"])
        with patch("mtscripts.cluster.source_identity", return_value={"revision": "test"}):
            with self.assertRaisesRegex(ValueError, "dedicated"):
                deploy(site, include_source=True, local_source=self.root)

    def test_stale_slurm_attempt_can_be_stopped_without_inventing_success(self):
        attempt = self.prepare()
        write_json(attempt/"status.json", {"state": "running", "scheduler_id": "123"})
        def scheduler(args, **kwargs):
            if args[0] == "scancel":
                return subprocess.CompletedProcess(args, 1, "", "Invalid job id")
            return subprocess.CompletedProcess(args, 0, "123|TIMEOUT|\n", "")
        with patch("mtscripts.runner.subprocess.run", side_effect=scheduler):
            stop(attempt)
        status = read_json(attempt/"status.json")
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["scheduler_terminal_state"], "TIMEOUT")


class DataTests(unittest.TestCase):
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
