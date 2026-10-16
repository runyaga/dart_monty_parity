/// VM fixture equivalent to oracle/run_cases.py build_mounts.
library;

import 'dart:io';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:monty_parity/src/mount_fixture.dart';

/// Opens fresh seeded directories and retains them across feeds.
MountFixture build(Map<String, Object?> c) {
  final fs = (c['fs'] as Map?) ?? const {};
  final specs = (fs['mounts'] as List?) ?? const [];
  if (specs.isEmpty) return MountFixture([]);
  final temp = Directory.systemTemp.createTempSync('parity-fs-');
  final root = temp.resolveSymbolicLinksSync();
  final mounts = <MountDir>[];
  try {
    for (var i = 0; i < specs.length; i++) {
      final spec = (specs[i] as Map).cast<String, Object?>();
      final host = Directory('$root/${c['id']}/${spec['host_index'] ?? i}')
        ..createSync(recursive: true);
      _seed(
        host.path,
        spec['virtual']! as String,
        (fs['files'] as Map?) ?? const {},
      );
      var hostPath = host.path;
      if (spec['host_missing'] == true) {
        hostPath = '${host.path}/missing';
      } else if (spec['host_noncanonical'] == true) {
        // A lexical normalizer cannot pass this fixture: core must resolve
        // an actual symlink as well as the trailing dot segment.
        final alias = Link('${host.path}-link')..createSync(host.path);
        hostPath = '${alias.path}/.';
      }
      try {
        mounts.add(constructMount(hostPath, spec));
        // Registration argument errors are measured; setup/internal errors
        // must escape to the runner-error boundary after cleanup.
        // ignore: avoid_catching_errors
      } on ArgumentError catch (error) {
        for (final mount in mounts) {
          mount.close();
        }
        temp.deleteSync(recursive: true);
        return MountFixture([], root: root, error: error);
      }
    }
    if (c['rename_host_before_feed'] == true) {
      for (final path in mounts.map((m) => m.hostPath).toSet()) {
        Directory(path).renameSync('$path-renamed');
      }
    }
    return MountFixture(
      mounts,
      root: root,
      cleanup: () => temp.deleteSync(recursive: true),
    );
  } on Object {
    for (final mount in mounts) {
      mount.close();
    }
    temp.deleteSync(recursive: true);
    rethrow;
  }
}

void _seed(String host, String virtual, Map<Object?, Object?> files) {
  // Normalize seed prefixes as the mount constructor does, without changing
  // the constructor argument whose validation the case is measuring.
  final parts = <String>[];
  for (final part in virtual.split('/')) {
    if (part == '..') {
      if (parts.isNotEmpty && parts.last != '..') {
        parts.removeLast();
      } else if (!virtual.startsWith('/')) {
        parts.add(part);
      }
    } else if (part.isNotEmpty && part != '.') {
      parts.add(part);
    }
  }
  final leading = virtual.startsWith('//') && !virtual.startsWith('///')
      ? '//'
      : virtual.startsWith('/')
      ? '/'
      : '';
  final normalized = '$leading${parts.join('/')}';
  final prefix = normalized.isEmpty ? '.' : normalized;
  final base = prefix.replaceFirst(RegExp(r'/+$'), '');
  for (final entry in files.entries) {
    final path = entry.key! as String;
    if (path != prefix && !path.startsWith('$base/')) continue;
    final rel = path.substring(base.length).replaceFirst(RegExp('^/+'), '');
    final target = '$host/$rel';
    if (rel.isEmpty || path.endsWith('/')) {
      Directory(target).createSync(recursive: true);
      continue;
    }
    final content = (entry.value! as Map).cast<String, Object?>();
    File(target).parent.createSync(recursive: true);
    if (content['symlink'] case final String link) {
      Link(target).createSync(link.replaceAll('{host}', host));
    } else if (content['hex'] case final String hex) {
      File(target).writeAsBytesSync([
        for (var i = 0; i < hex.length; i += 2)
          int.parse(hex.substring(i, i + 2), radix: 16),
      ]);
    } else {
      File(target).writeAsStringSync(content['text']! as String);
    }
  }
}
