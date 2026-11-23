"""Run identity and direct execution regressions using the compiled simulator."""
import fcntl
import gzip
import os
import xml.etree.ElementTree as ET
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
from unittest.mock import patch
from MTS_Management.cluster import Site, serve
from MTS_Management.data import mesh_snapshots, read_macros
from MTS_Management.inventory import inventory
from MTS_Management.config import Config, DEFAULTS
from MTS_Management.files import digest, file_digest, read_json, write_json
from MTS_Management.runner import prepare, launch, execute, attempt_status

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get('MTS_TEST_BINARY', ROOT / 'MTS2D/build/debug/MTS2D')).resolve()


class CampaignIdentityTests(unittest.TestCase):
    def test_campaign_hashes_match_fixed_pre_backend_identities(self):
        # Fingerprints of sorted original hashes, independent of the current hash implementation.
        expected = {
            'magi_benchmark_oct26': '872d3ae075becb76fe4604cba84b3e097723ca6c9e7cf5a11f7155294d5e1d6a',
            'magi_pilot_oct26': '5b1b80546018295fae27056ae0b64860a9d3d2ec5deb66dc3b5e2e5677520222',
            'magi_chain_smoke_oct26': 'd4d4d483f742b3c817afa82f5ae6854f99d1119b69fe9ebc7bd2bb1143a439ec',
        }
        for directory, fingerprint in expected.items():
            with self.subTest(campaign=directory):
                archive = read_json(ROOT/'MTSManager/campaigns'/directory/'simulations.json')
                hashes = [digest({"format": "mtscripts-config-v1", "settings": row["settings"]})
                          for row in archive["runs"]]
                actual = hashlib.sha256('\n'.join(sorted(hashes)).encode()).hexdigest()
                self.assertEqual(actual, fingerprint)

    def test_editing_supplement_changes_interpretation_not_identity(self):
        values = Config().as_dict()
        values.pop('tangentBackend')
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = root/'campaigns/example'
            write_json(directory/'simulations.json', {'name':'Example', 'runs':[{'label':'Run','settings':values}]})
            metadata = directory/'metadata.json'
            write_json(metadata, {'missing_settings':{'tangentBackend':'eigen'}})
            config_path = root/'config.conf'
            with patch('MTS_Management.campaign.__file__', str(root/'MTS_Management/campaign.py')):
                first = Config.saved(values)
                first.write(config_path)
                original = config_path.read_bytes()
                write_json(metadata, {'missing_settings':{'tangentBackend':'cholmod'}})
                second = Config.read(config_path)
                self.assertEqual((first['tangentBackend'], second['tangentBackend']), ('eigen','cholmod'))
                self.assertEqual(second.config_hash, first.config_hash)
                self.assertEqual(config_path.read_bytes(), original)
                explicit = Config.saved(values | {'tangentBackend':'eigen'})
                self.assertEqual(explicit['tangentBackend'], 'eigen')
                write_json(metadata, {'missing_settings':{}})
                with self.assertRaisesRegex(ValueError, 'Incomplete'):
                    Config.read(config_path)

@contextmanager
def prepared_run(**settings):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        executable = root/'build/MTS2D'
        executable.parent.mkdir()
        shutil.copy2(BINARY, executable)
        write_json(executable.parent/'build-manifest.json', {
            'branch':'main', 'revision':'fixture', 'executable_sha256':file_digest(executable),
            'execution_interface':'folder-v1', 'configuration_keys_sha256':digest(sorted(DEFAULTS))})
        values = dict(rows=3, cols=3, loading='noRelaxation', initialGuessNoise=0,
            startLoad=-.01, totalLoad=.004, loadIncrement=.001, checkpointInterval=1,
            writeDumps=True, writeMeshVTUs=False, vtuFilesPerUnitLoad=0, showProgress=-1, tangentBackend='eigen')
        config = Config(**(values | settings))
        options = dict(executable=executable, output_root=root/'output', state_dir=root/'.state')
        yield root, config, options


def physical_rows(folder):
    columns = ('load_step', 'load', 'completed_steps', 'avg_energy')
    return [tuple(row[key] for key in columns) for row in read_macros(folder/'macroData.csv')]


