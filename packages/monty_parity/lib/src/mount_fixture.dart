/// Host directories owned by one parity case (never part of the refmem run).
library;

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/mount_fixture_web.dart'
    if (dart.library.io) 'package:monty_parity/src/mount_fixture_vm.dart'
    as platform;

/// Builds the case's mounts, including constructor failures on the web.
MountFixture buildMountFixture(Map<String, Object?> c) => platform.build(c);

/// Open roots plus cleanup and stable-path encoding for their outcome.
final class MountFixture {
  /// Creates an owned fixture.
  MountFixture(
    this.mounts, {
    this.root = '',
    this.error,
    void Function()? cleanup,
  }) : _cleanup = cleanup;

  /// Mounts in case order; feeds may select a subset.
  final List<MountDir> mounts;

  /// Canonical temporary root, replaced in recorded messages.
  final String root;

  /// A host-side constructor refusal, before any feed.
  final Object? error;
  final void Function()? _cleanup;

  /// Constructor attributes, observable even without a feed.
  Map<String, Object?> get attributes => {
    't': 'list',
    'items': [
      for (final m in mounts)
        {
          't': 'tuple',
          'items': [
            {'t': 'str', 'v': m.hostPath},
            {'t': 'str', 'v': m.virtualPath},
            {'t': 'str', 'v': m.mode},
          ],
        },
    ],
  };

  /// Replaces only this fixture's root, preserving the case/mount suffix.
  Object? stable(Object? value) => switch (value) {
    final String s when root.isNotEmpty => s.replaceAll(root, '<fsroot>'),
    final List<Object?> xs => xs.map(stable).toList(),
    final Map<String, Object?> m => m.map((k, v) => MapEntry(k, stable(v))),
    _ => value,
  };

  /// Releases every capability before deleting its temporary directory.
  void close() {
    for (final m in mounts) {
      m.close();
    }
    _cleanup?.call();
  }
}
