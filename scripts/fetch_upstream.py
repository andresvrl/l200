"""Fetches the upstream Go source under port, pinned to a specific commit.

The source is NOT committed to this repository -- only the agent, the harness and the
conformance suite are. This script makes the working tree reproducible without vendoring
385 KB of someone else's code.

The pinned commit must match vendor/starlark-testdata/PROVENANCE.md: the conformance suite
and the source under port have to describe the same language, or the oracle is measuring
against a different specification than the agent is reading.

Usage:
    uv run python scripts/fetch_upstream.py [--force]
"""

from __future__ import annotations

import argparse
import io
import pathlib
import shutil
import sys
import tarfile
import urllib.request

# Must match vendor/starlark-testdata/PROVENANCE.md.
UPSTREAM_REPO = "google/starlark-go"
UPSTREAM_COMMIT = "5395d018f003e2a08bfbca6dcb2562acee700f62"

# Packages the port needs. internal/compile is deliberately excluded: the port is a
# tree-walking evaluator, so the bytecode VM (57 KB) is out of scope. See docs/spike.md.
WANTED_PREFIXES = ("syntax/", "resolve/", "starlark/", "starlarkstruct/", "LICENSE")

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "starlark-go"


def fetch(force: bool) -> int:
    if DEST.exists():
        if not force:
            files = sum(1 for _ in DEST.rglob("*.go"))
            print(f"already present: {DEST} ({files} .go files) — use --force to refetch")
            return 0
        shutil.rmtree(DEST)

    url = f"https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/{UPSTREAM_COMMIT}"
    print(f"fetching {UPSTREAM_REPO}@{UPSTREAM_COMMIT[:12]} ...")

    try:
        with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - fixed https host
            payload = resp.read()
    except Exception as exc:  # pragma: no cover - network failure path
        print(f"ERROR: could not download {url}: {exc}", file=sys.stderr)
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    extracted = 0

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # Strip the "<repo>-<sha>/" prefix the tarball wraps everything in.
            rel = member.name.split("/", 1)[1] if "/" in member.name else member.name
            if not rel.startswith(WANTED_PREFIXES):
                continue
            # Test files are not needed; the conformance suite is the oracle.
            if rel.endswith("_test.go"):
                continue

            target = DEST / rel
            # Defend against path traversal in a downloaded archive.
            if not target.resolve().is_relative_to(DEST.resolve()):
                print(f"WARNING: skipping suspicious path {rel}", file=sys.stderr)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                continue
            target.write_bytes(source.read())
            extracted += 1

    total_bytes = sum(p.stat().st_size for p in DEST.rglob("*.go"))
    print(f"extracted {extracted} files ({total_bytes / 1024:.0f} KB of Go) to {DEST.relative_to(ROOT)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="refetch even if already present")
    sys.exit(fetch(parser.parse_args().force))


if __name__ == "__main__":
    main()