class CheckpointSelectionTests(unittest.TestCase):
    def test_defunct_launcher_does_not_block_continuation(self):
        from MTS_Management.archive import check_inactive
        from MTS_Management.errors import ExpectedError

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            attempt = folder / "attempts" / "attempt"
            attempt.mkdir(parents=True)
            started = "Sun Nov 15 14:51:49 2026"
            write_json(attempt / "status.json", {"state": "prepared", "launcher_pid": 123, "launcher_token": started})
            write_json(attempt / "request.json", {"command": ["MTS2D"]})
            (attempt / "exitcode").write_text("0\n")

            def process_listing(state):
                def run(command, **kwargs):
                    fields = {"stat=": state, "lstart=": started}
                    return subprocess.CompletedProcess(command, 0, " ".join(fields[key] for key in command[4::2]) + "\n")
                return run

            with patch("MTS_Management.runner.subprocess.run", side_effect=process_listing("Z")):
                check_inactive(folder)
            with patch("MTS_Management.runner.subprocess.run", side_effect=process_listing("S")):
                with self.assertRaises(ExpectedError):
                    check_inactive(folder)

    def test_rounded_final_dump_and_rotating_slots_skip_provably_older_files(self):
        from MTS_Management.continuation import checkpoint_files, latest_checkpoint

        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            dumps = run / "dumps"
            dumps.mkdir()
            for name, step, load in (
                ("dump_l0.8", 12, .81), ("dump_l0.88", 19, .879), ("dump_l1.04", 31, 1.04),
                ("dump_l1.0", 32, 1.049), ("checkpoint_0", 28, .985), ("checkpoint_1", 30, 1.03),
            ):
                with gzip.open(dumps / (name + ".xml.gz"), "wt") as output:
                    output.write(f"<cereal><Simulation><mesh><load>{load}</load><loadSteps>{step}</loadSteps>"
                                 "</mesh></Simulation></cereal>")

            checkpoint, earlier, checked = latest_checkpoint(checkpoint_files(run), last_load=1.049)
            self.assertEqual((checkpoint["name"], checkpoint["step"]), ("dump_l1.0.xml.gz", 32))
            self.assertEqual(checked, 3)
            self.assertEqual({path.name for path in earlier}, {path.name for path in dumps.iterdir()} - {checkpoint["name"]})

            # A larger printed load can describe an earlier step when the
            # files were written with different decimal precisions.
            with gzip.open(dumps / "dump_l1.05.xml.gz", "wt") as output:
                output.write("<cereal><Simulation><mesh><load>1.046</load><loadSteps>31</loadSteps>"
                             "</mesh></Simulation></cereal>")
            (dumps / "dump_l1.04.xml.gz").unlink()
            checkpoint, _, checked = latest_checkpoint(checkpoint_files(run), last_load=1.049)
            self.assertEqual((checkpoint["name"], checkpoint["step"]), ("dump_l1.0.xml.gz", 32))
            self.assertEqual(checked, 4)


