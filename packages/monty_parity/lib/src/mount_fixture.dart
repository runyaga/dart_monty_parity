/// Host directories owned by one parity case (never part of the refmem run).
library;

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/mount_fixture_web.dart'
    if (dart.library.io) 'package:monty_parity/src/mount_fixture_vm.dart'
    as platform;

/// Builds the case's mounts, including constructor failures on the web.
MountFixture buildMountFixture(Map<String, Object?> c) => platform.build(c);

/// A refusal caught only at the browser MountDir constructor boundary.
final class BrowserMountRefused implements Exception {
  /// Wraps core's browser constructor failure.
  const BrowserMountRefused(this.error);

  /// The original constructor error.
  final UnsupportedError error;
}

/// Pass explicit caps through while leaving core's default cap under test.
MountDir constructMount(String hostPath, Map<String, Object?> spec) {
  final virtualPath = spec['virtual']! as String;
  final mode = (spec['mode'] as String?) ?? 'read-write';
  final writeBytesLimit = spec['write_bytes_limit'] as int?;
  final memoryUsageLimit = spec['memory_usage_limit'] as int?;
  if (memoryUsageLimit == null) {
    return MountDir(
      hostPath: hostPath,
      virtualPath: virtualPath,
      mode: mode,
      writeBytesLimit: writeBytesLimit,
    );
  }
  return MountDir(
    hostPath: hostPath,
    virtualPath: virtualPath,
    mode: mode,
    writeBytesLimit: writeBytesLimit,
    memoryUsageLimit: memoryUsageLimit,
  );
}

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
