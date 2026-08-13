/**
 * Proves the harness measures what it claims to.
 *
 * A conformance harness that always reports 0% is indistinguishable from a correct one
 * with nothing to measure -- and one that always reports success is worse. This runs the
 * real runner against `fake_interpreter.ts`, whose capabilities are known exactly, and
 * asserts the score matches.
 *
 * If this fails, the oracle is broken and every number the project reports is suspect.
 */

import { execFileSync } from "node:child_process";
import { readFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const DIST = resolve(HERE, "..");
const ROOT = resolve(DIST, "..", "..");

interface Expectation {
  label: string;
  actual: number;
  expected: number;
}

function main(): void {
  const out = join(mkdtempSync(join(tmpdir(), "starport-selftest-")), "report.json");

  execFileSync(
    process.execPath,
    [
      join(DIST, "run_conformance.js"),
      "--impl",
      join(DIST, "selftest", "fake_interpreter.js"),
      "--json",
      out,
    ],
    { cwd: ROOT, stdio: "pipe" },
  );

  const report = JSON.parse(readFileSync(out, "utf8"));
  const tier = (n: number): { total: number; passed: number } =>
    report.ladder.byTier.find((t: { tier: number }) => t.tier === n) ?? { total: 0, passed: 0 };

  // The fake handles literal assignment only, so tier 1 must pass in full and every
  // higher tier must score zero. Both directions matter: an over-counting harness is as
  // broken as an under-counting one.
  const checks: Expectation[] = [
    { label: "implementation detected", actual: report.implementationPresent ? 1 : 0, expected: 1 },
    { label: "tier 1 passed", actual: tier(1).passed, expected: tier(1).total },
    { label: "tier 2 passed", actual: tier(2).passed, expected: 0 },
    { label: "tier 3 passed", actual: tier(3).passed, expected: 0 },
    { label: "tier 4 passed", actual: tier(4).passed, expected: 0 },
    { label: "tier 5 passed", actual: tier(5).passed, expected: 0 },
    { label: "tier 6 passed", actual: tier(6).passed, expected: 0 },
    // The fake cannot execute a single conformance file, so no assertion should be counted.
    { label: "conformance assertions seen", actual: report.conformance.assertionsTotal, expected: 0 },
  ];

  const failed = checks.filter((c) => c.actual !== c.expected);
  for (const c of checks) {
    const mark = c.actual === c.expected ? "ok  " : "FAIL";
    console.log(`  ${mark} ${c.label}: ${c.actual} (expected ${c.expected})`);
  }

  if (failed.length > 0) {
    console.error(`\nharness self-test FAILED (${failed.length} mismatch(es)) -- the oracle is unreliable`);
    process.exit(1);
  }
  console.log(`\nharness self-test passed: ${tier(1).passed}/${report.ladder.total} probes, as designed`);
}

main();
