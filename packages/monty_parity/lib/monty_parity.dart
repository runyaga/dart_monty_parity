/// Parity runner for dart_monty_core against upstream pydantic-monty.
///
/// Cases and the scorer live in tool/parity; this package is the Dart half:
/// it runs a case through core's public API (`Monty` / `MontySession`), or
/// through the raw worker, and returns the outcome.
library;

export 'src/canon.dart';
