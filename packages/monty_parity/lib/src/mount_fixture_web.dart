/// Web construction deliberately exercises core's MountDir refusal.
library;

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/mount_fixture.dart';

/// Builds the first requested mount (core throws UnsupportedError).
MountFixture build(Map<String, Object?> c) {
  final fs = (c['fs'] as Map?) ?? const {};
  final specs = (fs['mounts'] as List?) ?? const [];
  return MountFixture([
    for (final spec in specs)
      MountDir(
        hostPath: '/unavailable',
        virtualPath: (spec as Map)['virtual'] as String,
      ),
  ]);
}
