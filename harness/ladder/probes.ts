/**
 * Tier-0 conformance ladder.
 *
 * The real `.star` files need most of the interpreter before their first assertion can
 * run (docs/spike.md Q1). These probes bridge that gap: each is a few lines of Starlark
 * with a mechanically checkable outcome, ordered so that progress is measurable from the
 * moment the lexer works.
 *
 * They are OUR tests, not upstream's, so they are a progress signal — never a correctness
 * claim. Only `vendor/starlark-testdata/*.star` can establish conformance. The Phase 0
 * spike is the cautionary tale: a hand-written invariant was wrong where the generated
 * code was right.
 */

import { Tuple, type StarlarkValue } from "../contract.js";
import { starlarkEquals } from "../assert_shim.js";

export interface Probe {
  /** Ordering key: lower tiers must work before higher ones can. */
  tier: number;
  name: string;
  src: string;
  /** Expected global bindings after execution. */
  expect?: Record<string, StarlarkValue>;
  /** If set, execution must throw and the message must match. */
  expectError?: RegExp;
  /**
   * Positive control for a negative probe. Required whenever `expectError` is set.
   *
   * A negative test with no positive control passes for the wrong reason: an interpreter
   * that cannot parse anything throws on every input and therefore "passes" every
   * error-expecting probe. The control is a structurally similar program that MUST run
   * cleanly, proving the interpreter is capable enough for the negative result to mean
   * something. Caught by the harness self-test, which scored a literal-only fake 3/7 on
   * tier 6 before this existed.
   */
  control?: string;
}

