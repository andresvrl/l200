# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tools for porting the Starlark interpreter from Go to TypeScript.

Every tool returns a dict with a ``status`` key rather than raising. A raised exception
reaches the model as an opaque stack trace it cannot act on; a structured error with a
``recovery_hint`` tells it what to do next. Errors are guidance, not just diagnosis.
"""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
UPSTREAM = ROOT / "vendor" / "starlark-go"
PORTED = ROOT / "ported"

# Snapshot of the highest-scoring port seen so far, plus the report that scored it.
# ported/ is gitignored, so without this a bad edit is unrecoverable -- which happened
# once: a local tuple fix cost 228 assertions and the good version was simply gone.
BEST_DIR = ROOT / ".port-best"
BEST_REPORT = ROOT / "reports" / "best.json"

# Generation is ~99% of the port cycle (docs/spike.md Q2), so verification timeouts are
# generous by comparison -- a slow verify is never the bottleneck worth optimising.
BUILD_TIMEOUT_S = 300
VERIFY_TIMEOUT_S = 300


def _score(report: dict[str, Any]) -> tuple[int, int]:
    """Ranks a run: conformance assertions first, ladder probes as the tie-break.

    Upstream assertions outrank our own probes because only upstream establishes
    conformance -- the ladder is a progress signal we wrote ourselves.
    """
    return (report["conformance"]["assertionsPassed"], report["ladder"]["passed"])


def _snapshot_if_best(report: dict[str, Any]) -> bool:
    """Keeps a copy of the best port seen so far. Returns True if this run is the new best."""
    if BEST_REPORT.exists():
        previous = json.loads(BEST_REPORT.read_text())
        if _score(report) <= _score(previous):
            return False

    if BEST_DIR.exists():
        shutil.rmtree(BEST_DIR)
    shutil.copytree(PORTED, BEST_DIR)
    BEST_REPORT.parent.mkdir(parents=True, exist_ok=True)
    BEST_REPORT.write_text(json.dumps(report, indent=2))
    return True


def restore_best_port() -> dict[str, Any]:
    """Restores the highest-scoring version of the port recorded so far.

    Use this after a change makes things worse and the cause is not obvious. Reverting to
    known-good and retrying a smaller step beats debugging a port that has regressed in
    several places at once.

    Returns:
        On success, a dict with ``status`` "ok", ``files_restored``, and the ``score`` that
        snapshot achieved. On failure, a dict with ``status`` "error" and a
        ``recovery_hint``.
    """
    if not BEST_DIR.exists() or not BEST_REPORT.exists():
        return _error(
            "no_snapshot",
            "No known-good snapshot has been recorded yet.",
            "Snapshots are taken automatically whenever verify_ported_interpreter finds a "
            "new best score. Keep working and one will appear.",
        )

    if PORTED.exists():
        shutil.rmtree(PORTED)
    shutil.copytree(BEST_DIR, PORTED)
    best = json.loads(BEST_REPORT.read_text())
    assertions, probes = _score(best)
    return {
        "status": "ok",
        "files_restored": len(list(PORTED.rglob("*.ts"))),
        "score": {"assertions": assertions, "probes": probes},
        "recovery_hint": "Re-run verify_ported_interpreter to confirm, then retry in smaller steps.",
    }


def _error(code: str, message: str, recovery_hint: str, **extra: Any) -> dict[str, Any]:
    """Builds a structured error the model can recover from."""
    return {
        "status": "error",
        "error_code": code,
        "message": message,
        "recovery_hint": recovery_hint,
        **extra,
    }


def list_upstream_go_modules() -> dict[str, Any]:
    """Lists the Go source files available to port, with their sizes.

    Call this before reading a module so paths are chosen from what exists rather than
    guessed. Sizes indicate roughly how much work each module represents.

    Returns:
        On success, a dict with ``status`` "ok" and ``modules``: a list of
        ``{"path", "bytes", "lines"}`` entries sorted smallest first. On failure, a dict
        with ``status`` "error" and a ``recovery_hint``.
    """
    if not UPSTREAM.exists():
        return _error(
            "upstream_missing",
            f"Upstream Go source is not present at {UPSTREAM}.",
            "Run `uv run python scripts/fetch_upstream.py` from the repository root, then retry.",
        )

    modules = []
    for path in sorted(UPSTREAM.rglob("*.go")):
        text = path.read_text(encoding="utf-8", errors="replace")
        modules.append(
            {
                "path": str(path.relative_to(UPSTREAM)),
                "bytes": len(text),
                "lines": text.count("\n") + 1,
            }
        )
    modules.sort(key=lambda m: m["bytes"])
    return {"status": "ok", "module_count": len(modules), "modules": modules}


def read_upstream_go_source(module_path: str) -> dict[str, Any]:
    """Reads one Go source file from the pinned upstream Starlark implementation.

    This is the reference behaviour being ported. Read a module before porting it rather
    than reconstructing Starlark semantics from memory -- upstream's edge cases (integer
    width, error message text, escape handling) are the specification.

    Args:
        module_path: Path relative to the upstream root, e.g. ``"syntax/quote.go"`` or
            ``"starlark/int.go"``. Call ``list_upstream_go_modules`` for valid values.

    Returns:
        On success, a dict with ``status`` "ok", ``path``, ``source`` (the full file text),
        ``bytes`` and ``lines``. On failure, a dict with ``status`` "error", a
        ``recovery_hint``, and ``available_paths`` listing nearby valid choices.
    """
    if not UPSTREAM.exists():
        return _error(
            "upstream_missing",
            f"Upstream Go source is not present at {UPSTREAM}.",
            "Run `uv run python scripts/fetch_upstream.py` from the repository root, then retry.",
        )

    candidate = (UPSTREAM / module_path).resolve()
    if not candidate.is_relative_to(UPSTREAM.resolve()):
        return _error(
            "path_escape",
            f"{module_path!r} resolves outside the upstream source tree.",
            "Pass a path relative to the upstream root, such as 'syntax/scan.go'.",
        )

    if not candidate.is_file():
        available = sorted(str(p.relative_to(UPSTREAM)) for p in UPSTREAM.rglob("*.go"))
        stem = pathlib.Path(module_path).stem
        near = [p for p in available if stem and stem in p]
        return _error(
            "module_not_found",
            f"No such upstream module: {module_path!r}.",
            "Choose a path from available_paths, or call list_upstream_go_modules first.",
            available_paths=near or available[:40],
        )

    text = candidate.read_text(encoding="utf-8", errors="replace")
    return {
        "status": "ok",
        "path": module_path,
        "source": text,
        "bytes": len(text),
        "lines": text.count("\n") + 1,
    }


def write_ported_typescript_module(relative_path: str, source_code: str) -> dict[str, Any]:
    """Writes a TypeScript module into the port output directory.

    The module must satisfy ``harness/contract.ts``. In particular ``ported/index.ts`` must
    export ``execFile``, since that is the entry point the conformance harness calls.

    Writing does not verify anything. Call ``verify_ported_interpreter`` afterwards -- type
    checking costs under a second, so there is never a reason to batch several writes
    before verifying.

    Args:
        relative_path: Destination path under ``ported/``, e.g. ``"index.ts"`` or
            ``"syntax/scanner.ts"``. Must end in ``.ts``.
        source_code: Complete TypeScript source for the file. This replaces any existing
            content; it is not a patch.

    Returns:
        On success, a dict with ``status`` "ok", ``path``, ``bytes_written`` and ``lines``.
        On failure, a dict with ``status`` "error" and a ``recovery_hint``.
    """
    if not relative_path.endswith(".ts"):
        return _error(
            "bad_extension",
            f"{relative_path!r} is not a TypeScript file.",
            "Use a path ending in '.ts', for example 'index.ts'.",
        )

    target = (PORTED / relative_path).resolve()
    if not target.is_relative_to(PORTED.resolve()):
        return _error(
            "path_escape",
            f"{relative_path!r} resolves outside the ported/ directory.",
            "Write only inside ported/. Never modify the harness, the vendored suite, or the agent.",
        )

    if not source_code.strip():
        return _error(
            "empty_source",
            "Refusing to write an empty module.",
            "Provide the complete TypeScript source; this tool overwrites rather than patches.",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source_code, encoding="utf-8")
    return {
        "status": "ok",
        "path": str(target.relative_to(ROOT)),
        "bytes_written": len(source_code),
        "lines": source_code.count("\n") + 1,
    }


def edit_ported_typescript_module(
    relative_path: str, existing_text: str, replacement_text: str
) -> dict[str, Any]:
    """Replaces one exact fragment of an already-written TypeScript module.

    Prefer this over ``write_ported_typescript_module`` for any change smaller than the
    whole file. Regenerating a 20 KB module to fix three lines costs roughly forty seconds
    and gives the model a fresh opportunity to regress code that was already correct --
    observed during the walking-skeleton run, where a whole-file rewrite silently undid an
    import fix that had already been applied.

    Args:
        relative_path: Path under ``ported/``, e.g. ``"eval.ts"``.
        existing_text: Exact text to replace, including indentation. Must appear exactly
            once in the file; ambiguous or absent matches are refused rather than guessed.
        replacement_text: Text to substitute in its place.

    Returns:
        On success, a dict with ``status`` "ok", ``path``, and ``bytes_written``. On
        failure, a dict with ``status`` "error", a ``recovery_hint``, and where useful an
        ``occurrences`` count so the fragment can be made unique.
    """
    target = (PORTED / relative_path).resolve()
    if not target.is_relative_to(PORTED.resolve()):
        return _error(
            "path_escape",
            f"{relative_path!r} resolves outside the ported/ directory.",
            "Edit only inside ported/.",
        )
    if not target.is_file():
        return _error(
            "module_not_found",
            f"No such ported module: {relative_path!r}.",
            "Create it with write_ported_typescript_module first, or check the path.",
        )

    content = target.read_text(encoding="utf-8")
    occurrences = content.count(existing_text)

    if occurrences == 0:
        return _error(
            "fragment_not_found",
            f"The given text does not appear in {relative_path!r}.",
            "Read the file's current contents before editing; it may already have changed. "
            "Whitespace and indentation must match exactly.",
            occurrences=0,
        )
    if occurrences > 1:
        return _error(
            "fragment_ambiguous",
            f"The given text appears {occurrences} times in {relative_path!r}.",
            "Include more surrounding context so the fragment matches exactly once.",
            occurrences=occurrences,
        )

    updated = content.replace(existing_text, replacement_text)
    target.write_text(updated, encoding="utf-8")
    return {
        "status": "ok",
        "path": str(target.relative_to(ROOT)),
        "bytes_written": len(updated),
        "bytes_delta": len(updated) - len(content),
    }


def read_ported_typescript_module(relative_path: str) -> dict[str, Any]:
    """Reads back a module that has already been written to the port.

    Needed before editing, since the file may have changed since it was written, and
    ``edit_ported_typescript_module`` requires an exact match.

    Args:
        relative_path: Path under ``ported/``, e.g. ``"eval.ts"``.

    Returns:
        On success, a dict with ``status`` "ok", ``path``, ``source``, ``bytes`` and
        ``lines``. On failure, a dict with ``status`` "error", a ``recovery_hint``, and
        ``existing_modules`` listing what has been written so far.
    """
    target = (PORTED / relative_path).resolve()
    if not target.is_relative_to(PORTED.resolve()):
        return _error(
            "path_escape",
            f"{relative_path!r} resolves outside the ported/ directory.",
            "Read only inside ported/.",
        )
    if not target.is_file():
        existing = (
            sorted(str(p.relative_to(PORTED)) for p in PORTED.rglob("*.ts"))
            if PORTED.exists()
            else []
        )
        return _error(
            "module_not_found",
            f"No such ported module: {relative_path!r}.",
            "Choose one of existing_modules, or write it first.",
            existing_modules=existing,
        )

    text = target.read_text(encoding="utf-8")
    return {
        "status": "ok",
        "path": relative_path,
        "source": text,
        "bytes": len(text),
        "lines": text.count("\n") + 1,
    }


def verify_ported_interpreter() -> dict[str, Any]:
    """Type-checks the port, runs the conformance oracle, and returns the work queue.

    This is the ground truth for progress. It runs three stages:

    1. ``tsc`` in strict mode -- if this fails, no conformance result is meaningful, so
       the type errors are returned and later stages are skipped.
    2. The conformance runner -- tier-0 ladder probes plus the vendored upstream
       ``*.star`` suite.
    3. The gap report -- remaining work ranked by assertions per unit of feature cost.

    Trust this over any judgement about whether the code looks correct. During the Phase 0
    spike a hand-written correctness assumption was wrong where the generated code was
    right; only the oracle settles it.

    Returns:
        A dict with ``status`` "ok" (verification ran) or "error" (it could not run).
        When ``status`` is "ok", ``typecheck_passed`` reports whether ``tsc`` succeeded;
        if false, ``typecheck_errors`` holds the diagnostics and must be fixed first.
        Otherwise ``ladder``, ``conformance``, ``immediate_work`` and ``ranked_files``
        describe measured progress and what to do next.
    """
    build = subprocess.run(
        ["npx", "tsc", "-p", "tsconfig.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_S,
    )
    if build.returncode != 0:
        diagnostics = [ln for ln in build.stdout.splitlines() if ": error TS" in ln]
        return {
            "status": "ok",
            "typecheck_passed": False,
            "typecheck_error_count": len(diagnostics),
            "typecheck_errors": diagnostics[:40],
            "recovery_hint": (
                "Fix these type errors and call verify_ported_interpreter again. Type checking "
                "is the cheapest signal available; conformance results are not computed until "
                "it passes."
            ),
        }

    run = subprocess.run(
        ["node", "dist/harness/run_conformance.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=VERIFY_TIMEOUT_S,
    )
    if run.returncode != 0:
        return _error(
            "conformance_runner_failed",
            f"The conformance runner exited {run.returncode}: {run.stderr[:600]}",
            "This usually means ported/index.ts does not match harness/contract.ts. Confirm it "
            "exports execFile(filename, src, predeclared, thread) and retry.",
        )

    gap = subprocess.run(
        ["python3", "harness/report.py", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=VERIFY_TIMEOUT_S,
    )
    if gap.returncode != 0:
        return _error(
            "gap_report_failed",
            f"Could not build the gap report: {gap.stderr[:400]}",
            "Check that reports/conformance.json was written by the conformance runner.",
        )

    report = json.loads(gap.stdout)
    result: dict[str, Any] = {
        "status": "ok",
        "typecheck_passed": True,
        "implementation_loaded": report["implementationPresent"],
        "ladder": report["ladder"],
        "conformance": report["conformance"],
        "immediate_work": report["immediateWork"],
        # Root causes grouped across conformance files, ranked by assertions unlocked. The
        # ladder's tier order builds the interpreter bottom-up but is the wrong order for
        # earning conformance -- a cosmetic tier-3 gap can outrank the defect gating every
        # upstream file.
        "conformance_blockers": report["conformanceBlockers"],
        "ranked_files": report["rankedFiles"],
    }

    # Snapshot the best port automatically, and warn loudly when a change lost ground --
    # a local win with a global blast radius is the failure mode this catches.
    is_best = _snapshot_if_best(report)
    result["is_best_so_far"] = is_best
    if not is_best and BEST_REPORT.exists():
        best = json.loads(BEST_REPORT.read_text())
        best_a, best_p = _score(best)
        now_a, now_p = _score(report)
        if now_a < best_a:
            result["regression_warning"] = (
                f"This version earns {now_a} assertions; the best recorded is {best_a} "
                f"({best_a - now_a} lost). Consider restore_best_port and a smaller step."
            )
    return result

    # Type checking can pass while the module still fails to LOAD -- most often an import
    # that Node's ESM loader cannot resolve. Surfacing the loader's own message is the
    # difference between a fixable error and an unexplained zero.
    if not report["implementationPresent"]:
        result["load_error"] = report.get("implementationNote")
        result["recovery_hint"] = (
            "The port type-checks but could not be loaded, so every probe scored zero. Read "
            "load_error for the loader's message. The usual cause is a relative import "
            "missing its '.js' extension: Node ESM requires `from './eval.js'`, not "
            "`from './eval'`. Fix the imports and verify again."
        )
    return result
