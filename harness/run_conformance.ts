/**
 * Conformance runner: the project's oracle.
 *
 * Runs two suites against the ported interpreter and emits a machine-readable report:
 *
 *   1. The tier-0 ladder (harness/ladder/probes.ts) — our own probes, fine-grained,
 *      measurable from the interpreter's first line. A PROGRESS signal only.
 *   2. The vendored upstream suite (vendor/starlark-testdata/*.star) — self-checking
 *      Starlark. The only thing that establishes CONFORMANCE.
 *
 * Runs cleanly with no implementation present, reporting zero and saying why, so the
 * harness can be validated before the port exists.
 *
 * Usage: node dist/harness/run_conformance.js [--json <path>]
 */

import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import type { Interpreter, StarlarkValue, Thread } from "./contract.js";
import { createAssertShim } from "./assert_shim.js";
import { PROBES, checkProbe } from "./ladder/probes.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "..", "..");
const TESTDATA = join(ROOT, "vendor", "starlark-testdata");

/** Files vendored for reference rather than execution. */
const NOT_A_TEST = new Set(["assert.star"]);

/** Upstream files whose dependencies are explicitly out of scope (see docs/spike.md). */
const DEFERRED = new Set(["proto.star", "benchmark.star"]);

interface FileResult {
  file: string;
  status: "pass" | "partial" | "fail" | "error" | "deferred";
  assertionsPassed: number;
  assertionsFailed: number;
  error?: string;
  failures: string[];
}

interface ProbeResult {
  tier: number;
  name: string;
  ok: boolean;
  detail?: string;
}

interface Report {
  generatedAt: string;
  implementationPresent: boolean;
  implementationNote?: string;
  ladder: {
    total: number;
    passed: number;
    rate: number;
    byTier: { tier: number; total: number; passed: number }[];
    failures: ProbeResult[];
  };
  conformance: {
    filesTotal: number;
    filesPassed: number;
    assertionsTotal: number;
    assertionsPassed: number;
    rate: number;
    files: FileResult[];
  };
}

/**
 * Loads the ported interpreter, tolerating its absence.
 *
 * `--impl <path>` overrides the default location, so a candidate port can be scored
 * before being promoted, and so the harness self-test can point at a known fake.
 */
async function loadInterpreter(implPath: string): Promise<{ interp: Interpreter | null; note?: string }> {
  try {
    // Absolute specifiers must be file:// URLs for Node's ESM loader.
    const mod = await import(pathToFileURL(implPath).href);
    const interp = (mod.default ?? mod) as Interpreter;
    if (typeof interp?.execFile !== "function") {
      return { interp: null, note: `${implPath} does not export execFile (see harness/contract.ts)` };
    }
    return { interp };
  } catch (e) {
    return { interp: null, note: `no implementation at ${implPath}: ${(e as Error).message.split("\n")[0]}` };
  }
}

