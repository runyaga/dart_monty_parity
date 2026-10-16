"""Offline oracle regressions, using the pinned Python binding."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cases as runner


class DispatchTest(unittest.TestCase):
    def run_case(self, **options):
        case = {'id': 'dispatch_test', 'code': 'import time\ntime.sleep(0)\n7', **options}
        with tempfile.TemporaryDirectory() as tmp, runner.pm.Monty() as pool:
            return runner.run_case(pool, case, Path(tmp))

    def test_snapshot_requirement_implies_feed_start(self):
        with self.assertRaisesRegex(RuntimeError, 'OS suspension .* not reached'):
            self.run_case(code='1', record_dispatch=True, expect_os_snapshot='system.sleep')

    def test_manual_dispatch_implies_feed_start(self):
        result = self.run_case(record_dispatch=True, dispatch='manual', os={'system.sleep': [{'return': {'t': 'none'}}]})
        self.assertEqual(result['steps'][0]['host_calls'][0]['ev'], 'start')
        self.assertEqual(len(result['steps'][0]['os_calls']), 1)

    def test_sleep_access_does_not_require_dispatch_recording_or_mounts(self):
        result = self.run_case(sleep_access=True)
        self.assertEqual(result['steps'][0]['host_calls'], [{'ev': 'handler', 'name': 'system.sleep'}])

    def test_start_count_excludes_unrelated_host_events(self):
        decode = runner.decode
        def with_event(value, ctx=None, memo=None):
            if ctx is not None:
                ctx.log.append({'op': 'unrelated-host-event'})
            return decode(value, ctx, memo)
        with mock.patch.object(runner, 'decode', side_effect=with_event):
            result = self.run_case(record_dispatch=True, feed_start=True,
                                   inputs={'x': {'t': 'int', 'v': '1'}})
        start = next(e for e in result['steps'][0]['host_calls'] if e.get('ev') == 'start')
        self.assertEqual(start['handler_calls'], 0)

    def test_seed_root_entry_is_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner.seed_files(Path(tmp), '/data', {'/data': {}})
            self.assertTrue(Path(tmp).is_dir())


if __name__ == '__main__':
    unittest.main()
