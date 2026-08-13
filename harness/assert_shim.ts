/**
 * Native TypeScript implementation of Starlark's `assert` module.
 *
 * Upstream, `assert` is defined in Starlark itself (`starlarktest/assert.star`) on top of
 * six host builtins. Reimplementing it natively removes `load()`, `def`, `%`-formatting,
 * tuples and the module system from the critical path, so the FIRST passing assertion no
 * longer requires most of the interpreter. See docs/spike.md Q1.
 *
 * The crucial semantic, quoted from upstream `assert.star`:
 *
 *   error(msg): report an error in Go's test framework WITHOUT halting execution.
 *   This is distinct from the built-in fail function, which halts execution.
 *
 * Non-halting `error` is what makes the conformance metric continuous: a file that fails
 * its 30th assertion still reports the other 300 as passing.
 */

import {
  type NativeFn,
  type NativeModule,
  type StarlarkValue,
  Tuple,
  isNativeModule,
} from "./contract.js";

export interface AssertionStats {
  passed: number;
  failed: number;
  /** Human-readable failure messages, in order, capped by the runner. */
  failures: string[];
}

// --- value formatting -------------------------------------------------------

/** Renders a value as Starlark `repr` would. Used in assertion failure messages. */
export function starlarkRepr(v: StarlarkValue): string {
  if (v === null || v === undefined) return "None";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "bigint") return v.toString();
  if (typeof v === "number") {
    if (Number.isInteger(v) && Number.isFinite(v)) return `${v}.0`;
    return String(v);
  }
  if (typeof v === "string") return JSON.stringify(v);
  if (v instanceof Uint8Array) {
    return `b${JSON.stringify(String.fromCharCode(...v))}`;
  }
  if (v instanceof Tuple) {
    const inner = v.items.map(starlarkRepr).join(", ");
    return v.items.length === 1 ? `(${inner},)` : `(${inner})`;
  }
  if (Array.isArray(v)) return `[${v.map(starlarkRepr).join(", ")}]`;
  if (v instanceof Map) {
    const parts = [...v.entries()].map(
      ([k, val]) => `${starlarkRepr(k)}: ${starlarkRepr(val)}`,
    );
    return `{${parts.join(", ")}}`;
  }
  if (v instanceof Set) return `set([${[...v].map(starlarkRepr).join(", ")}])`;
  if (isNativeModule(v)) return `<module ${v.__starlarkModule__}>`;
  if (typeof v === "function") return "<built-in function>";
  return String(v);
}

// --- value comparison -------------------------------------------------------

/** True if x and y are the same float within one unit in the last place. */
export function floatEq(x: number, y: number): boolean {
  if (Number.isNaN(x) && Number.isNaN(y)) return true;
  if (x === y) return true;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  const buf = new DataView(new ArrayBuffer(8));
  buf.setFloat64(0, x);
  const bx = buf.getBigInt64(0);
  buf.setFloat64(0, y);
  const by = buf.getBigInt64(0);
  const diff = bx > by ? bx - by : by - bx;
  return diff <= 1n;
}

/**
 * Structural equality over the contract's value mapping.
 *
 * Starlark compares int and float numerically (`1 == 1.0` is True), so bigint/number
 * comparison is cross-type rather than strict.
 */
export function starlarkEquals(a: StarlarkValue, b: StarlarkValue): boolean {
  if (a === b) return true;
  if (a === null || b === null) return a === b;

  // Numeric tower: int (bigint) and float (number) compare by value.
  const aNum = typeof a === "bigint" || typeof a === "number";
  const bNum = typeof b === "bigint" || typeof b === "number";
  if (aNum && bNum) {
    if (typeof a === "bigint" && typeof b === "bigint") return a === b;
    return Number(a) === Number(b);
  }

  if (typeof a !== typeof b && !(a instanceof Object && b instanceof Object)) {
    return false;
  }

  if (a instanceof Uint8Array && b instanceof Uint8Array) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
  }

  const aItems = a instanceof Tuple ? a.items : Array.isArray(a) ? a : null;
  const bItems = b instanceof Tuple ? b.items : Array.isArray(b) ? b : null;
  if (aItems && bItems) {
    // A tuple never equals a list, matching Starlark.
    if (a instanceof Tuple !== b instanceof Tuple) return false;
    return (
      aItems.length === bItems.length &&
      aItems.every((v, i) => starlarkEquals(v, bItems[i]))
    );
  }

  if (a instanceof Map && b instanceof Map) {
    if (a.size !== b.size) return false;
    for (const [k, v] of a) {
      let found = false;
      for (const [k2, v2] of b) {
        if (starlarkEquals(k, k2)) {
          if (!starlarkEquals(v, v2)) return false;
          found = true;
          break;
        }
      }
      if (!found) return false;
    }
    return true;
  }

  if (a instanceof Set && b instanceof Set) {
    if (a.size !== b.size) return false;
    for (const v of a) {
      if (![...b].some((w) => starlarkEquals(v, w))) return false;
    }
    return true;
  }

  return false;
}

/** Starlark `<` for the subset `assert.lt` needs. */
function starlarkLess(x: StarlarkValue, y: StarlarkValue): boolean {
  if (typeof x === "bigint" && typeof y === "bigint") return x < y;
  if (
    (typeof x === "bigint" || typeof x === "number") &&
    (typeof y === "bigint" || typeof y === "number")
  ) {
    return Number(x) < Number(y);
  }
  if (typeof x === "string" && typeof y === "string") return x < y;
  throw new Error(`unsupported comparison: ${starlarkRepr(x)} < ${starlarkRepr(y)}`);
}

