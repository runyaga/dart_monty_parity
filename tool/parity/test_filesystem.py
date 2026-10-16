"""Offline P-F1 scoring and refmem runner regression tests."""
from __future__ import annotations

import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run
import score
from probes_compare import judge

BACKENDS = ('vm', 'dart2js', 'dart2wasm')
EXPECTED = score.ROOT / 'tool/parity/expected'


class FilesystemScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=score.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.rules = json.loads(score.RULES.read_text())['rows']

    def write(self, cid, engine, backend, doc):
        folder = self.out / cid
        folder.mkdir(exist_ok=True)
        (folder / f'{engine}-{backend}.json').write_text(json.dumps(doc))

    def test_b1_vm_uses_host_and_web_and_memory_rows_use_refmem(self):
        target = json.loads(score.TARGET.read_text())
        rows = [row for row, ex in target['excluded'].items() if ex['clause'] == 'B1']
        self.assertEqual(len(rows), 10)
        for row in rows + ['memory_filesystem']:
            rule = self.rules[row]
            for cid in rule['cases']:
                for backend in BACKENDS:
                    memory = row == 'memory_filesystem' or backend != 'vm'
                    ref = score.load(EXPECTED / cid / ('refmem-py.json' if memory else 'ref-py.json'))
                    self.assertIsNotNone(ref, cid)
                    with self.subTest(row=row, cid=cid, backend=backend):
                        self.write(cid, 'core' if memory else 'refmem', backend, {'runner_error': 'wrong column'})
                        got = copy.deepcopy(ref)
                        for step in got['steps']:
                            step['host_calls'] = []  # The shared Dart runner's step schema.
                        self.write(cid, 'refmem' if memory else 'core', backend, got)
                        self.assertEqual(score.classify_case(cid, backend, rule, self.out, EXPECTED)[0], 'MATCH')

    def test_refmem_missing_does_not_fall_back_to_core(self):
        cid = 'b11fs_read_text'
        for backend in BACKENDS[1:]:
            with self.subTest(backend=backend):
                self.write(cid, 'core', backend, score.load(EXPECTED / cid / 'ref-py.json'))
                self.assertEqual(score.classify_case(cid, backend, self.rules['os_file_reads'],
                                                     self.out, EXPECTED)[0], 'NO-OUTCOME')

    def test_historical_filesystem_uses_core_and_host_oracle(self):
        for row, rule in self.rules.items():
            if rule.get('engine') != 'refmem' and rule.get('engine_web') != 'refmem':
                continue
            for cid in rule['cases']:
                for backend in BACKENDS:
                    with self.subTest(row=row, cid=cid, backend=backend):
                        got = score.load(EXPECTED / cid / 'ref-py.json')
                        self.write(cid, 'core', backend, got)
                        self.assertEqual(score.classify_case(cid, backend, rule, self.out, EXPECTED,
                                                             historical=True)[0], 'MATCH')
                        # Historical selection is explicit even if a refmem file exists.
                        self.write(cid, 'refmem', backend, {'runner_error': 'wrong instrument'})
                        self.assertEqual(score.classify_case(cid, backend, rule, self.out, EXPECTED,
                                                             historical=True)[0], 'MATCH')
        # This case distinguishes the two oracles, including for memory_filesystem.
        cid = 'b11fs_rename_missing'
        self.assertNotEqual(score.load(EXPECTED / cid / 'ref-py.json')['steps'][0]['exception']['message'],
                            score.load(EXPECTED / cid / 'refmem-py.json')['steps'][0]['exception']['message'])

    def test_cli_selects_historical_only_for_historical_assertions(self):
        (self.out / 'platform.json').write_text('{"arch": "arm64"}')
        for flags, historical in [([], False), (['--assert-rows'], True), (['--assert-baseline'], True),
                                  (['--assert-rows', '--assert-baseline'], True)]:
            with self.subTest(flags=flags), \
                 mock.patch.object(sys, 'argv', ['score.py', str(self.out), *flags]), \
                 mock.patch.object(score, 'classify_case', return_value=('NO-OUTCOME', ['!'], [])) as classify, \
                 contextlib.redirect_stdout(io.StringIO()):
                score.main()
                self.assertTrue(classify.called)
                self.assertEqual({call.kwargs['historical'] for call in classify.call_args_list}, {historical})

    def test_historical_flags_cannot_disable_current_checks(self):
        for flag in ['--assert-current', '--write-current']:
            with self.subTest(flag=flag), \
                 mock.patch.object(sys, 'argv', ['score.py', str(self.out), '--assert-baseline', flag, 'unused.json']), \
                 contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                score.main()
            self.assertEqual(error.exception.code, 2)

    def test_mount_unsupported_cannot_match_even_without_config_comparison(self):
        cid = 'b11fs_read_text'
        got = score.load(EXPECTED / cid / 'ref-py.json')
        got['unsupported'] = {'fs.mounts': 'until P-F2'}
        self.write(cid, 'core', 'vm', got)
        cls, _, _ = score.classify_case(cid, 'vm', {'compare': ['V']}, self.out, EXPECTED)
        self.assertEqual(cls, 'UNSUPPORTED')
        self.assertEqual(score.row_status([cls]), 'ABSENT')

    def test_rename_message_is_exact_and_old_exception_is_rejected(self):
        cid = 'b11fs_rename_missing'
        got = score.load(EXPECTED / cid / 'refmem-py.json')
        got['steps'][0]['exception']['message'] += " -> '/data/z.txt'"
        self.write(cid, 'refmem', 'vm', got)
        rule = self.rules['memory_filesystem']
        self.assertEqual(score.classify_case(cid, 'vm', rule, self.out, EXPECTED), ('DIVERGE', ['Em'], []))
        with self.assertRaises(SystemExit):
            score.check_exceptions({'memory_filesystem': {'exceptions': [
                {'name': 'rename-missing-message', 'ignore': ['Em']}]}})

    def test_budget_probe_unsupported_is_absent(self):
        got = {'events': [], 'unsupported': {'fs.mounts': 'until P-F2'}}
        cls, _, _ = judge('api_memory_budget', {'events': []}, got, {})
        self.assertEqual(cls, 'UNSUPPORTED')
        self.assertEqual(score.row_status([cls]), 'ABSENT')

    def test_callback_exception_only_tolerates_host_type(self):
        ref = score.load(EXPECTED / 'api_callback_files/ref-py.json')
        got = copy.deepcopy(ref)
        for event in got['events']:
            if event.get('op') == 'write':
                event['type'] = 'String'
        rule = self.rules['callback_files']
        self.assertEqual(judge('api_callback_files', ref, got, rule)[0], 'MATCH')
        for event in got['events']:
            if event.get('op') == 'write':
                event['text'] = 'wrong'
        self.assertEqual(judge('api_callback_files', ref, got, rule)[0], 'DIVERGE')


class RefmemRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=score.ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.cases = [score.CASES / 'os_fs/b11fs_read_text.json', score.CASES / 'os_fs/b14_getcwd_set.json']

    def test_refmem_selects_only_marked_cases_and_clears_stale_outcomes(self):
        for backend in BACKENDS:
            stale = self.out / 'b11fs_read_text' / f'refmem-{backend}.json'
            stale.parent.mkdir(exist_ok=True)
            stale.write_text('{}')
            def invoke(cases, out, log, engine, probes=()):
                self.assertEqual(cases, self.cases[:1])
                self.assertEqual(engine, 'refmem')
                self.assertFalse(stale.exists())
                return 0
            with mock.patch.object(run, 'run_worker_vm', side_effect=invoke) as vm, \
                 mock.patch.object(run, 'run_web', side_effect=lambda b, c, o, l, core, e: invoke(c, o, l, e)) as web:
                self.assertEqual(run.run_refmem(backend, self.cases, self.out, self.out), 0)
                (vm if backend == 'vm' else web).assert_called_once()

    def test_vm_refmem_uses_session_entrypoint_and_marks_missing_output(self):
        log = self.out / 'run.log'
        with mock.patch.object(run, 'sh', return_value=1) as sh:
            self.assertEqual(run.run_worker_vm(self.cases[:1], self.out, log, 'refmem'), 1)
        command = sh.call_args.args[0]
        self.assertIn('bin/parity_session_vm.dart', command)
        self.assertIn('--engine=refmem', command)
        got = score.load(self.out / 'b11fs_read_text/refmem-vm.json')
        self.assertTrue(got['runner_error'])

    def test_memory_failure_fails_each_requested_engine(self):
        for engine in run.ENGINES:
            args = mock.Mock(engine=engine, backend='vm', cases=['selected'], no_crash_tests=True)
            with mock.patch.object(run, 'run_refmem', return_value=1) as memory, \
                 mock.patch.object(run, 'run_vm', return_value=0), \
                 mock.patch.object(run, 'run_worker_vm', return_value=0):
                self.assertEqual(run.run_selected(args, self.cases, self.out, self.out / 'log', self.out), 1)
                memory.assert_called_once_with('vm', self.cases, self.out, self.out)

    def test_generator_check_detects_stale_input_without_rewriting(self):
        pkg = self.out / 'pkg'
        (pkg / 'test').mkdir(parents=True)
        with mock.patch.object(run, 'PKG', pkg):
            self.assertFalse(run.bake(self.cases, engine='core', check=True))
            self.assertTrue(run.bake(self.cases, engine='core'))
            self.assertTrue(run.bake(self.cases, engine='core', check=True))
            generated = (pkg / 'test/cases.g.dart').read_bytes()
            self.assertFalse(run.bake(self.cases[:1], engine='core', check=True))
            self.assertEqual((pkg / 'test/cases.g.dart').read_bytes(), generated)

    def test_web_refmem_transport_writes_both_web_backends(self):
        for backend in BACKENDS[1:]:
            log = self.out / f'{backend}.log'
            res = run.placeholder('b11fs_read_text', backend, 'test transport', 'refmem')
            log.write_text(run.MARKER + json.dumps(res) + '\n')
            with mock.patch.object(run, 'PKG', self.out / 'pkg'), \
                 mock.patch.object(run, 'bake'), mock.patch.object(run, 'web_test', return_value=0):
                self.assertEqual(run.run_web(backend, self.cases[:1], self.out, log, self.out, 'refmem'), 0)
            self.assertEqual(score.load(self.out / f'b11fs_read_text/refmem-{backend}.json'), res)


if __name__ == '__main__':
    unittest.main()
