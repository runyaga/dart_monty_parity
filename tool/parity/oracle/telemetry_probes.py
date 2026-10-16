"""R-P2: one process-wide SDK installation, six independently buffered runs.

Measurements are a multiset, not SDK aggregates: scheduling may interleave
instruments, but no increment, attribute, or repeated measurement is lost.
"""
from __future__ import annotations

import json
import time

import pydantic_monty as pm
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor

SCENARIOS = ('plain', 'print', 'external', 'manual', 'error', 'dump_load')
OMIT = {'code.file.path', 'code.line.number', 'code.module.name', 'busy_ns', 'idle_ns', 'worker_pid'}
MASK = {'total_execution_micros', 'feed_execution_micros', 'call_id', 'state_bytes'}


def attributes(raw):
    return {k: ({'type': type(v).__name__, 'masked': True} if k in MASK else v)
            for k, v in sorted(raw.items()) if k not in OMIT and not k.startswith(('logfire.', 'thread.'))}


class Instrument:
    def __init__(self, meter, name, kind, unit, description):
        self.meter, self.name, self.kind = meter, name, kind
        self.unit, self.description = unit, description

    def record(self, value, attributes=None, context=None):
        self.meter.records.append({
            'name': self.name, 'kind': self.kind, 'unit': self.unit,
            'description': self.description, 'value': value, 'attributes': dict(attributes or {}),
            'empty_context': isinstance(context, Context) and not context,
        })

    add = record


class RecordingMeter:
    def __init__(self):
        self.records = []

    def create_counter(self, name, unit='', description=''):
        return Instrument(self, name, 'counter', unit, description)

    def create_up_down_counter(self, name, unit='', description=''):
        return Instrument(self, name, 'up_down_counter', unit, description)

    def create_histogram(self, name, unit='', description=''):
        return Instrument(self, name, 'histogram', unit, description)


class Recorder:
    def __init__(self):
        self.spans = InMemorySpanExporter()
        self.tp = TracerProvider()
        self.tp.add_span_processor(SimpleSpanProcessor(self.spans))
        self.logs = InMemoryLogRecordExporter()
        self.lp = LoggerProvider()
        self.lp.add_log_record_processor(SimpleLogRecordProcessor(self.logs))
        self.meter = RecordingMeter()
        self.tracer = self.tp.get_tracer('parity')
        pm.instrument_telemetry(tracer=self.tracer, meter=self.meter, logger=self.lp.get_logger('parity'))

    def reset(self):
        self.spans.clear()
        self.logs.clear()
        self.meter.records.clear()
        self.begin = time.time_ns()

    def result(self, sizes):
        end = time.time_ns()
        spans = sorted(self.spans.get_finished_spans(), key=lambda s: s.start_time)
        host = next(s for s in spans if s.name == 'app-request')
        spans.remove(host)
        labels = {s.context.span_id: i for i, s in enumerate(spans)}
        by_id = {s.context.span_id: s for s in [host, *spans]}
        def parent(sid):
            return 'app-request' if sid == host.context.span_id else labels[sid]
        def timestamp(n):
            assert type(n) is int and self.begin <= n <= end, ('epoch nanoseconds', n)
        for s in [host, *spans]:
            timestamp(s.start_time)
            timestamp(s.end_time)
            assert s.start_time <= s.end_time
            if s.parent:
                p = by_id[s.parent.span_id]
                assert p.start_time <= s.start_time <= s.end_time <= p.end_time
        for s in spans:
            if s.name.startswith('session '):
                assert s.attributes['monty_version'] == '1.1.0'
        state_sizes = [int(s.attributes['state_bytes']) for s in spans if 'state_bytes' in s.attributes]
        assert state_sizes == sizes, ('state_bytes', state_sizes, sizes)
        logs = []
        last_log = self.begin
        for record in self.logs.get_finished_logs():
            log = record.log_record
            timestamp(log.timestamp)
            assert last_log <= log.timestamp
            last_log = log.timestamp
            p = by_id[log.span_id]
            assert p.start_time <= log.timestamp <= p.end_time
            logs.append({'parent': parent(log.span_id), 'severity': log.severity_text,
                         'severity_number': log.severity_number.value, 'body': log.body,
                         'attributes': attributes(log.attributes or {}), 'timestamp_valid': True})
        metrics = []
        snapshots = []
        for raw in self.meter.records:
            m = dict(raw)
            assert m['empty_context'], m
            assert m['value'] >= 0 or m['kind'] == 'up_down_counter', m
            if m['name'] == 'monty.snapshot.bytes':
                snapshots.append(m['value'])
            if m['unit'] == 's':
                m['value'] = '<seconds>'
            elif m['name'] in ('monty.snapshot.bytes', 'monty.wire.frame.bytes'):
                assert isinstance(m['value'], int) and m['value'] > 0
                m['value'] = '<bytes>'
            metrics.append(m)
        assert snapshots == sizes[:1], ('snapshot.bytes', snapshots, sizes)
        return {'ev': 'telemetry', 'spans': [
            {'name': s.name, 'parent': parent(s.parent.span_id) if s.parent else None,
             'attributes': attributes(s.attributes), 'timestamps_valid': True}
            for s in spans], 'logs': logs,
            'metrics': sorted(metrics, key=lambda m: json.dumps(m, sort_keys=True)),
            'sizes_valid': True}


RECORDER = None


def install():
    global RECORDER
    RECORDER = Recorder()


def scenario(name, t):
    r = RECORDER
    r.reset()
    sizes = []
    with r.tracer.start_as_current_span('app-request'):
        # A fresh pool captures the entire worker lifecycle, including shutdown.
        with pm.Monty(max_processes=1) as pool:
            with pool.checkout(limits={"max_memory": 50_000_000}, assert_message_annotations=120) as s:
                if name == 'plain':
                    t.add({'ev': 'value', 'value': s.feed_run('1 + 1')})
                elif name == 'print':
                    printed = []
                    s.feed_run("print('é🙂')", print_callback=lambda stream, text: printed.append(text))
                    t.add({'ev': 'printed', 'text': ''.join(printed)})
                elif name == 'external':
                    def ext(x):
                        t.add({'ev': 'callback_context', 'name': getattr(trace.get_current_span(), 'name', None)})
                        return x * 2
                    t.add({'ev': 'value', 'value': s.feed_run('ext(21)', external_lookup={'ext': ext})})
                elif name == 'manual':
                    snap = s.feed_start('ext(21)')
                    t.add({'ev': 'trace_context', 'name': getattr(trace.get_current_span(snap.trace_context()), 'name', None)})
                    t.add({'ev': 'value', 'value': snap.resume({'return_value': 42}).output})
                    try:
                        snap.trace_context()
                    except RuntimeError:
                        t.add({'ev': 'context_after_resume', 'refused': True})
                elif name == 'error':
                    try:
                        s.feed_run('1/0')
                    except pm.MontyRuntimeError as e:
                        t.add({'ev': 'error_type', 'type': type(e.exception()).__name__})
                elif name == 'dump_load':
                    s.feed_run('x = 42')
                    state = s.dump()
                    sizes.append(len(state))
            if name == 'dump_load':
                with pool.checkout(limits={"max_memory": 50_000_000}, assert_message_annotations=120) as restored:
                    restored.load_session(state)
                    sizes.append(len(state))
                    t.add({'ev': 'value', 'value': restored.feed_run('x')})
    t.add(r.result(sizes))