@unittest.skipUnless(BINARY.exists(), 'Build MTS2D before simulator lifecycle tests')
class FolderExecutionTests(unittest.TestCase):
    def test_campaign_skips_completed_run_and_manual_removal_allows_fresh_start(self):
        from MTS_Management.errors import ExpectedError
        from MTS_Management.runner import find_fresh_conflict

        with prepared_run() as (root, config, options):
            attempt = prepare(config, **options, label="First")
            launch(attempt)
            run = attempt.parents[1]
            self.assertIsNone(prepare(config, **(options | {"executable": root / "other-build"}),
                                      label="Second", skip_completed=True))
            self.assertEqual(list((run / "attempts").iterdir()), [attempt])
            self.assertIn(str(run), find_fresh_conflict(options["output_root"], config, "Second"))
            site = Site(name="local", directory=str(root), executable=str(root / "other-build"))
            result = serve({"site": asdict(site), "operation": "run",
                            "options": {"settings": config.as_dict(), "skip_completed": True}})
            self.assertTrue(result["skipped"])
            with self.assertRaisesRegex(ExpectedError, "use a separate output root"):
                prepare(config, **options, label="Second")
            run.rename(root / "removed")
            self.assertEqual(find_fresh_conflict(options["output_root"], config, "Second"), "")
            new = prepare(config, **options, label="Second")
            self.assertTrue(new.parents[1].name.startswith("Second["))

    def test_custom_periodic_mesh_keeps_cut_and_restored_diagonal(self):
        from MTS_Management.continuation import plan
        from MTS_Management.initial_mesh import preview

        # Cut the four tiles adjoining (2, 2) and keep one alternate diagonal.
        points = [(x, y, 0) for y in range(5) for x in range(5)]
        cells = []
        for y in range(4):
            for x in range(4):
                if x in (1, 2) and y in (1, 2):
                    continue
                a = 5 * y + x
                cells.extend(((a, a+1, a+6), (a, a+6, a+5)) if a == 0 else
                             ((a, a+1, a+5), (a+1, a+6, a+5)))
        coordinates = " ".join(str(value) for point in points for value in point)
        connectivity = " ".join(str(value) for cell in cells for value in cell)
        offsets = " ".join(str(3 * (i + 1)) for i in range(len(cells)))
        mesh = f'''<VTKFile type="UnstructuredGrid"><UnstructuredGrid>
          <Piece NumberOfPoints="25" NumberOfCells="24">
          <Points><DataArray format="ascii" NumberOfComponents="3">{coordinates}</DataArray></Points>
          <Cells>
            <DataArray format="ascii" Name="connectivity">{connectivity}</DataArray>
            <DataArray format="ascii" Name="offsets">{offsets}</DataArray>
            <DataArray format="ascii" Name="types">{'5 ' * len(cells)}</DataArray>
          </Cells></Piece></UnstructuredGrid></VTKFile>'''
        checksum = hashlib.sha256(mesh.encode()).hexdigest()
        with prepared_run(rows=4, cols=4, boundary="periodic", startLoad=0,
                          initialMeshSha256=checksum, writeMeshVTUs=True,
                          vtuFilesPerUnitLoad=1000) as (_, config, options):
            regular = Config(**(config.as_dict() | {"initialMeshSha256": "",
                                                    "segmentLoad": .05, "directions": "horizontal,vertical"}))
            native = ET.fromstring(preview(options["executable"], regular)).find(".//Piece")
            lattice = native.find("PointData/DataArray[@Name='lattice_index']").text.split()
            loaded = native.find("PointData/DataArray[@Name='loaded_position']").text.split()
            seam = next(i for i in range(0, len(lattice), 3) if lattice[i:i+3] == ["0", "4", "0"])
            self.assertEqual(list(map(float, loaded[seam:seam+3])), [0.2, 4.01, 0])

            attempt = prepare(config, **options, initial_mesh=mesh)
            folder = attempt.parents[1]
            asset = folder / "initialMesh.vtu"
            asset.write_text(mesh + "\n")
            failed = subprocess.run([options["executable"], folder, "--resume"], capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("checksum", failed.stderr)
            self.assertFalse((folder / "macroData.csv").exists())
            asset.write_text(mesh)
            launch(attempt)
            self.assertEqual(file_digest(asset), checksum)
            first = mesh_snapshots(folder)[0]
            piece = ET.parse(folder / "data" / first["name"]).find(".//Piece")
            values = list(map(float, piece.find("Points/DataArray").text.split()))
            xyz = [(round(x - first["load"] * y, 10), y, z)
                   for x, y, z in zip(values[::3], values[1::3], values[2::3])]
            native_cells = list(map(int, piece.find("Cells/DataArray[@Name='connectivity']").text.split()))
            actual = {frozenset(xyz[i] for i in native_cells[j:j+3]) for j in range(0, len(native_cells), 3)}
            self.assertEqual(actual, {frozenset(points[i] for i in cell) for cell in cells})

            selection = {"folder": str(folder), "config_hash": config.config_hash,
                         "manifest_sha256": file_digest(folder / "manifest.json")}
            reviewed = plan(options["output_root"], selection)
            extended = Config(**(reviewed["settings"] | {"totalLoad": .006}))
            continuation = prepare(extended, **options, continuation=selection |
                                   {"checkpoint_catalog": reviewed["checkpoint_catalog"]}, log=lambda _: None)
            launch(continuation)
            continued = continuation.parents[1]
            self.assertEqual(file_digest(continued / asset.name), checksum)
            self.assertAlmostEqual(float(physical_rows(continued)[-1][1]), .006)

    def test_load_further_copies_only_history_through_checkpoint(self):
        from MTS_Management.continuation import plan

        with prepared_run(startLoad=0., writeDumps=False, writeMeshVTUs=True,
                          vtuFilesPerUnitLoad=1000) as (_, config, options):
            original_attempt = prepare(config, **options)
            launch(original_attempt)
            source = original_attempt.parents[1]
            original_csv = (source / "macroData.csv").read_bytes()
            source_manifest = source / "manifest.json"
            selection = {"folder": str(source), "config_hash": config.config_hash,
                         "manifest_sha256": file_digest(source_manifest)}
            with patch("MTS_Management.continuation.checkpoint_location",
                       side_effect=AssertionError("Review opened a compressed checkpoint")):
                reviewed = plan(options["output_root"], selection)
            self.assertAlmostEqual(reviewed["settings"]["totalLoad"], 1.004)
            self.assertEqual(set(options["output_root"].resolve().glob("*/manifest.json")), {source_manifest})
            checksum = file_digest(source / "dumps" / "checkpoint_0.xml.gz")

            edited = Config(**(reviewed["settings"] | {"totalLoad": .006, "loadIncrement": .0005,
                                                      "checkpointInterval": 4}))
            messages = []
            receipt = selection | {"checkpoint_catalog": reviewed["checkpoint_catalog"]}
            attempt = prepare(edited, **options, continuation=receipt, log=messages.append)
            progress = [int(line.rsplit(" ", 1)[-1][:-1]) for line in messages if line.startswith("Copying original simulation")]
            self.assertEqual(progress, sorted(set(progress)))
            self.assertEqual((progress[0], progress[-1]), (0, 100))
            self.assertGreater(len(progress), 2)
            destination = attempt.parents[1]
            self.assertNotEqual(destination, source)
            record = read_json(destination / "manifest.json")["continuation"]
            self.assertEqual(record["checkpoint"]["step"], 4)
            self.assertAlmostEqual(record["checkpoint"]["load"], .003)
            # Before the first new step the solver writes the same slot.
            self.assertEqual(file_digest(destination / "dumps" / "checkpoint_1.xml.gz"), checksum)
            self.assertEqual(physical_rows(destination), physical_rows(source)[:4])
            copied = mesh_snapshots(destination)
            self.assertEqual([frame["step"] for frame in copied], [1, 2, 3, 4])
            for frame in copied:
                self.assertEqual(file_digest(destination / "data" / frame["name"]),
                                 file_digest(source / "data" / frame["name"]))
            self.assertFalse((destination / "termination.json").exists())

            launch(attempt)
            final = physical_rows(destination)
            self.assertEqual(final[:4], physical_rows(source)[:4])
            self.assertAlmostEqual(float(final[4][1]), .0035)
            self.assertAlmostEqual(float(final[-1][1]), .006)
            self.assertEqual((source / "macroData.csv").read_bytes(), original_csv)
            saved = destination / record["saved_checkpoint"]
            self.assertEqual(file_digest(saved), checksum)

    def test_direct_resume_matches_uninterrupted_output_and_original_binary(self):
        with prepared_run() as (root, config, options):
            attempt = prepare(config, **options)
            # A Python wrapper would fail in this environment; a saved binary command works.
            with patch.dict(os.environ, {'PYTHONHOME':'/unavailable-manager', 'PYTHONPATH':'/unavailable-manager'}):
                launch(attempt)
            request = read_json(attempt/'request.json')
            folder = Path(request['output_folder'])
            original = (folder/'config.conf').read_bytes()
            expected = physical_rows(folder)
            (folder/'termination.json').unlink()
            for dump in (folder/'dumps').glob('dump_l*'):
                dump.unlink()
            checkpoints = list((folder/'dumps').glob('checkpoint_*.xml.gz'))
            steps = {p: int(ET.fromstring(gzip.decompress(p.read_bytes())).find('.//loadSteps').text) for p in checkpoints}
            greatest = max(steps.values())
            for p, step in steps.items():
                os.utime(p, (100 if step == greatest else 2000000000,)*2)
            # Its name puts this old file strictly before either rotating checkpoint.
            (folder/'dumps/dump_l-0.100.xml.gz').write_bytes(b'corrupt old dump')
            (attempt/'exitcode').write_text('1\n')
            with (attempt/'stdout.log').open('a') as log:
                log.write('Cannot write VTU file: No space left on device\n')
            self.assertEqual(inventory(options['output_root'])[0]['status']['failure_reason'], 'storage space')
            selection = {'folder': str(folder), 'config_hash': config.config_hash,
                         'manifest_sha256': file_digest(folder/'manifest.json')}
            resumed = prepare(config, **(options | {'executable':root/'missing-build'}),
                              checkpoint='latest', resume_selection=selection)
            self.assertEqual(read_json(resumed/'request.json')['executable'], request['executable'])
            launch(resumed)
            self.assertEqual(physical_rows(folder), expected)
            self.assertEqual(attempt_status(resumed)['state'], 'finished')
            self.assertIn(f'step={greatest}', (resumed/'stdout.log').read_text())
            self.assertEqual((folder/'config.conf').read_bytes(), original)
            with (folder/'.run.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                run = subprocess.run(request['command'], capture_output=True, text=True, timeout=15)
                self.assertNotEqual(run.returncode, 0)
                self.assertIn('lock', run.stderr)
            (folder/'termination.json').unlink()
            max(checkpoints, key=steps.get).write_bytes(b'corrupt checkpoint')
            run = subprocess.run(request['command'], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(run.returncode, 0)

    def test_target_completion_and_explicit_replay_preserve_rows_and_counts(self):
        with prepared_run(startLoad=0, stopAfterSteps=2) as (root, config, options):
            first = prepare(config, **options)
            launch(first)
            folder = first.parents[1]
            expected = physical_rows(folder)
            original_csv = (folder/'macroData.csv').read_bytes()
            latest = prepare(config, checkpoint='latest', **options)
            launch(latest)
            self.assertEqual((folder/'macroData.csv').read_bytes(), original_csv)
            # The rotating checkpoint precedes the second accepted step.
            replay = prepare(config, checkpoint=folder/'dumps/checkpoint_0.xml.gz', **options)
            launch(replay)
            self.assertEqual(physical_rows(folder), expected)
            item = inventory(options['output_root'])[0]
            self.assertEqual((item['termination']['steps'], item['termination']['stopReason']), (2,'sample_targets'))
            self.assertEqual(item['progress']['fraction'], .5)

    def test_failed_checkpoint_publication_preserves_previous_snapshot(self):
        with prepared_run() as (root, config, options):
            attempt = prepare(config, **options)
            launch(attempt)
            request = read_json(attempt/'request.json')
            folder = Path(request['output_folder'])
            expected = physical_rows(folder)
            (folder/'termination.json').unlink()
            for dump in (folder/'dumps').glob('dump_l*'):
                dump.unlink()
            checkpoints = list((folder/'dumps').glob('checkpoint_*.xml.gz'))
            selected = max(checkpoints, key=lambda p:
                int(ET.fromstring(gzip.decompress(p.read_bytes())).find('.//loadSteps').text))
            original = selected.read_bytes()
            # Deterministic write failure, independent of user/root permission settings.
            blocked = selected.with_name(selected.name + '.tmp.gz')
            blocked.mkdir()
            run = subprocess.run(request['command'], capture_output=True, text=True, timeout=15)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(selected.read_bytes(), original)
            blocked.rmdir()
            subprocess.run(request['command'], check=True, capture_output=True, timeout=15)
            self.assertEqual(physical_rows(folder), expected)

    def test_slurm_chain_executes_saved_scripts_and_discovers_late_checkpoint(self):
        with prepared_run() as (root, config, options):
            site = Site(name='fixture', directory=str(root), executable=str(options['executable']),
                        output_root=str(options['output_root']), state_dir=str(options['state_dir']), mode='slurm')
            submitted = []
            original_run = subprocess.run
            def scheduler(command, **kwargs):
                if command[0] == 'sbatch':
                    submitted.append(command)
                    return subprocess.CompletedProcess(command, 0, str(900+len(submitted)), '')
                if command[0] == 'sacct':
                    return subprocess.CompletedProcess(command, 0, '901|PENDING|0:0\n', '')
                return original_run(command, **kwargs)
            with patch('MTS_Management.runner.subprocess.run', side_effect=scheduler):
                result = serve({'site':asdict(site), 'operation':'run',
                                'options':{'settings':config.as_dict(), 'chain':2}})
            self.assertIn('--dependency=afterany:901', submitted[1])
            first, second = [Path(job['attempt']) for job in result['jobs']]
            folder = first.parents[1]
            self.assertFalse((folder/'dumps').exists())
            environment = os.environ | {'PYTHONHOME':'/unavailable-manager', 'PYTHONPATH':'/unavailable-manager'}
            original_run(['bash',str(first/'submit.sh')], env=environment, check=True, capture_output=True, timeout=15)
            expected = physical_rows(folder)
            (folder/'termination.json').unlink()
            for dump in (folder/'dumps').glob('dump_l*'):
                dump.unlink()
            original_run(['bash',str(second/'submit.sh')], env=environment, check=True, capture_output=True, timeout=15)
            self.assertEqual(physical_rows(folder), expected)
