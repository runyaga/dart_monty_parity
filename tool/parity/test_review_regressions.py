"""Scorer boundary regressions: every omitted web case still has a verdict."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import score


class WebSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        (self.out / 'platform.json').write_text('{"arch":"arm64"}')
        self.doc = json.loads(score.RULES.read_text())
        self.rule = copy.deepcopy(self.doc['rows']['working_directory'])
        self.doc['rows'] = {'working_directory': self.rule}
        self.path = self.out / 'rules.json'

    def run_score(self, *flags):
        self.path.write_text(json.dumps(self.doc))
        with mock.patch.object(score, 'RULES', self.path), \
             mock.patch.object(sys, 'argv', ['score.py', str(self.out), '--backends', 'dart2js', *flags]), \
             contextlib.redirect_stdout(io.StringIO()):
            return score.main()

    def seed(self):
        for cid in self.rule['cases']:
            mounted = bool(score.case_index()[cid].get('fs', {}).get('mounts'))
            got = ({'refused': {'where': 'MountDir', 'type': 'UnsupportedError'}} if mounted
                   else score.load(score.ROOT / 'tool/parity/expected' / cid / 'ref-py.json'))
            path = self.out / cid / 'core-dart2js.json'
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(got))

    def test_excluded_mounted_cases_must_refuse(self):
        self.seed()
        self.assertEqual(self.run_score(), 0)
        excluded = set(self.rule['cases']) - set(self.rule['cases_web'])
        self.assertTrue(excluded)
        for cid in excluded:
            path = self.out / cid / 'core-dart2js.json'
            original = path.read_text()
            for got in (None, {'runner_error': 'crash'}, {'steps': []},
                        score.load(score.ROOT / 'tool/parity/expected' / cid / 'ref-py.json')):
                with self.subTest(case=cid, outcome=got):
                    if got is None:
                        path.unlink()
                    else:
                        path.write_text(json.dumps(got))
                    self.assertEqual(self.run_score(), 1)
                    path.write_text(original)

    def test_omitted_portable_case_with_passing_probe_still_fails(self):
        self.seed()
        self.rule['cases_web'] = []
        self.rule['probes'] = ['api_run_blocking']
        cid = self.rule['cases'][0]
        self.assertFalse(score.case_index()[cid].get('fs', {}).get('mounts'))
        (self.out / cid / 'core-dart2js.json').unlink()
        with mock.patch.object(score, 'probe_status', return_value=('PRESENT', [
                {'case': 'api_run_blocking', 'class': 'MATCH'}])):
            self.assertEqual(self.run_score(), 1)

    def test_status_cannot_declare_success_without_evidence(self):
        for rule in ({'status': 'PRESENT'}, {'status': 'REFUSED'},
                     {'allowed': 'REFUSED-as-upstream'},
                     {'allowed': 'REFUSED-as-upstream', 'reason': 'x', 'status': 'PRESENT'}):
            with self.subTest(rule=rule), self.assertRaises(SystemExit):
                self.doc['rows'] = {'mount_usage_inspection': rule}
                self.run_score()

    def test_valid_upstream_declaration_needs_no_status_escape_hatch(self):
        self.doc['rows'] = {'mount_usage_inspection': {
            'allowed': 'REFUSED-as-upstream', 'reason': 'no binding API (P13)'}}
        report = self.out / 'report.json'
        self.assertEqual(self.run_score('--json', str(report)), 0)
        self.assertEqual(json.loads(report.read_text())['rows']['mount_usage_inspection']['dart2js']['status'], 'REFUSED')

    def test_raw_worker_unsupported_dispatch_fails_scoring(self):
        cid = 'x21_dispatch_sleep_feed_start'
        ref = score.load(score.ROOT / 'tool/parity/expected' / cid / 'ref-py.json')
        folder = self.out / cid
        folder.mkdir()
        for key in ('feed_start', 'dispatch', 'expect_os_snapshot', 'record_dispatch',
                    'sleep_access', 'mount_indices', 'restore_mount'):
            got = copy.deepcopy(ref)
            got['unsupported'] = {key: 'requires the public session driver'}
            (folder / 'worker-vm.json').write_text(json.dumps(got))
            with self.subTest(key=key):
                cls, bad = score.classify_worker(cid, 'vm', self.out,
                    score.ROOT / 'tool/parity/expected')
                self.assertEqual(cls, 'DIVERGE')
                self.assertIn('C', bad)

    def test_historical_selection_ignores_current_web_controls(self):
        self.rule['controls_web'] = ['new-control-not-in-history']
        self.rule.pop('historical', None)
        with mock.patch.object(score, 'classify_case', return_value=('MATCH', [], [])) as classify:
            self.run_score('--assert-rows')
        self.assertEqual([call.args[0] for call in classify.call_args_list], self.rule['cases'])
        self.assertTrue(all(call.kwargs['historical'] for call in classify.call_args_list))


if __name__ == '__main__':
    unittest.main()
