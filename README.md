# starport

An agent that ports the [Starlark](https://github.com/google/starlark-go) interpreter from
Go to TypeScript, steered by Starlark's own self-verifying test suite as an external oracle.

The interesting part is not the port. It is that **every claim the agent makes is checkable
by something that is not a language model**, and the architecture is arranged around that.

## The problem

Cross-language ports are judged by "does it look right", because nobody has a cheap way to
check behavioural equivalence. So they ship subtly wrong — off-by-one escapes, integer
semantics that differ at the boundaries, encoding assumptions that hold until they don't.

LLMs make this worse, not better: they produce *plausible* code faster than anyone can
review it. Generation stopped being the bottleneck. Verification is.

## The approach

Pick a target where the verifier is free and external, then let the agent search against it.

1. **`tsc --noEmit`** — static errors, ~0.7 s, free.
2. **`starlark/testdata/*.star`** — 24 conformance files, **2,136 assertions**, written in
   Starlark itself and self-checking via `assert.eq` / `assert.true`. The test file *is* the
   spec, and it was written by the people who wrote the language.

That puts the project at the strongest rung of the spec ladder:

```
prose spec → typed interface → invariant → test suite → conformance oracle
weakest                                                        strongest
```

Spec-driven development sits at the top rung; its known failure mode is silent drift,
because prose checks nothing. Here the spec is executable and self-verifying.

It also means the repair loop consumes **real failure reports** rather than model
self-critique — the distinction that separates refinement loops that work from ones that
degrade output (Huang et al., *LLMs Cannot Self-Correct Reasoning Yet*, ICLR 2024).

## Architecture

```
                    ┌──────────────────────────────────────────────┐
   plugins on the   │  observability   JSON logs · intent/outcome  │
   App wrap every   │                  OTel spans · PII redaction  │
   agent below      │  guardrails      refuse · block · ask        │
                    └──────────────────────────────────────────────┘
                                        │
  port_coordinator ....... talks to the human, owns the goal          [Pro]
   └── port_increment ..... Sequential — one increment, start to finish
        ├── planner ....... reads the gap report, picks ONE thing     [Pro]
        ├── analysts ...... Parallel — two independent reads
        │    ├── upstream_analyst ... what upstream specifies         [Flash]
        │    └── convention_analyst . what the port already does      [Flash-Lite]
        ├── porter ........ writes the code                           [Flash]
        └── verify_and_repair ...... Loop, max 5
             ├── repairer .......... fixes what the oracle reports    [Pro]
             └── stall_check ....... code, not a model, decides when to quit
```

Why this shape and not one capable agent:

| Decision | Reason |
|---|---|
| Separate roles | They need separate tools. The planner has **no write tool**, so a plan cannot silently become an edit. The analysts cannot write at all. |
| Parallel analysts | The work is genuinely independent — different corpora, neither needs the other's output — and it keeps each context small (*Lost in the Middle*). |
| A loop for repair | Repair is iterative and needs external evidence every round. |
| A stopping rule in code | Asking the model that just made a fix whether the fix worked puts the question to the party least able to answer it. `stall_check` reads `reports/conformance.json` directly and escalates after two rounds with no measured gain. |
| Three model tiers | Bulk translation is faithful transcription and Flash does it well. Choosing what to port next, and repairing a failure whose cause is three modules from where it surfaced, is where the expensive tier earns its cost. Cascade routing, FrugalGPT / RouteLLM. |

### Guardrails come in two strengths

Choosing between them is the whole design.

```
  ACTION                                RULE                     WHY THAT STRENGTH
  ─────────────────────────────────     ──────────────────────   ──────────────────────
  write into harness/ or vendor/    →   REFUSED                  never right; asking a
                                                                 human adds nothing

  a verify scoring below the best   →   BLOCKED, not warned      it was a warning once,
                                                                 and got ignored while a
                                                                 fix cost 228 assertions

  overwrite a module that is                                     sometimes right,
  already earning score             →   ASKS A HUMAN             sometimes destroys hours

  restore_best_port                 →   ASKS A HUMAN             discards the working tree
```

The halves interlock: detection is automatic, and the destructive recovery it points at
needs approval.

### Retargeting

The port target is data, not assumptions. `app/config.py` holds one `PortTarget` object —
what to read, where to write, what grades it, and the conventions whose violation is silent
and expensive. Swap it and the same agent ports something else.

Tool *names* stay concrete (`write_ported_typescript_module`, not `write_file`) because a
model picks a tool by reading its name. Renaming on retarget is cheap; a vague name is a
tax on every call.

## Measured results

Nothing below is an estimate.

```
  conformance     358 assertions passing, of 482 reached      (suite total 2,136)
  files           1 of 22 conformance files fully clean
  ladder          49 of 51 tier-0 probes, highest clean tier 5
  port            8 TypeScript modules, ~3,700 lines, gitignored
  tests           123 deterministic unit tests, zero model calls
  cycle time      generation ~99% of it; tsc is 0.7 s
```

Reproduce with `npm run conformance && python3 harness/report.py`. Snapshots in `reports/`
are committed as evidence.

### Things that were wrong, and how we found out

The project's own thesis got turned on us more than once. These are in the git history:

- A hand-written round-trip invariant said the port was broken. **The invariant was wrong
  and the generated code was right.**
- The regression gate compared totals, reported "no regression" at 40 → 46 probes, and
  missed two builtins silently breaking. Totals are a progress metric; only per-item
  comparison is a safety gate.
- Every `expectError` probe passed on *any* error, including a parse failure — so the
  oracle's negative tests were passing for the wrong reason. Found by the oracle self-test.
- `verify_ported_interpreter` had a `return` above the branch explaining a port that
  type-checks but cannot load, making it unreachable. Nothing exercised it.

## Verifying the verifier

A conformance number from an unvalidated harness is not evidence.

```bash
npm run selftest
```

Runs a deliberately incompetent interpreter — literal assignment only — and requires
**exactly 8 of 51 probes**. Too low and the harness has stopped detecting correct
behaviour; too high and it is giving marks away. This runs in CI, and it is what caught the
`expectError` defect above.

## Running it

```bash
uv sync && npm install
uv run python scripts/fetch_upstream.py   # pinned google/starlark-go

uv run pytest tests/unit                  # 123 tests, no model calls
npm run selftest                          # validate the oracle
npm run conformance                       # run the port against the suite
uv run python harness/report.py           # the work queue

agents-cli playground                     # interactive; HITL approvals appear here
agents-cli eval run                       # trajectory metric + LLM judge
```

`agents-cli playground` is the demo surface — the human-approval gates are visible there.

## Rubric map

| Row | Where |
|---|---|
| 1.1 Docstrings | `app/tools/port_tools.py`; enforced by `tests/unit/test_port_tools.py` |
| 1.2 Descriptive naming | `write_ported_typescript_module`, not `write_file` — test-enforced |
| 1.3 Explicit JSON schemas | Typed params + docstring Args → ADK schema; a test fails on any untyped or undocumented parameter |
| 1.4 Guided errors | Every tool returns `{status, error_code, message, recovery_hint}`; a test requires each hint to actually guide |
| 2.1 System instructions | `app/prompts.py` — one constitution rendered from `PortTarget`, plus short role briefs |
| 2.2 History compaction | `EventsCompactionConfig(20, overlap 3)` on the `App` |
| 2.3 Persistent state | `output_key` + `state_delta` into the session service; Vertex AI sessions and Memory Bank when deployed (`app/app_utils/services.py`) |
| 2.4 Async memory | `app/memory.py` — scheduled, never awaited; in-flight tasks held so they cannot be collected mid-write |
| 3.1 Multi-agent | Coordinator → Sequential → Parallel → Loop + `StopWhenStalled(BaseAgent)` |
| 3.2 Model routing | `app/config.py` — three tiers, each justified against measured cost |
| 3.3 Guardrails | `app/plugins/guardrails.py` — runner-wide policy plugin |
| 3.4 Human-in-the-loop | ADK `require_confirmation`, conditional on the arguments |
| 4.1 JSON logging | `app/plugins/observability.py` — Cloud Logging field names |
| 4.2 Intent vs outcome | `before_tool_callback` / `after_tool_callback` + latency + promoted progress fields |
| 4.3 Tracing | OTel trace and span ids on every line, so a log pivots to its trace |
| 4.4 PII redaction | `app/redaction.py`, on the log *and* memory paths |
| 5.1 Eval suites | `tests/eval/method_adherence.py` (deterministic, trajectory) + LLM judge; oracle self-test and per-item regression gate in CI |
| 5.2 Infrastructure as code | `deployment/terraform/` — `terraform validate` passes |
| 5.3 Secret management | No long-lived credential by design: ADC at runtime, Workload Identity Federation in CI. The one unavoidable secret comes from Secret Manager by reference, container created but never a version |

## Deliberately not done

- **Terraform is never applied.** It is written and validated; `terraform apply` and
  `agents-cli deploy` are not run.
- **`internal/compile` is out of scope.** The port is a tree-walking evaluator, not the
  bytecode VM.
- **The ported TypeScript is gitignored.** The agent, the harness, and the reports are the
  artefact; the generated output is reproducible from them.

## Attribution

- Source under port: [`google/starlark-go`](https://github.com/google/starlark-go), BSD 3-Clause,
  pinned at `5395d018f003e2a08bfbca6dcb2562acee700f62`.
- Conformance suite (`*.star`) vendored unmodified — see
  [`vendor/starlark-testdata/PROVENANCE.md`](vendor/starlark-testdata/PROVENANCE.md).

This project ports that work; it does not claim authorship of it.