export const PROBES: Probe[] = [
  // --- tier 1: lexer + parser + assignment ---
  { tier: 1, name: "empty program", src: "" },
  { tier: 1, name: "comment only", src: "# nothing here\n" },
  { tier: 1, name: "int literal", src: "x = 1", expect: { x: 1n } },
  { tier: 1, name: "negative int", src: "x = -42", expect: { x: -42n } },
  { tier: 1, name: "string literal", src: 'x = "hi"', expect: { x: "hi" } },
  { tier: 1, name: "bool literals", src: "a = True\nb = False", expect: { a: true, b: false } },
  { tier: 1, name: "none literal", src: "x = None", expect: { x: null } },
  { tier: 1, name: "multiple bindings", src: "a = 1\nb = 2\nc = 3", expect: { a: 1n, b: 2n, c: 3n } },

  // --- tier 2: arithmetic and comparison ---
  { tier: 2, name: "addition", src: "x = 1 + 2", expect: { x: 3n } },
  { tier: 2, name: "operator precedence", src: "x = 2 + 3 * 4", expect: { x: 14n } },
  { tier: 2, name: "parenthesised", src: "x = (2 + 3) * 4", expect: { x: 20n } },
  { tier: 2, name: "floor division", src: "x = 7 // 2", expect: { x: 3n } },
  { tier: 2, name: "modulo", src: "x = 7 % 3", expect: { x: 1n } },
  { tier: 2, name: "bigint beyond 2^53", src: "x = 9007199254740993 + 1", expect: { x: 9007199254740994n } },
  { tier: 2, name: "comparison", src: "x = 1 < 2\ny = 2 < 1", expect: { x: true, y: false } },
  { tier: 2, name: "equality", src: 'x = 1 == 1\ny = "a" == "b"', expect: { x: true, y: false } },
  { tier: 2, name: "boolean ops", src: "x = True and False\ny = True or False", expect: { x: false, y: true } },
  { tier: 2, name: "not", src: "x = not True", expect: { x: false } },

  // --- tier 3: collections ---
  { tier: 3, name: "list literal", src: "x = [1, 2, 3]", expect: { x: [1n, 2n, 3n] } },
  { tier: 3, name: "empty list", src: "x = []", expect: { x: [] } },
  { tier: 3, name: "list index", src: "x = [10, 20, 30][1]", expect: { x: 20n } },
  { tier: 3, name: "tuple literal", src: "x = (1, 2)", expect: { x: new Tuple([1n, 2n]) } },
  { tier: 3, name: "tuple is not list", src: "x = (1, 2) == [1, 2]", expect: { x: false } },
  { tier: 3, name: "dict literal", src: 'x = {"a": 1}', expect: { x: new Map([["a", 1n]]) } },
  { tier: 3, name: "dict index", src: 'x = {"a": 1, "b": 2}["b"]', expect: { x: 2n } },
  { tier: 3, name: "string index", src: 'x = "hello"[1]', expect: { x: "e" } },
  { tier: 3, name: "list slice", src: "x = [1, 2, 3, 4][1:3]", expect: { x: [2n, 3n] } },
  { tier: 3, name: "in operator", src: "x = 2 in [1, 2, 3]", expect: { x: true } },
  { tier: 3, name: "len builtin", src: "x = len([1, 2, 3])", expect: { x: 3n } },

  // --- tier 4: control flow ---
  { tier: 4, name: "if true branch", src: "if True:\n  x = 1\nelse:\n  x = 2", expect: { x: 1n } },
  { tier: 4, name: "if false branch", src: "if False:\n  x = 1\nelse:\n  x = 2", expect: { x: 2n } },
  { tier: 4, name: "elif chain", src: "x = 0\nif False:\n  x = 1\nelif True:\n  x = 2", expect: { x: 2n } },
  { tier: 4, name: "for loop accumulate", src: "x = 0\nfor i in [1, 2, 3]:\n  x = x + i", expect: { x: 6n } },
  { tier: 4, name: "for over range", src: "x = 0\nfor i in range(4):\n  x = x + i", expect: { x: 6n } },
  { tier: 4, name: "break", src: "x = 0\nfor i in [1, 2, 3]:\n  if i == 2:\n    break\n  x = x + i", expect: { x: 1n } },
  { tier: 4, name: "continue", src: "x = 0\nfor i in [1, 2, 3]:\n  if i == 2:\n    continue\n  x = x + i", expect: { x: 4n } },

  // --- tier 5: functions ---
  { tier: 5, name: "def and call", src: "def f():\n  return 1\nx = f()", expect: { x: 1n } },
  { tier: 5, name: "positional args", src: "def add(a, b):\n  return a + b\nx = add(1, 2)", expect: { x: 3n } },
  { tier: 5, name: "default args", src: "def f(a, b = 10):\n  return a + b\nx = f(1)", expect: { x: 11n } },
  { tier: 5, name: "keyword args", src: "def f(a, b):\n  return a - b\nx = f(b = 1, a = 5)", expect: { x: 4n } },
  { tier: 5, name: "recursion", src: "def fac(n):\n  if n <= 1:\n    return 1\n  return n * fac(n - 1)\nx = fac(5)", expect: { x: 120n } },
  { tier: 5, name: "closure over global", src: "g = 7\ndef f():\n  return g\nx = f()", expect: { x: 7n } },
  { tier: 5, name: "lambda", src: "f = lambda a: a * 2\nx = f(21)", expect: { x: 42n } },
  { tier: 5, name: "list comprehension", src: "x = [i * 2 for i in [1, 2, 3]]", expect: { x: [2n, 4n, 6n] } },

  // --- tier 6: host interop and errors (what the assert shim depends on) ---
  { tier: 6, name: "call predeclared native fn", src: "x = _probe_double(21)", expect: { x: 42n } },
  { tier: 6, name: "native module member access", src: "x = assert.eq", expect: undefined },
  {
    tier: 6,
    name: "fail() halts",
    src: 'fail("boom")',
    expectError: /boom/,
    control: "def f():\n  return 1\nx = f()",
  },
  {
    tier: 6,
    name: "undefined name errors",
    src: "x = nosuchname",
    expectError: /nosuchname|undefined/i,
    control: "nosuchname = 1\nx = nosuchname",
  },
  {
    tier: 6,
    name: "type error on bad add",
    src: 'x = 1 + "a"',
    expectError: /.+/,
    control: "x = 1 + 2",
  },
  { tier: 6, name: "string % formatting", src: 'x = "%d-%s" % (1, "a")', expect: { x: "1-a" } },
  { tier: 6, name: "type builtin", src: 'x = type(1)\ny = type("s")', expect: { x: "int", y: "string" } },
];

/**
 * Compares actual globals against a probe's expectation.
 *
 * @param controlThrew For negative probes, whether the positive control failed. If it did,
 *                     the probe fails regardless of the negative result -- the interpreter
 *                     is not capable enough for the negative outcome to carry information.
 */
export function checkProbe(
  probe: Probe,
  globals: Record<string, StarlarkValue> | null,
  thrown: Error | null,
  controlThrew?: Error | null,
): { ok: boolean; detail?: string } {
  if (probe.expectError) {
    if (controlThrew) {
      return { ok: false, detail: `positive control failed (${controlThrew.message}) — negative result is meaningless` };
    }
    if (!thrown) return { ok: false, detail: "expected an error, none thrown" };
    return probe.expectError.test(thrown.message)
      ? { ok: true }
      : { ok: false, detail: `error ${JSON.stringify(thrown.message)} did not match ${probe.expectError}` };
  }
  if (thrown) return { ok: false, detail: `threw: ${thrown.message}` };
  if (!globals) return { ok: false, detail: "no globals returned" };
  for (const [key, want] of Object.entries(probe.expect ?? {})) {
    const got = globals[key];
    if (!starlarkEquals(got, want)) {
      return { ok: false, detail: `${key}: expected ${String(want)}, got ${String(got)}` };
    }
  }
  return { ok: true };
}
