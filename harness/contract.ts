/**
 * The contract the ported interpreter must satisfy.
 *
 * This file is written BEFORE the port exists and is the specification the agent
 * builds against. It is deliberately the narrowest interface that lets the
 * conformance suite run: everything here is required, nothing here is optional
 * decoration.
 *
 * Value mapping (Go -> TypeScript). The harness compares values structurally, so
 * the port must use these representations or the oracle cannot judge it:
 *
 *   Starlark      Go                      TypeScript
 *   --------      --------------------    ----------------------------
 *   None          starlark.None           null
 *   bool          starlark.Bool           boolean
 *   int           starlark.Int            bigint          (arbitrary precision)
 *   float         starlark.Float          number
 *   string        starlark.String         string
 *   bytes         starlark.Bytes          Uint8Array
 *   list          *starlark.List          StarlarkValue[]           (mutable)
 *   tuple         starlark.Tuple          Tuple                     (frozen wrapper)
 *   dict          *starlark.Dict          Map<StarlarkValue, StarlarkValue>
 *   set           *starlark.Set           Set<StarlarkValue>
 *   function      *starlark.Function      NativeFn | interpreter callable
 *   module/struct starlarkstruct.Module   NativeModule
 *
 * `int` is `bigint`, not `number`. Starlark integers are arbitrary precision, and
 * `int.star` exercises values well beyond 2^53. Using `number` will pass early
 * tests and fail late ones, which is the worst possible failure mode.
 */

/** Any value crossing the host/interpreter boundary. */
export type StarlarkValue = unknown;

/**
 * A builtin implemented in TypeScript.
 *
 * Positional and keyword arguments are passed separately rather than merged, because
 * Starlark distinguishes them and `assert.true(cond, msg="...")` depends on it.
 */
export type NativeFn = (
  args: StarlarkValue[],
  kwargs: Record<string, StarlarkValue>,
) => StarlarkValue;

/** A frozen sequence. Distinct from `list` because Starlark distinguishes them. */
export class Tuple {
  constructor(readonly items: readonly StarlarkValue[]) {}
}

/** A named namespace of members, as produced by Starlark's `module()` builtin. */
export interface NativeModule {
  readonly __starlarkModule__: string;
  readonly members: Record<string, StarlarkValue>;
}

export function isNativeModule(v: StarlarkValue): v is NativeModule {
  return typeof v === "object" && v !== null && "__starlarkModule__" in v;
}

/**
 * Per-execution state. Mirrors `*starlark.Thread` in the Go implementation.
 */
export interface Thread {
  /**
   * Resolves `load("module", ...)`, returning that module's globals.
   *
   * The harness intercepts `"assert.star"` and returns its native shim, so the
   * module system is not on the critical path to the first passing assertion.
   */
  load?: (module: string) => Record<string, StarlarkValue>;

  /** Receives output from the `print()` builtin. */
  print?: (msg: string) => void;
}

/**
 * Raised by the interpreter for Starlark-level errors (including `fail()`).
 *
 * `assert.fails` matches against `message`, so the port must preserve upstream
 * error text. Error strings are part of the observable behaviour, not incidental:
 * `quote.go`'s "non-ASCII hex escape ..." message is asserted by upstream tests.
 */
export interface StarlarkError extends Error {
  /** Optional call stack, rendered as in Go. */
  backtrace?: string;
}

/**
 * The single entry point the port must export from `ported/index.ts`.
 *
 * Mirrors `starlark.ExecFile(thread, filename, src, predeclared)`.
 *
 * @param filename Reported in error messages and tracebacks.
 * @param src      Starlark source text.
 * @param predeclared Bindings visible to the module before execution, used to
 *                    inject the native `assert` module and its supporting builtins.
 * @param thread   Execution state; supplies the `load` resolver.
 * @returns The module's global bindings after execution.
 * @throws StarlarkError on any Starlark-level failure (syntax, resolve, or runtime).
 */
export interface Interpreter {
  execFile(
    filename: string,
    src: string,
    predeclared: Record<string, StarlarkValue>,
    thread?: Thread,
  ): Record<string, StarlarkValue>;

  /**
   * Invokes an interpreter-level callable from the host.
   *
   * Required by `assert.fails` and `catch()`, which must call a Starlark function and
   * capture its error. Optional only because a port may represent Starlark functions as
   * plain JS functions, in which case the harness calls them directly.
   */
  call?(fn: StarlarkValue, args: StarlarkValue[]): StarlarkValue;
}
