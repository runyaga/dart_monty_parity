import 'package:monty_parity/src/telemetry_probes.dart';
import 'package:test/test.dart';

void main() {
  test('the telemetry family exposes all six scenarios', () {
    expect(
      telemetryProbeIds,
      containsAll(const [
        'telemetry_plain',
        'telemetry_print',
        'telemetry_external',
        'telemetry_manual',
        'telemetry_error',
        'telemetry_dump_load',
      ]),
    );
  });
}
