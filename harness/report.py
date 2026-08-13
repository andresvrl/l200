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
import re
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


def _error_signature(message: str) -> str:
    """Collapses a conformance error to a comparable shape.

    Strips the ``file.star:line:col:`` prefix and generalises attribute names, so that
    twenty files failing for one reason group into one actionable defect instead of
    twenty unrelated-looking strings.
    """
    message = re.sub(r"^[A-Za-z0-9_./-]+[.]star:[0-9]+:[0-9]+:\s*", "", message)
    if "attribute" in message:
        message = re.sub(r"[.][A-Za-z_][A-Za-z0-9_]*", ".<attr>", message)
    return message[:80]


def find_conformance_blockers(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Groups conformance failures by root cause, ranked by assertions unlocked.

    The ladder's "lowest incomplete tier" rule is the right order for building the
    interpreter bottom-up, but it is the wrong order for earning conformance: a cosmetic
    tier-3 gap can outrank the single defect gating every upstream file. This surfaces
    what actually unblocks the oracle.
    """
    groups: dict[str, dict[str, Any]] = {}

    for entry in report["conformance"]["files"]:
        if entry["status"] not in ("error", "fail"):
            continue
        message = entry.get("error") or (entry["failures"][0] if entry["failures"] else "")
        if not message:
            continue

        signature = _error_signature(message)
        assertions, _cost = SPIKE_ESTIMATES.get(entry["file"], (0, 1))
        group = groups.setdefault(
            signature,
            {"signature": signature, "filesBlocked": 0, "assertionsBlocked": 0, "examples": []},
        )
        group["filesBlocked"] += 1
        group["assertionsBlocked"] += assertions
        if len(group["examples"]) < 3:
            group["examples"].append({"file": entry["file"], "error": message[:120]})

    return sorted(groups.values(), key=lambda g: -g["assertionsBlocked"])


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
        "conformanceBlockers": find_conformance_blockers(report)[:5],
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

    if gap["conformanceBlockers"]:
        print("\nCONFORMANCE BLOCKERS (one defect, many files — fix these to earn assertions):")
        for b in gap["conformanceBlockers"]:
            print(f"  {b['assertionsBlocked']:>5} assertions across {b['filesBlocked']:>2} files"
                  f"  ::  {b['signature']}")
            print(f"        e.g. {b['examples'][0]['file']}: {b['examples'][0]['error'][:70]}")

    if gap["rankedFiles"]:
        print("\nHIGHEST-VALUE CONFORMANCE FILES (assertions per unit of feature cost):")
        print(f"  {'file':<22}{'avail':>7}{'cost':>6}{'value':>7}  status")
        for r in gap["rankedFiles"]:
            print(f"  {r['file']:<22}{r['assertionsAvailable']:>7}{r['featureCost']:>6}"
                  f"{r['value']:>7}  {r['status']}")


def check_regression(current: dict[str, Any], baseline_path: pathlib.Path) -> int:
    """Fails if anything that previously worked has stopped working.

    Compares PER ITEM, not in aggregate. An aggregate gate is trivially defeated by a
    change that gains more than it loses: during Step 3 the totals moved 40 -> 46 probes
    and reported "no regression" while `len` and `type` had silently broken. Totals are a
    progress metric; only per-item comparison is a safety gate.
    """
    baseline = load(baseline_path)

    base_failing = {f["name"] for f in baseline["ladder"]["failures"]}
    cur_failing = {f["name"] for f in current["ladder"]["failures"]}
    broken_probes = sorted(cur_failing - base_failing)
    fixed_probes = sorted(base_failing - cur_failing)

    base_files = {f["file"]: f["assertionsPassed"] for f in baseline["conformance"]["files"]}
    regressed_files = [
        (f["file"], base_files[f["file"]], f["assertionsPassed"])
        for f in current["conformance"]["files"]
        if f["file"] in base_files and f["assertionsPassed"] < base_files[f["file"]]
    ]

    cur_a = current["conformance"]["assertionsPassed"]
    base_a = baseline["conformance"]["assertionsPassed"]
    cur_p = current["ladder"]["passed"]
    base_p = baseline["ladder"]["passed"]

    print(f"assertions : {base_a} -> {cur_a} ({cur_a - base_a:+d})")
    print(f"probes     : {base_p} -> {cur_p} ({cur_p - base_p:+d})")
    if fixed_probes:
        print(f"fixed      : {len(fixed_probes)} probes ({', '.join(fixed_probes[:4])}"
              f"{' …' if len(fixed_probes) > 4 else ''})")

    if not broken_probes and not regressed_files:
        print("no regression")
        return 0

    for name in broken_probes:
        print(f"REGRESSED  : probe '{name}' passed in baseline, fails now")
    for name, was, now in regressed_files:
        print(f"REGRESSED  : {name} earned {was} assertions, now earns {now}")
    print(f"\nREGRESSION — {len(broken_probes) + len(regressed_files)} item(s) went backwards")
    return 1


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
