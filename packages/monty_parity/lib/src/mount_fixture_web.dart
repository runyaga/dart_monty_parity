/// Web construction deliberately exercises core's MountDir refusal.
library;

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/mount_fixture.dart';

/// Builds the first requested mount (core throws UnsupportedError).
MountFixture build(Map<String, Object?> c) {
  final fs = (c['fs'] as Map?) ?? const {};
  final specs = (fs['mounts'] as List?) ?? const [];
  final mounts = <MountDir>[];
  for (final spec in specs) {
    try {
      mounts.add(
        constructMount('/unavailable', (spec as Map).cast<String, Object?>()),
      );
      // Only this constructor's platform refusal is evidence for web.
      // ignore: avoid_catching_errors
    } on UnsupportedError catch (error) {
      for (final mount in mounts) {
        mount.close();
      }
      throw BrowserMountRefused(error);
    }
  }
  return MountFixture(mounts);
}
