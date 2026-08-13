/**
 * A deliberately incompetent Starlark "interpreter", used only to test the harness.
 *
 * It understands exactly one construct -- assigning a literal to a name -- and throws on
 * everything else. That makes its score against the ladder exactly predictable, which is
 * what lets `run_selftest.ts` prove the harness measures real progress rather than always
 * reporting zero (or always reporting success).
 *
 * This is NOT a starting point for the port. It is a measuring stick for the oracle.
 */

import type { StarlarkValue, Thread } from "../contract.js";

const ASSIGN = /^([A-Za-z_]\w*)\s*=\s*(.+?)\s*$/;

function parseLiteral(text: string): StarlarkValue {
  if (text === "True") return true;
  if (text === "False") return false;
  if (text === "None") return null;
  if (/^-?\d+$/.test(text)) return BigInt(text);
  const quoted = /^"([^"\\]*)"$/.exec(text) ?? /^'([^'\\]*)'$/.exec(text);
  if (quoted) return quoted[1] ?? "";
  throw new Error(`fake interpreter: unsupported expression ${JSON.stringify(text)}`);
}

export function execFile(
  _filename: string,
  src: string,
  _predeclared: Record<string, StarlarkValue>,
  _thread?: Thread,
): Record<string, StarlarkValue> {
  const globals: Record<string, StarlarkValue> = {};

  for (const raw of src.split("\n")) {
    const line = raw.replace(/#.*$/, "").trim();
    if (line === "") continue;

    const m = ASSIGN.exec(line);
    if (!m) throw new Error(`fake interpreter: cannot parse ${JSON.stringify(line)}`);
    globals[m[1] as string] = parseLiteral(m[2] as string);
  }

  return globals;
}

export default { execFile };
