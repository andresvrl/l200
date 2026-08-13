"""Turns a conformance run into a prioritised work queue.

The agent does not choose what to port next -- the gap report does. Failing probes and
unearned assertions ARE the backlog, which keeps the loop grounded in measured state
rather than the model's opinion about what matters.

Usage:
    python3 harness/report.py                 # human summary
    python3 harness/report.py --json          # machine-readable gap report
    python3 harness/report.py --check-regression reports/baseline.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / "reports" / "conformance.json"

# Assertion counts and rough feature cost per conformance file, measured during the
# Phase 0 spike (docs/spike.md Q1). Used to rank files by value before they can run --
# once a file executes, its real counts from the report take over.
SPIKE_ESTIMATES: dict[str, tuple[int, int]] = {
    # file: (assertions, feature-cost)
    "recursion.star": (2, 10),
    "function_param.star": (0, 12),
    "control.star": (14, 12),
    "bool.star": (36, 13),
    "while.star": (3, 14),
    "tuple.star": (35, 16),
    "module.star": (7, 19),
    "math.star": (327, 21),
    "time.star": (90, 23),
    "assign.star": (54, 27),
    "paths.star": (0, 27),
    "misc.star": (34, 28),
    "bytes.star": (76, 28),
    "function.star": (50, 31),
    "dict.star": (108, 35),
    "int.star": (174, 36),
    "list.star": (108, 37),
    "set.star": (113, 37),
    "json.star": (55, 44),
    "float.star": (327, 44),
    "builtins.star": (150, 46),
    "string.star": (341, 50),
}


def load(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"no report at {path} — run `npm run conformance` first")
    return json.loads(path.read_text())


def highest_clean_tier(ladder: dict[str, Any]) -> int:
    """Highest tier with every probe passing. Tiers build on each other, so this is
    the honest 'where are we' number -- a later tier passing while an earlier one fails
    usually means an accident, not progress."""
    clean = 0
    for t in sorted(ladder["byTier"], key=lambda x: x["tier"]):
        if t["total"] > 0 and t["passed"] == t["total"]:
            clean = t["tier"]
        else:
            break
    return clean


def build_gap_report(report: dict[str, Any]) -> dict[str, Any]:
    ladder = report["ladder"]
    conf = report["conformance"]
    clean_tier = highest_clean_tier(ladder)

    # Immediate work: failing probes in the lowest incomplete tier. Everything above is
    # noise until these pass, because higher tiers depend on them.
    next_tier = clean_tier + 1
    immediate = [f for f in ladder["failures"] if f["tier"] == next_tier]

    # Conformance files ranked by assertions per unit of feature cost. Files already
    # passing cleanly are dropped.
    by_file = {f["file"]: f for f in conf["files"]}
    ranked = []
    for name, (assertions, cost) in SPIKE_ESTIMATES.items():
        result = by_file.get(name)
        if result and result["status"] == "pass":
            continue
        earned = result["assertionsPassed"] if result else 0
        available = max(assertions - earned, 0)
        if available == 0 and assertions > 0:
            continue
        ranked.append(
            {
                "file": name,
                "assertionsAvailable": available,
                "featureCost": cost,
                "value": round(available / cost, 2) if cost else 0.0,
                "status": result["status"] if result else "not-run",
                "topFailures": (result["failures"][:3] if result else []),
                "error": (result.get("error") if result else None),
            }
        )
    ranked.sort(key=lambda r: (-r["value"], r["featureCost"]))

    return {
        "implementationPresent": report["implementationPresent"],
        # The note carries the actual load failure (e.g. an unresolvable import). Without
        # it the agent sees "0 probes" with no cause and cannot act -- observed during the
        # walking-skeleton run, where it looped blind against a missing .js extension.
        "implementationNote": report.get("implementationNote"),
        "ladder": {
            "highestCleanTier": clean_tier,
            "nextTier": next_tier,
            "passed": ladder["passed"],
            "total": ladder["total"],
        },
        "conformance": {
            "assertionsPassed": conf["assertionsPassed"],
            "assertionsSeen": conf["assertionsTotal"],
            "filesPassed": conf["filesPassed"],
            "filesTotal": conf["filesTotal"],
        },
        "immediateWork": immediate,
        "rankedFiles": ranked[:8],
    }


def print_human(report: dict[str, Any], gap: dict[str, Any]) -> None:
    if not report["implementationPresent"]:
        print(f"NO IMPLEMENTATION — {report.get('implementationNote', '')}\n")

    lad, conf = gap["ladder"], gap["conformance"]
    print(f"ladder       : {lad['passed']}/{lad['total']} probes, highest clean tier {lad['highestCleanTier']}")
    print(f"conformance  : {conf['assertionsPassed']}/{conf['assertionsSeen']} assertions seen, "
          f"{conf['filesPassed']}/{conf['filesTotal']} files clean")

    if gap["immediateWork"]:
        print(f"\nNEXT — tier {lad['nextTier']} must pass before anything above it counts:")
        for f in gap["immediateWork"][:10]:
            detail = f.get("detail") or ""
            print(f"  · {f['name']}: {detail[:100]}")

    if gap["rankedFiles"]:
        print("\nHIGHEST-VALUE CONFORMANCE FILES (assertions per unit of feature cost):")
        print(f"  {'file':<22}{'avail':>7}{'cost':>6}{'value':>7}  status")
        for r in gap["rankedFiles"]:
            print(f"  {r['file']:<22}{r['assertionsAvailable']:>7}{r['featureCost']:>6}"
                  f"{r['value']:>7}  {r['status']}")


def check_regression(current: dict[str, Any], baseline_path: pathlib.Path) -> int:
    """Fails if conformance went backwards. The guardrail that makes the loop monotonic:
    a patch that lowers the pass rate is rejected, no matter how good its rationale."""
    baseline = load(baseline_path)
    cur = current["conformance"]["assertionsPassed"]
    base = baseline["conformance"]["assertionsPassed"]
    cur_probes = current["ladder"]["passed"]
    base_probes = baseline["ladder"]["passed"]

    ok = cur >= base and cur_probes >= base_probes
    print(f"assertions : {base} -> {cur} ({cur - base:+d})")
    print(f"probes     : {base_probes} -> {cur_probes} ({cur_probes - base_probes:+d})")
    print("REGRESSION" if not ok else "no regression")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=pathlib.Path, default=DEFAULT_REPORT)
    parser.add_argument("--json", action="store_true", help="emit the gap report as JSON")
    parser.add_argument("--check-regression", type=pathlib.Path, metavar="BASELINE")
    args = parser.parse_args()

    report = load(args.report)

    if args.check_regression:
        sys.exit(check_regression(report, args.check_regression))

    gap = build_gap_report(report)
    if args.json:
        json.dump(gap, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_human(report, gap)


if __name__ == "__main__":
    main()
