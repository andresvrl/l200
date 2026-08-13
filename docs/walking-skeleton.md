# Phase 1 — walking skeleton and decision gate

The thinnest end-to-end slice: one agent, six tools, no memory, no model routing, no
guardrails. Its only purpose was to answer whether the loop closes — read the gap report,
write TypeScript, verify against the oracle, repair from real failures, and watch the
score rise — before building ten steps of infrastructure on top of that assumption.

## Verdict: the gate passes

| | Before | After |
|---|---:|---:|
| Ladder probes | 0 / 51 | **40 / 51** |
| Highest clean tier | 0 | **2** |
| Type errors | — | 0 |

Target was tiers 1 and 2. Both are clean, and tier 4 came along for free.

| Tier | Result | |
|---|---:|---|
| 1 — literals, assignment | 8/8 | ✅ |
| 2 — arithmetic, comparison | 10/10 | ✅ |
| 3 — collections | 10/11 | `tuple` returns a bare array, not the contract's `Tuple` |
| 4 — control flow | 7/7 | ✅ |
| 5 — functions | 2/8 | parser rejects `return <expr>`: *"got INT, want NEWLINE"* |
| 6 — host interop | 3/7 | bigint mixing, module attribute access, `%` formatting |

The agent produced roughly 78 KB of TypeScript — scanner, parser, AST, evaluator, value
model — that lexes, parses, and evaluates a real subset of Starlark.

**No re-scope needed.** The `google/robotstxt` fallback stays on the shelf.

## Three defects the skeleton exposed

Each would have been far more expensive to find later, and none were visible from reading
the design.

### 1. The cheap verifier wasn't catching what it should

`moduleResolution: "bundler"` accepts extensionless relative imports. Node's ESM loader does
not. So `tsc` passed in 0.7 s and the port then failed to load at run time with
`Cannot find module dist/ported/eval` — inverting the entire premise that the cheap signal
should catch as much as possible.

Fixed by moving to `moduleResolution: "nodenext"`, which turns it into a type error with an
exact suggestion (`Did you mean './eval.js'?`).

### 2. The agent was looping blind

`verify_ported_interpreter` returned the ladder score but dropped `implementationNote` —
the loader's own error message. The agent saw "0 of 51, no implementation" with no cause,
and could not act. Worse, the tool's `recovery_hint` blamed a contract mismatch, actively
misdirecting it.

Fixed by surfacing `load_error` and rewriting the hint to name the actual common cause.

### 3. Whole-file rewrites silently regressed working code

With only `write_ported_typescript_module` available, fixing three import lines in a 20 KB
module meant regenerating the entire file — roughly forty seconds, and a fresh opportunity
to break something already correct. Observed directly: the count of correct imports went
**8 → 7 → 8** as a regeneration undid a fix that had already landed.

Fixed by adding `read_ported_typescript_module` and `edit_ported_typescript_module`, which
does exact-fragment replacement and refuses ambiguous or missing matches rather than
guessing.

## Two things that worked as designed

- **The positive-control mechanism fired in the wild.** The `fail() halts` probe reported
  *"positive control failed — negative result is meaningless"* because the interpreter
  cannot yet parse `def`/`return`. Without the control added during Step 2, that probe would
  have scored as a pass and inflated the number.
- **The agent repaired monotonically from real signal.** Given actionable type errors it
  fixed 11 broken imports without further prompting.

## Correction to the Phase 0 cycle-time measurement

`docs/spike.md` reports ~113 s per generation. That figure is **model-specific**: it was
measured with `gemini-3.5-flash`, whereas the scaffold uses `gemini-3.6-flash`, which
produced ~54 KB of TypeScript in ~115 s — roughly **3× faster**. The qualitative conclusion
is unchanged and if anything strengthened: verification remains effectively free relative to
generation, so verify after every change.

## Highest-value next fix

Every one of the 22 conformance files currently errors, and the most common cause is a
single gap: `bool.star:6:1: object has no attribute .true`. The port does not resolve
attribute access against the contract's `NativeModule` shape (`{__starlarkModule__,
members}`), so `assert.eq(...)` fails on every file.

Fixing module attribute access plus `return <expr>` parsing should unlock the first real
conformance assertions — currently 0 of 2,136.

## Known limitation of this phase

There is no observability yet. During one run the agent sat for over four minutes with zero
CPU, blocked on a model call, with no way to see which tool call was in flight. That is the
concrete motivation for the intent-versus-outcome logging in Step 8, rather than a
box-ticking exercise for the rubric.
