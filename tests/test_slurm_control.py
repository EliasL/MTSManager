"""Scheduler selection must preserve unrelated, active and unreviewed jobs."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from MTS_Management.cluster import Site, call
from MTS_Management.config import Config
from MTS_Management.files import write_json
from MTS_Management.slurm_control import matches, remote, validate_rule


class SlurmControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.output.mkdir()
        self.site = Site(name='fixture', directory=str(self.root), output_root='output', state_dir='.state', mode='slurm')
        self.jobs, index = {}, {}
        for job_id, size, seed, state, reason in (
                ('101', 800, 0, 'PENDING', 'Priority'), ('102', 800, 1, 'RUNNING', 'node1'),
                ('103', 100, 0, 'PENDING', 'Priority'), ('104', 800, 2, 'PENDING', 'JobHeldUser'),
                ('105', 800, 3, 'PENDING', 'Priority')):
            config = Config(rows=size, cols=size, seed=seed)
            folder = self.output / config.folder_name()
            folder.mkdir()
            config.write(folder / 'config.conf')
            write_json(folder / 'manifest.json', {'settings': config.as_dict(), 'config_hash': config.config_hash, 'campaign': 'Example'})
            attempt = folder / 'attempts/attempt'
            attempt.mkdir(parents=True)
            write_json(attempt / 'status.json', {'scheduler_id': job_id})
            index[config.config_hash] = folder.name
            self.jobs[job_id] = dict(job_id=job_id, state=state, name=folder.name, reason=reason)
        write_json(self.output / '.run-index.json', index)
        self.rule = dict(action='hold', match='all', criteria=[dict(field='L', operator='eq', value=800)])
        self.payload = {'site': asdict(self.site), 'options': {'rule': self.rule}}

    def test_hold_changes_only_reviewed_pending_matches_and_records_outcome(self):
        def scheduler(command, **kwargs):
            self.assertEqual(command[:2], ['scontrol', 'hold'])
            for job_id in command[2:]:
                self.jobs[job_id]['reason'] = 'JobHeldUser'
            from subprocess import CompletedProcess
            return CompletedProcess(command, 0, '', '')
        with patch('MTS_Management.slurm_control.queue', side_effect=lambda: {k: dict(v) for k, v in self.jobs.items()}), \
                patch('MTS_Management.slurm_control.subprocess.run', side_effect=scheduler):
            plan = remote(self.payload)
            self.assertEqual({j['job_id'] for j in plan['jobs']}, {'101', '105'})
            applied = remote(self.payload | {'options': {'rule': self.rule, 'apply': True, 'job_ids': ['101', '103', '104']}})
        self.assertEqual(applied['applied'], ['101'])
        self.assertEqual(self.jobs['102']['state'], 'RUNNING')
        self.assertEqual(self.jobs['103']['reason'], 'Priority')
        self.assertEqual(self.jobs['105']['reason'], 'Priority')
        records = [json.loads(line) for line in (self.root / '.state/job-log.jsonl').read_text().splitlines()]
        self.assertEqual(records[-1]['applied'], ['101'])

    def test_job_starting_after_review_is_excluded_before_mutation(self):
        calls = 0
        def changing_queue():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.jobs['101']['state'] = 'RUNNING'
            return {k: dict(v) for k, v in self.jobs.items()}
        with patch('MTS_Management.slurm_control.queue', side_effect=changing_queue):
            result = remote(self.payload | {'options': {'rule': self.rule, 'apply': True, 'job_ids': ['101']}})
        self.assertEqual(result['applied'], [])
        self.assertEqual(result['skipped'], ['101'])
        self.assertEqual(result['command'], '')
        self.assertEqual(self.jobs['101']['reason'], 'Priority')
        self.assertFalse((self.root / '.state/job-log.jsonl').exists())

    def test_any_all_criteria_and_client_program_transport(self):
        config = Config(rows=800, cols=800, reconnectionMethod='none')
        rule = self.rule | {'criteria': self.rule['criteria'] + [dict(field='reconnectionMethod', operator='eq', value='edgeFlip')]}
        self.assertFalse(matches(config, {'campaign': 'Example'}, validate_rule(rule)))
        self.assertTrue(matches(config, {'campaign': 'Example'}, validate_rule(rule | {'match': 'any'})))
        result = call(self.site, 'fixture', _program="print(json.dumps({'value': __mts_control_request['options']['value']}))", value='unchanged')
        self.assertEqual(result, {'value': 'unchanged'})


if __name__ == '__main__':
    unittest.main()