/** Starlark `in` for the subset `assert.contains` needs. */
function starlarkContains(container: StarlarkValue, item: StarlarkValue): boolean {
  if (typeof container === "string") {
    return typeof item === "string" && container.includes(item);
  }
  if (container instanceof Tuple) return container.items.some((v) => starlarkEquals(v, item));
  if (Array.isArray(container)) return container.some((v) => starlarkEquals(v, item));
  if (container instanceof Set) return [...container].some((v) => starlarkEquals(v, item));
  if (container instanceof Map) return [...container.keys()].some((k) => starlarkEquals(k, item));
  throw new Error(`unsupported 'in' operand: ${starlarkRepr(container)}`);
}

// --- the shim ---------------------------------------------------------------

export interface AssertShim {
  /** Bindings to pass as `predeclared`, plus the `assert` module itself. */
  predeclared: Record<string, StarlarkValue>;
  stats: AssertionStats;
}

/**
 * Builds a fresh `assert` module and its supporting builtins.
 *
 * One shim per file execution, so assertion counts are per-file.
 *
 * @param callFn Invokes an interpreter-level callable. Supplied by the runner because only
 *               the port knows how to call its own function objects; `assert.fails` and
 *               `catch()` need it.
 */
export function createAssertShim(
  callFn: (fn: StarlarkValue, args: StarlarkValue[]) => StarlarkValue,
): AssertShim {
  const stats: AssertionStats = { passed: 0, failed: 0, failures: [] };

  const fail = (msg: string): void => {
    stats.failed++;
    if (stats.failures.length < 50) stats.failures.push(msg);
  };
  const ok = (): void => {
    stats.passed++;
  };

  // --- host builtins that assert.star documents as predeclared ---

  const error: NativeFn = (args) => {
    fail(args.map((a) => (typeof a === "string" ? a : starlarkRepr(a))).join(" "));
    return null; // non-halting, by design
  };

  const catchFn: NativeFn = (args) => {
    try {
      callFn(args[0], []);
      return null;
    } catch (e) {
      return (e as Error).message ?? String(e);
    }
  };

  const matches: NativeFn = (args) => {
    const [pattern, str] = args as [string, string];
    try {
      return new RegExp(pattern).test(str);
    } catch {
      return false;
    }
  };

  const moduleFn: NativeFn = (args, kwargs) => {
    const name = typeof args[0] === "string" ? args[0] : "module";
    return { __starlarkModule__: name, members: { ...kwargs } } satisfies NativeModule;
  };

  const freeze: NativeFn = (args) => args[0];

  const floatEqFn: NativeFn = (args) => floatEq(Number(args[0]), Number(args[1]));

  // --- the assert module, mirroring starlarktest/assert.star ---

  const eq: NativeFn = (args) => {
    const [x, y] = args;
    if (starlarkEquals(x, y)) ok();
    else fail(`${starlarkRepr(x)} != ${starlarkRepr(y)}`);
    return null;
  };

  const ne: NativeFn = (args) => {
    const [x, y] = args;
    if (!starlarkEquals(x, y)) ok();
    else fail(`${starlarkRepr(x)} == ${starlarkRepr(y)}`);
    return null;
  };

  const isTrue: NativeFn = (args, kwargs) => {
    const cond = args[0];
    const msg = (args[1] ?? kwargs["msg"] ?? "assertion failed") as string;
    // Starlark truthiness: None, False, 0, "", and empty containers are falsy.
    const truthy =
      cond !== null &&
      cond !== false &&
      cond !== 0 &&
      cond !== 0n &&
      cond !== "" &&
      !(Array.isArray(cond) && cond.length === 0) &&
      !(cond instanceof Tuple && cond.items.length === 0) &&
      !(cond instanceof Map && cond.size === 0) &&
      !(cond instanceof Set && cond.size === 0);
    if (truthy) ok();
    else fail(String(msg));
    return null;
  };

  const lt: NativeFn = (args) => {
    const [x, y] = args;
    try {
      if (starlarkLess(x, y)) ok();
      else fail(`${starlarkRepr(x)} is not less than ${starlarkRepr(y)}`);
    } catch (e) {
      fail((e as Error).message);
    }
    return null;
  };

  const contains: NativeFn = (args) => {
    const [x, y] = args;
    try {
      if (starlarkContains(x, y)) ok();
      else fail(`${starlarkRepr(x)} does not contain ${starlarkRepr(y)}`);
    } catch (e) {
      fail((e as Error).message);
    }
    return null;
  };

  const fails: NativeFn = (args) => {
    const [fn, pattern] = args as [StarlarkValue, string];
    let msg: string | null = null;
    try {
      callFn(fn, []);
    } catch (e) {
      msg = (e as Error).message ?? String(e);
    }
    if (msg === null) {
      fail(`evaluation succeeded unexpectedly (want error matching ${starlarkRepr(pattern)})`);
    } else if (!new RegExp(pattern).test(msg)) {
      fail(`regular expression (${pattern}) did not match error (${msg})`);
    } else {
      ok();
    }
    return null;
  };

  const assertModule: NativeModule = {
    __starlarkModule__: "assert",
    members: { fail: error, eq, ne, true: isTrue, lt, contains, fails },
  };

  return {
    stats,
    predeclared: {
      assert: assertModule,
      error,
      catch: catchFn,
      matches,
      module: moduleFn,
      _freeze: freeze,
      freeze,
      _floateq: floatEqFn,
    },
  };
}
