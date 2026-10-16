@TestOn('vm')
library;

import 'dart:convert';
import 'dart:io';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/telemetry_probes.dart';
import 'package:monty_parity/src/telemetry_recorder.dart';
import 'package:test/test.dart';

void main() {
  setUpAll(installParityTelemetry);
  for (final id in telemetryProbeIds) {
    test('$id matches the Python oracle', () async {
      final events = <Map<String, Object?>>[];
      await runTelemetryProbe(
        id,
        () => Monty.start(maxProcesses: 1),
        events.add,
      );
      final reference =
          jsonDecode(
                File(
                  '../../tool/parity/expected/$id/ref-py.json',
                ).readAsStringSync(),
              )
              as Map<String, Object?>;
      expect(events, reference['events']);
    });
  }
}
