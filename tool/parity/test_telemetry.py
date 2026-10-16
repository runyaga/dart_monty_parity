#!/usr/bin/env python3
"""Offline negative controls: telemetry fields and raw measurements must count."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probes_compare import judge

EXPECTED = Path(__file__).parent / 'expected'


def reference(scenario):
    return json.loads((EXPECTED / f'telemetry_{scenario}/ref-py.json').read_text())


class TelemetryComparisonTest(unittest.TestCase):
    def rejects(self, scenario, mutate):
        ref = reference(scenario)
        got = copy.deepcopy(ref)
        mutate(got['events'][-1])
        self.assertEqual(judge(ref['probe'], ref, got, {})[0], 'DIVERGE')

    def test_all_scenarios_match_themselves(self):
        for path in EXPECTED.glob('telemetry_*/ref-py.json'):
            ref = json.loads(path.read_text())
            self.assertIsNone(ref['runner_error'])
            self.assertEqual(judge(ref['probe'], ref, ref, {})[0], 'MATCH')

    def test_raw_counter_increments_and_worker_decrements(self):
        for scenario, name, value in [('print', 'monty.print.bytes', 8),
                                      ('manual', 'monty.run.suspensions', 2),
                                      ('plain', 'monty.pool.workers.live', 0)]:
            def mutate(event):
                m = next(m for m in event['metrics'] if m['name'] == name)
                self.assertNotEqual(m['value'], value)
                m['value'] = value
            self.rejects(scenario, mutate)

    def test_frame_and_snapshot_counts_and_attributes(self):
        for name in ('monty.snapshot.bytes', 'monty.wire.frame.bytes'):
            def drop(event):
                event['metrics'].remove(next(m for m in event['metrics'] if m['name'] == name))
            self.rejects('dump_load', drop)
            def attribute(event):
                next(m for m in event['metrics'] if m['name'] == name)['attributes'] = {'wrong': 'value'}
            self.rejects('dump_load', attribute)

    def test_metric_kind_unit_and_empty_context(self):
        for field, value in [('kind', 'counter'), ('unit', 'ms'), ('empty_context', False)]:
            self.rejects('plain', lambda event: event['metrics'][0].__setitem__(field, value))

    def test_span_parent_version_language_and_masked_type(self):
        self.rejects('plain', lambda e: e['spans'][1].__setitem__('parent', 'app-request'))
        self.rejects('plain', lambda e: e['spans'][0]['attributes'].__setitem__('monty_version', 'core'))
        self.rejects('plain', lambda e: e['spans'][1]['attributes'].__setitem__('code.language', 'dart'))
        self.rejects('error', lambda e: e['logs'][0]['attributes']['total_execution_micros'].__setitem__('type', 'str'))

    def test_log_severity_body_attributes_and_order(self):
        for field, value in [('severity_number', 9), ('severity', 'INFO'), ('body', 'wrong'),
                             ('attributes', {}), ('timestamp_valid', False)]:
            self.rejects('error', lambda event: event['logs'][0].__setitem__(field, value))
        self.rejects('plain', lambda e: e['spans'][0].__setitem__('timestamps_valid', False))
        self.rejects('dump_load', lambda e: e.__setitem__('sizes_valid', False))

    def test_callback_and_snapshot_context_are_observed(self):
        for scenario in ('external', 'manual'):
            ref = reference(scenario)
            got = copy.deepcopy(ref)
            got['events'][0]['name'] = 'app-request'
            self.assertEqual(judge(ref['probe'], ref, got, {})[0], 'DIVERGE')


if __name__ == '__main__':
    unittest.main()
