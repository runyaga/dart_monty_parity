import 'dart:typed_data';

import 'package:dart_monty_core/dart_monty_core.dart';
import 'package:dart_monty_core/src/worker/proto_session.dart';
import 'package:monty_parity/src/case_runner.dart';
import 'package:monty_parity/src/session_runner.dart';
import 'package:monty_parity/src/worker_runner.dart';
import 'package:test/test.dart';

void main() {
  _memoryTests();
  _mountTests();
  _caseTests();
}

void _memoryTests() {
  test(
    'memory handler reads text and bytes without mount permissions',
    () async {
      final unsupported = <String, Object?>{};
      final handler = buildFsHandler(
        {
          'files': {
            '/data/a': {'text': 'hello'},
            '/data/b': {'hex': '00ff'},
          },
        },
        _fallback,
        unsupported,
      );
      expect(
        await handler('Path.read_text', [MontyPath('/data/a')], {}),
        const MontyString('hello'),
      );
      final bytes = await handler('Path.read_bytes', [
        MontyPath('/data/b'),
      ], {});
      expect(bytes, isA<MontyBytes>());
      expect((bytes as MontyBytes).value, Uint8List.fromList([0, 255]));
      expect(unsupported, isEmpty);
    },
  );
}

void _mountTests() {
  test('mount requests stay unsupported until P-F2', () {
    final unsupported = <String, Object?>{};
    buildFsHandler(
      {
        'mounts': [
          {'virtual': '/data', 'mode': 'overlay'},
        ],
      },
      _fallback,
      unsupported,
    );
    expect(unsupported.keys, contains('fs.mounts'));
    expect(unsupported.keys, isNot(contains('fs.mode')));
  });
  test('memory handler falls back only for unhandled calls', () async {
    var calls = 0;
    MontyValue fallback(
      String name,
      List<MontyValue> args,
      Map<String, MontyValue> kwargs,
    ) {
      calls++;
      return const MontyString('fallback');
    }

    final handler = buildFsHandler({}, fallback, {});
    expect(
      await handler('unknown.call', [], {}),
      const MontyString('fallback'),
    );
    expect(calls, 1);
    await expectLater(
      handler('Path.read_text', [MontyPath('/missing')], {}),
      throwsA(isA<ExternalException>()),
    );
    expect(calls, 1);
  });
}

void _caseTests() {
  test('case cwd is forwarded on every feed without unsupported cwd', () async {
    final session = _RecordingSession();
    final result = await runEngineCase(
      {
        'id': 'cwd',
        'feeds': ['first', 'second'],
        'cwd': '/work',
      },
      'vm',
      engine: 'core',
      engineVersion: 'test',
      open: (_, _) async => session,
    );
    expect(session.directories, ['/work', '/work']);
    expect(result['unsupported'], isEmpty);
    expect(session.closed, isTrue);
  });
  test('refmem removes mount semantics without changing the original case', () {
    final original = <String, Object?>{
      'id': 'sample',
      'refmem': true,
      'fs': {
        'mounts': [
          {'virtual': '/data', 'mode': 'read-only'},
        ],
        'files': {
          '/data/a': {'text': 'hello'},
        },
      },
    };
    final memory = refmemCase(original);
    expect((memory['fs']! as Map).containsKey('mounts'), isFalse);
    expect((original['fs']! as Map).containsKey('mounts'), isTrue);
    expect((memory['fs']! as Map)['files'], (original['fs']! as Map)['files']);
  });
}

MontyValue _fallback(
  String name,
  List<MontyValue> args,
  Map<String, MontyValue> kwargs,
) => throw StateError('unexpected fallback: $name');

final class _RecordingSession implements CaseSession {
  final directories = <String?>[];
  bool closed = false;

  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
    String? cwd,
  }) async {
    directories.add(cwd);

    return const MontyNone();
  }

  @override
  Future<void> restore() async {}

  @override
  Future<void> close() async {
    closed = true;
  }
}
