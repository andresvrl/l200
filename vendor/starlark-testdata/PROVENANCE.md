# Vendored conformance suite

These files are copied **unmodified** from [`google/starlark-go`](https://github.com/google/starlark-go).

| | |
|---|---|
| Upstream commit | `5395d018f003e2a08bfbca6dcb2562acee700f62` |
| Commit date | 2026-07-08T15:06:28Z |
| Source paths | `starlark/testdata/*.star`, `starlarktest/assert.star` |
| Licence | BSD 3-Clause — see `LICENSE` |

## Why vendored rather than fetched

The conformance suite is this project's **oracle**. Fetching it at run time would mean the
measured pass rate could change without a commit, making results irreproducible and
regression gates meaningless. Pinning it makes every reported number attributable to a
specific tree.

## Do not edit these files

They are the specification. If a test appears wrong, the port is wrong — that assumption was
validated during the Phase 0 spike, where a hand-written invariant turned out to be
incorrect while the generated code was faithful to upstream behaviour. See
[`docs/spike.md`](../../docs/spike.md).

`assert.star` is vendored for reference only; the harness supplies a **native TypeScript
`assert` module** instead, so that `load()` and the module system are not prerequisites for
the first passing assertion.

## Refreshing

Re-copy from the pinned commit, or bump the pin deliberately and re-baseline the reports in
`reports/`. Never edit in place.
