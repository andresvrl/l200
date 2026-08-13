# starport

A conformance-driven agent that ports the [Starlark](https://github.com/google/starlark-go)
interpreter from Go to TypeScript, using Starlark's own self-verifying test suite as the oracle.

> Status: **in development.** See [`docs/spike.md`](docs/spike.md) for the Phase 0
> feasibility findings that shaped the architecture.

## The problem

Cross-language ports are judged by "does it look right", because nobody has a cheap way to
check behavioural equivalence. So they ship subtly wrong — off-by-one escapes, integer
semantics that differ at the boundaries, encoding assumptions that hold until they don't.

LLMs make this worse, not better: they produce *plausible* code faster than anyone can
review it. Generation stopped being the bottleneck; verification is.

## The approach

Pick a target where the verifier is free and external, then let the agent search against it.

1. **`tsc --noEmit`** — static errors, ~0.7 s, free.
2. **`starlark/testdata/*.star`** — 24 conformance files, **2,136 assertions**, written in
   Starlark itself and self-checking via `assert.eq` / `assert.true`. The test file *is* the spec.

This puts the project at the strongest rung of the spec ladder:

```
prose spec → typed interface → invariant → test suite → conformance oracle
weakest                                                        strongest
```

Spec-driven development sits at the top rung; its known failure mode is silent drift,
because prose checks nothing. Here the spec is executable and self-verifying.

It also means the refinement loop consumes **real failure reports** rather than model
self-critique — the distinction that separates refinement loops that work from ones that
degrade output (Huang et al., *LLMs Cannot Self-Correct Reasoning Yet*, ICLR 2024).

## Attribution

- Source under port: [`google/starlark-go`](https://github.com/google/starlark-go) — BSD 3-Clause.
- Conformance suite (`*.star`) vendored from the same repository, unmodified.

This project ports that work; it does not claim authorship of it.
