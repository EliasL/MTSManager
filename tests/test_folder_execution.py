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
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
import hashlib
from unittest.mock import patch
import io

from MTS_Management.campaign import campaign_entries
from MTS_Management.cluster import Site, serve
from MTS_Management.data import read_macros
from MTS_Management.inventory import inventory
from MTS_Management.config import Config
from MTS_Management.files import file_digest, read_json, write_json
from MTS_Management.runner import prepare, launch, execute, attempt_status

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get('MTS_TEST_BINARY', ROOT / 'MTS2D/build/MTS2D')).resolve()


class CampaignCompatibilityTests(unittest.TestCase):
    def test_campaign_hashes_match_fixed_pre_backend_identities(self):
        # Fingerprints of sorted original hashes, independent of the current hash implementation.
        expected = {
            'magi_benchmark_oct26': '872d3ae075becb76fe4604cba84b3e097723ca6c9e7cf5a11f7155294d5e1d6a',
            'magi_pilot_oct26': '5b1b80546018295fae27056ae0b64860a9d3d2ec5deb66dc3b5e2e5677520222',
            'magi_chain_smoke_oct26': 'd4d4d483f742b3c817afa82f5ae6854f99d1119b69fe9ebc7bd2bb1143a439ec',
        }
        for directory, fingerprint in expected.items():
            with self.subTest(campaign=directory), tempfile.TemporaryDirectory() as td:
                _, entries = campaign_entries(ROOT/'MTSManager/campaigns'/directory/'simulations.json')
                actual = hashlib.sha256('\n'.join(sorted(c.config_hash for c, _ in entries)).encode()).hexdigest()
                self.assertEqual(actual, fingerprint)
                config, label = entries[0]
                path = Path(td)/'config.conf'
                config.write(path, label)
                saved = Config.read(path)
                self.assertEqual(saved.config_hash, config.config_hash)
                self.assertNotIn('tangentBackend', saved.as_dict())
                self.assertEqual(saved['tangentBackend'], 'eigen')

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

    @unittest.skipUnless((ROOT/'MTSManager/build/build-manifest.json').exists(), 'Historical Eigen build not present')
    def test_historical_eigen_binary_runs_and_resumes_without_rehashing(self):
        executable = ROOT/'MTSManager/build/MTS2D'
        build = read_json(executable.parent/'build-manifest.json')
        if build.get('execution_interface'):
            self.skipTest('Installed binary uses the folder interface')
        values = Config(rows=4, cols=4, loading='noRelaxation', totalLoad=.004,
                        loadIncrement=.001, writeDumps=True, writeMeshVTUs=False).as_dict()
        values.pop('tangentBackend')
        with tempfile.TemporaryDirectory() as td, patch('MTS_Management.campaign.supplemental_settings',
                return_value={'tangentBackend':'eigen'}), redirect_stdout(io.StringIO()):
            config = Config.saved(values)
            options = dict(executable=executable, output_root=Path(td)/'output', state_dir=Path(td)/'state')
            attempt = prepare(config, **options)
            original_hash = config.config_hash
            execute(attempt)
            request = read_json(attempt/'request.json')
            self.assertEqual(request['build']['executable_sha256'], build['executable_sha256'])
            folder = Path(request['output_folder'])
            before = (folder/'macroData.csv').read_bytes()
            resumed = prepare(config, **(options | {'executable':Path(td)/'missing'}), checkpoint='latest')
            execute(resumed)
            self.assertEqual((folder/'macroData.csv').read_bytes(), before)
            self.assertEqual(Config.read(folder/'config.conf').config_hash, original_hash)
            self.assertNotIn('tangentBackend', (folder/'config.conf').read_text())
            self.assertEqual(read_json(resumed/'request.json')['executable'], request['executable'])


@contextmanager
def prepared_run(**settings):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        executable = root/'build/MTS2D'
        executable.parent.mkdir()
        shutil.copy2(BINARY, executable)
        write_json(executable.parent/'build-manifest.json', {
            'branch':'main', 'revision':'fixture', 'executable_sha256':file_digest(executable),
            'execution_interface':'folder-v1', 'configuration_keys_sha256':'historical-schema'})
        values = dict(rows=3, cols=3, loading='noRelaxation', initialGuessNoise=0,
            startLoad=-.01, totalLoad=.004, loadIncrement=.001, checkpointInterval=1,
            writeDumps=True, writeMeshVTUs=False, nrVTUFrames=0, showProgress=-1, tangentBackend='eigen')
        config = Config(**(values | settings))
        options = dict(executable=executable, output_root=root/'output', state_dir=root/'.state')
        yield root, config, options


def physical_rows(folder):
    columns = ('load_step', 'load', 'completed_steps', 'avg_energy')
    return [tuple(row[key] for key in columns) for row in read_macros(folder/'macroData.csv')]


@unittest.skipUnless(BINARY.exists(), 'Build MTS2D before simulator lifecycle tests')
class FolderExecutionTests(unittest.TestCase):
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
            resumed = prepare(config, **(options | {'executable':root/'missing-build'}), checkpoint='latest')
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