/** Reads a `--flag value` pair from argv, if present. */
function argValue(flag: string): string | undefined {
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

function runLadder(interp: Interpreter | null): Report["ladder"] {
  const results: ProbeResult[] = [];

  for (const probe of PROBES) {
    if (!interp) {
      results.push({ tier: probe.tier, name: probe.name, ok: false, detail: "no implementation" });
      continue;
    }
    const shim = createAssertShim((fn, args) =>
      interp.call ? interp.call(fn, args) : (fn as (...a: StarlarkValue[]) => StarlarkValue)(...args),
    );
    const predeclared: Record<string, StarlarkValue> = {
      ...shim.predeclared,
      _probe_double: (args: StarlarkValue[]) => (args[0] as bigint) * 2n,
    };

    // Negative probes run their positive control first; without it, an interpreter that
    // throws on everything would "pass" every error-expecting probe.
    let controlThrew: Error | null = null;
    if (probe.control !== undefined) {
      try {
        interp.execFile(`probe:${probe.name}:control`, probe.control, predeclared);
      } catch (e) {
        controlThrew = e as Error;
      }
    }

    let globals: Record<string, StarlarkValue> | null = null;
    let thrown: Error | null = null;
    try {
      globals = interp.execFile(`probe:${probe.name}`, probe.src, predeclared);
    } catch (e) {
      thrown = e as Error;
    }
    const { ok, detail } = checkProbe(probe, globals, thrown, controlThrew);
    results.push({ tier: probe.tier, name: probe.name, ok, detail });
  }

  const tiers = [...new Set(PROBES.map((p) => p.tier))].sort((a, b) => a - b);
  return {
    total: results.length,
    passed: results.filter((r) => r.ok).length,
    rate: results.length ? results.filter((r) => r.ok).length / results.length : 0,
    byTier: tiers.map((tier) => ({
      tier,
      total: results.filter((r) => r.tier === tier).length,
      passed: results.filter((r) => r.tier === tier && r.ok).length,
    })),
    failures: results.filter((r) => !r.ok),
  };
}

function runConformance(interp: Interpreter | null): Report["conformance"] {
  const files = readdirSync(TESTDATA)
    .filter((f) => f.endsWith(".star") && !NOT_A_TEST.has(f))
    .sort();

  const results: FileResult[] = [];

  for (const file of files) {
    if (DEFERRED.has(file)) {
      results.push({ file, status: "deferred", assertionsPassed: 0, assertionsFailed: 0, failures: [] });
      continue;
    }
    if (!interp) {
      results.push({
        file,
        status: "error",
        assertionsPassed: 0,
        assertionsFailed: 0,
        error: "no implementation",
        failures: [],
      });
      continue;
    }

    const src = readFileSync(join(TESTDATA, file), "utf8");
    const shim = createAssertShim((fn, args) =>
      interp.call ? interp.call(fn, args) : (fn as (...a: StarlarkValue[]) => StarlarkValue)(...args),
    );

    // Intercept load("assert.star", ...) with the native shim, so the module system is
    // not a prerequisite for the first passing assertion.
    const thread: Thread = {
      load: (module: string) => {
        if (module === "assert.star") return shim.predeclared;
        throw new Error(`unsupported load(${JSON.stringify(module)})`);
      },
      print: () => {},
    };

    let error: string | undefined;
    try {
      interp.execFile(file, src, shim.predeclared, thread);
    } catch (e) {
      error = (e as Error).message?.split("\n")[0] ?? String(e);
    }

    const { passed, failed, failures } = shim.stats;
    const status: FileResult["status"] =
      error && passed === 0 ? "error" : failed === 0 && !error && passed > 0 ? "pass" : passed > 0 ? "partial" : "fail";

    results.push({ file, status, assertionsPassed: passed, assertionsFailed: failed, error, failures });
  }

  const assertionsPassed = results.reduce((n, r) => n + r.assertionsPassed, 0);
  const assertionsSeen = results.reduce((n, r) => n + r.assertionsPassed + r.assertionsFailed, 0);
  return {
    filesTotal: results.filter((r) => r.status !== "deferred").length,
    filesPassed: results.filter((r) => r.status === "pass").length,
    assertionsTotal: assertionsSeen,
    assertionsPassed,
    rate: assertionsSeen ? assertionsPassed / assertionsSeen : 0,
    files: results,
  };
}

async function main(): Promise<void> {
  // The port is authored as ported/*.ts and compiled to dist/ported/*.js, so the default
  // points at the build output rather than the source tree.
  const implPath = argValue("--impl") ?? join(ROOT, "dist", "ported", "index.js");
  const { interp, note } = await loadInterpreter(implPath);

  const report: Report = {
    generatedAt: new Date().toISOString(),
    implementationPresent: interp !== null,
    implementationNote: note,
    ladder: runLadder(interp),
    conformance: runConformance(interp),
  };

  const outPath = argValue("--json") ?? join(ROOT, "reports", "conformance.json");
  mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, `${JSON.stringify(report, null, 2)}\n`);

  const { ladder, conformance } = report;
  console.log(`implementation : ${report.implementationPresent ? "present" : `ABSENT — ${note}`}`);
  console.log(`ladder         : ${ladder.passed}/${ladder.total} probes (${(ladder.rate * 100).toFixed(1)}%)`);
  for (const t of ladder.byTier) {
    console.log(`   tier ${t.tier}      : ${t.passed}/${t.total}`);
  }
  console.log(
    `conformance    : ${conformance.assertionsPassed}/${conformance.assertionsTotal} assertions ` +
      `(${(conformance.rate * 100).toFixed(1)}%), ${conformance.filesPassed}/${conformance.filesTotal} files clean`,
  );
  console.log(`report         : ${outPath}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
