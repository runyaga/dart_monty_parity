@TestOn('vm')
library;

import 'dart:io';

import 'package:monty_parity/src/mount_fixture.dart';
import 'package:test/test.dart';

void main() {
  test('relative seed prefixes do not select absolute files', () {
    final fixture = buildMountFixture({
      'id': 'relative_seed',
      'fs': {
        'mounts': [
          {'virtual': 'data'},
        ],
        // Not under the relative prefix: this entry must not be decoded.
        'files': {'/data/a': <String, Object?>{}},
      },
    });
    addTearDown(fixture.close);
    expect(fixture.error, isA<ArgumentError>());
    expect('${fixture.error}', contains('virtual'));
  });

  test('canonical host case traverses a real symlink', () {
    final fixture = buildMountFixture({
      'id': 'canonical',
      'fs': {
        'mounts': [
          {'virtual': '/data', 'host_noncanonical': true},
        ],
      },
    });
    addTearDown(fixture.close);
    expect(fixture.error, isNull);
    final alias = Link('${fixture.root}/canonical/0-link');
    expect(alias.existsSync(), isTrue);
    expect(fixture.mounts.single.hostPath, alias.resolveSymbolicLinksSync());
    expect(fixture.mounts.single.hostPath, isNot(contains('-link')));
  });
  test('an entry equal to the mount root seeds a directory', () {
    final fixture = buildMountFixture({
      'id': 'root_entry',
      'fs': {
        'mounts': [
          {'virtual': '/data'},
        ],
        'files': {
          '/data': <String, Object?>{},
          '/data/a': {'text': 'A'},
        },
      },
    });
    addTearDown(fixture.close);
    expect(fixture.error, isNull);
    expect(File('${fixture.mounts.single.hostPath}/a').readAsStringSync(), 'A');
  });
}
