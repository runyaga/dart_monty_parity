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
  test(
    'filesystem preserves scripted environment calls and arguments',
    () async {
      final calls = <String>[];
      const environment = MontyDict([
        (MontyString('A'), MontyString('1')),
      ]);
      final args = [const MontyString('HOME'), const MontyNone()];
      final kwargs = <String, MontyValue>{};
      final handler = buildFsHandler({}, (name, actualArgs, actualKwargs) {
        calls.add(name);
        expect(actualKwargs, same(kwargs));
        if (name == 'os.getenv') {
          expect(actualArgs, same(args));
          return const MontyString('/home/u');
        }
        expect(actualArgs, isEmpty);
        return environment;
      }, {});
      expect(
        await handler('os.getenv', args, kwargs),
        const MontyString('/home/u'),
      );
      expect(await handler('os.environ', [], kwargs), same(environment));
      expect(calls, ['os.getenv', 'os.environ']);
    },
  );
  test(
    'refmem retains the OSAccess environment and missing-key default',
    () async {
      final handler = buildFsHandler(
        {},
        _fallback,
        {},
        scriptedEnvironment: false,
      );
      expect(
        await handler('os.getenv', [
          const MontyString('HOME'),
          const MontyNone(),
        ], {}),
        const MontyNone(),
      );
      expect(
        await handler('os.getenv', [
          const MontyString('NOPE'),
          const MontyString('dflt'),
        ], {}),
        const MontyString('dflt'),
      );
      expect(await handler('os.environ', [], {}), const MontyDict([]));
    },
  );
  test('raw worker memory handler does not emulate host mounts', () {
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
  test(
    'runner StateError is never a host refusal, even for local_refusal',
    () async {
      final failure = StateError('runner invariant');
      final session = _RecordingSession()..failure = failure;
      await expectLater(
        runEngineCase(
          {'id': 'invariant', 'code': '1', 'local_refusal': true},
          'vm',
          engine: 'core',
          engineVersion: 'test',
          open: (_, _) async => session,
        ),
        throwsA(same(failure)),
      );
      expect(session.closed, isTrue);
    },
  );
  test(
    'raw worker reports per-feed dispatch controls as unsupported',
    () async {
      final result = await runEngineCase(
        {
          'id': 'controls',
          'feeds': [
            {
              'code': '1',
              'dispatch': 'manual',
              'expect_os_snapshot': 'x',
              'record_dispatch': true,
              'sleep_access': true,
              'mount_indices': [0],
            },
          ],
        },
        'vm',
        engine: 'worker',
        engineVersion: 'test',
        open: (_, _) async => _RecordingSession(),
      );
      expect(
        (result['unsupported']! as Map).keys,
        containsAll([
          'dispatch',
          'expect_os_snapshot',
          'record_dispatch',
          'sleep_access',
          'mount_indices',
        ]),
      );
    },
  );

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
      'mount_indices': [0],
      'feeds': [
        {
          'code': '1',
          'mount_indices': [0],
        },
      ],
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
    expect(memory.containsKey('mount_indices'), isFalse);
    expect(
      ((memory['feeds']! as List).single as Map).containsKey('mount_indices'),
      isFalse,
    );
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
  StateError? failure;

  @override
  Future<MontyValue> feed(
    String code, {
    required Map<String, MontyValue> inputs,
    required Map<String, ProtoExternalFunction> externals,
    required OsHandler onOsCall,
    required ProtoPrintCallback onPrint,
    String? cwd,
    Map<String, Object?> options = const {},
    List<Object?>? observations,
  }) async {
    directories.add(cwd);
    if (failure case final StateError error) throw error;

    return const MontyNone();
  }

  @override
  Future<void> restore() async {}

  @override
  Future<void> close() async {
    closed = true;
  }
}
