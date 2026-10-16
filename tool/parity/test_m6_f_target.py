"""Offline checks for the DRAFT M6 F target and backend-specific measurement."""
import copy
import contextlib
import io
import tempfile
from unittest import mock
import csv
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import score

class M6FilesystemTargetTest(unittest.TestCase):
    def test_backend_case_selection(self):
        rule = {'cases': ['mounted', 'portable'], 'cases_web': ['portable']}
        self.assertEqual(score.cases_for(rule, 'vm'), ['mounted', 'portable'])
        for backend in ('dart2js', 'dart2wasm'):
            self.assertEqual(score.cases_for(rule, backend), ['portable'])
        self.assertEqual(score.cases_for({'cases': ['mounted'], 'cases_web': []}, 'dart2js'), [])
        self.assertEqual(score.cases_for({'cases': ['portable']}, 'dart2wasm'), ['portable'])

    def test_web_matrix_with_constructed_refusals_and_portable_matches(self):
        target = json.loads((score.ROOT / 'tool/parity/m6-target.json').read_text())
        rules = json.loads(score.RULES.read_text())['rows']
        cases = score.case_index()
        expected = score.ROOT / 'tool/parity/expected'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'platform.json').write_text('{"arch": "arm64"}')
            for row in target['rows']:
                rule = rules[row]
                for cid in rule.get('cases', []):
                    folder = out / cid
                    folder.mkdir(exist_ok=True)
                    mounted = bool(cases[cid].get('fs', {}).get('mounts'))
                    for backend in ('dart2js', 'dart2wasm'):
                        got = {'refused': {'where': 'MountDir', 'type': 'UnsupportedError'}} if mounted else score.load(expected / cid / 'ref-py.json')
                        (folder / f'core-{backend}.json').write_text(json.dumps(got))
                        refmem = score.load(expected / cid / 'refmem-py.json')
                        if refmem:
                            (folder / f'refmem-{backend}.json').write_text(json.dumps(refmem))
                for pid in rule.get('probes', []):
                    folder = out / pid
                    folder.mkdir(exist_ok=True)
                    got = {'refused': {'where': 'MountDir', 'type': 'UnsupportedError'}} if row.startswith('mount_') else score.load(expected / pid / 'ref-py.json')
                    for backend in ('dart2js', 'dart2wasm'):
                        (folder / f'core-{backend}.json').write_text(json.dumps(got))
            report = out / 'score.json'
            with mock.patch.object(sys, 'argv', ['score.py', str(out), '--backends', 'dart2js,dart2wasm', '--json', str(report)]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(score.main(), 0)
            measured = json.loads(report.read_text())['rows']
            for backend in ('dart2js', 'dart2wasm'):
                statuses = [measured[row][backend]['status'] for row in target['rows']]
                self.assertEqual(statuses.count('PRESENT'), 14)
                self.assertEqual(statuses.count('REFUSED'), 10)
            for row in ('working_directory', 'virtual_path_validation_normalization'):
                self.assertTrue(rules[row]['cases_web'])
                self.assertTrue(set(rules[row]['cases_web']) <= set(rules[row]['cases']))
                self.assertFalse(any(cases[c].get('fs', {}).get('mounts') for c in rules[row]['cases_web']))

    def test_recorded_handler_controls_detect_unwanted_dispatch(self):
        expected = score.ROOT / 'tool/parity/expected'
        ref = score.load(expected / 'x21_dispatch_rename_unmounted_to_mounted/ref-py.json')
        got = copy.deepcopy(ref)
        self.assertEqual(got['steps'][0]['os_calls'], [])
        got['steps'][0]['os_calls'].append({'name': 'Path.rename'})
        self.assertIn('O', score.compare_steps(ref, got, score.expand(score.ALL_CODES)))
        ref = score.load(expected / 'x21_dispatch_sleep_feed_start/ref-py.json')
        got = copy.deepcopy(ref)
        self.assertEqual(got['steps'][0]['host_calls'][0]['handler_calls'], 0)
        got['steps'][0]['host_calls'][0]['handler_calls'] = 1
        self.assertIn('H', score.compare_steps(ref, got, score.expand(score.ALL_CODES)))

    def test_draft_partition_and_allowances(self):
        target = json.loads((score.ROOT / 'tool/parity/m6-target.json').read_text())
        scope = {r['cap_id']: r for r in csv.DictReader(score.MANIFEST.read_text().splitlines(), delimiter='\t') if r['in_scope']=='yes'}
        m3 = json.loads(score.TARGET.read_text())
        wanted = {r for r, ex in m3['excluded'].items() if ex['clause'] in ('A1','B1')}
        self.assertEqual(set(target['rows']), wanted)
        self.assertEqual(len(wanted), 24)
        self.assertEqual(target['status'], 'DRAFT')
        self.assertEqual(score.target_problems(target, scope), [])
        for backend, count in [('vm', 1), ('dart2js', 10), ('dart2wasm', 10)]:
            self.assertEqual(sum(backend in r.get('allowed', {}).get('backends', []) for r in target['rows'].values()), count)
        for row, rule in target['rows'].items():
            allowed = rule.get('allowed')
            if allowed:
                self.assertEqual((allowed['kind'], allowed['status']), ('refused-as-upstream', 'REFUSED'))
                self.assertIn('monty-js/ts/node.ts:3-4', allowed['cite'])
        statuses = {r: {b: ('REFUSED' if b in v.get('allowed', {}).get('backends', []) else 'PRESENT', 'measured') for b in score.ALL_BACKENDS} for r,v in target['rows'].items()}
        self.assertFalse(score.score_target(target, statuses, list(score.ALL_BACKENDS))[1])
        broken = copy.deepcopy(target); broken['rows'].pop('mount_modes')
        self.assertTrue(score.target_problems(broken, scope))
        statuses['mount_modes']['vm'] = ('PARTIAL', 'measured')
        self.assertTrue(score.score_target(target, statuses, list(score.ALL_BACKENDS))[1])
        statuses['mount_modes']['vm'] = ('PRESENT', 'measured')
        statuses['mount_modes']['dart2js'] = ('PRESENT', 'measured')
        self.assertTrue(score.score_target(target, statuses, list(score.ALL_BACKENDS))[1])

if __name__ == '__main__': unittest.main()
