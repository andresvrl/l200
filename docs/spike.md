# Phase 0 — feasibility spike

Timeboxed spike run before any agent code, to answer three questions that would have
invalidated the architecture if answered badly. Spike code was discarded; these are the
findings.

Target under test: `google/starlark-go` `syntax/quote.go` (309 lines) → TypeScript.

---

## Q1 — Is the conformance metric a step function?

**Largely no.** The concern was that every `.star` file opens with
`load("assert.star", "assert")`, so nothing would pass until most of the interpreter existed.

`starlarktest/assert.star` documents its own contract:

> `error(msg)`: report an error in Go's test framework **without halting execution**.
> This is distinct from the built-in `fail` function, which halts execution.

Assertion failures do not stop the file. With a native `assert` shim controlling `error()`,
each conformance file yields **partial credit** — N passed of M — so the metric is continuous
*within* files, not only across them.

### Measured surface

24 files, **2,136 assertions total**. Cheapest-first ordering (cost = weighted count of
distinct language features required):

| File | Cost | Assertions | Notes |
|---|---:|---:|---|
| `recursion.star` | 10 | 2 | cheapest reachable real file |
| `control.star` | 12 | 14 | |
| `bool.star` | 13 | 36 | good early value |
| `while.star` | 14 | 3 | |
| `tuple.star` | 16 | 35 | |
| `math.star` | 21 | **327** | **best value in the suite** — 15% of all assertions |
| `dict.star` | 35 | 108 | |
| `int.star` | 36 | 174 | |
| `string.star` | 50 | 341 | most expensive |

`string`, `float` and `math` together hold **47%** of all assertions.

**Floor for the first real file:** `def`, calls, tuples, recursion, and `catch()` for
`assert.fails`. No dicts, comprehensions, string methods or floats required. That is
syntax + resolve + core eval + functions — a credible early milestone.

> Caveat: the `tuple`, `list` and `kwargs_call` feature detectors are regex-based and
> over-fire on ordinary call syntax, inflating absolute costs by a roughly constant amount.
> Ordering is driven by the distinctive features (`float`, `bigint`, `str_method`,
> comprehensions, `set`, `dict`) and is trusted directionally only.

---

## Q2 — How long is one port → verify cycle?

| Stage | Wall clock |
|---|---:|
| Generation (`gemini-3.5-flash`, 2,979 in / 4,898 out tokens) | **112.7 s** |
| `tsc --noEmit` | **0.7 s** |
| **Total** | **113.4 s** |

Generation is **99.4%** of the cycle. Three design consequences:

1. **Verify after every single change.** There is no cost argument for batching.
2. **Parallel fan-out is the highest-leverage optimisation** — generation is the only
   bottleneck and it parallelises cleanly.
3. **Prompt quality beats repair speed.** Each repair round costs another ~113 s, so
   avoiding a round is worth more than making one faster.

---

## Q3 — Can the models produce a correct port?

**Yes.** `gemini-3.5-flash`, first attempt, temperature 0:

- 309 lines Go → 415 lines TypeScript
- **0 errors** under `tsc --strict`
- **214/214** metamorphic round-trip cases pass (all 256 byte values, 2/3/4-byte UTF-8,
  200 seeded-random byte strings)

It was not a transliteration. It modelled Go `string` as `Uint8Array` rather than TS
`string`, wrote its own `decodeRune`/`encodeRune` UTF-8 codec (Go gets this from its
runtime; TypeScript does not), preserved multi-return as a struct, and kept Go's
error-as-value idiom:

```ts
export function Quote(s: Uint8Array, b: boolean): Uint8Array
export function unquote(quoted: Uint8Array):
    { s: Uint8Array; triple: boolean; isByte: boolean; err: Error | null }
```

**Caveat that shapes the design:** the prompt explicitly warned that Go strings are byte
sequences while TypeScript strings are UTF-16. That warning is likely why it reached for
`Uint8Array`. Semantic traps must therefore be encoded in the agent's system instruction
rather than left to the model to notice.

---

## Methodological finding (the important one)

The first round-trip run failed **201/214** with `non-ASCII hex escape \x80`. This looked
like a port bug. It was not.

From `quote.go`:

```go
// unquote
if !isByte && n > 127 {
    err = fmt.Errorf(`non-ASCII hex escape %s (use \u%04X ...)`, ...)
}
// Quote: "String (!b) literals accept \xXX escapes only for ASCII"
```

Go's `Quote(s, false)` emits `\xNN` for invalid UTF-8, and Go's own `unquote` then rejects
it — the round trip is genuinely **asymmetric in Go** for string (non-bytes) literals. The
port had faithfully reproduced that asymmetry, error-message text included. Re-running with
`b=true` (bytes literals, where high escapes are legal) gave 214/214.

**The invariant was wrong, not the code.** Hand-written oracles encode their author's
misunderstandings. This is direct evidence for anchoring on the *upstream* conformance
suite rather than properties we invent — which is the project's central thesis, now with
a worked example against it.

---

## Environment findings

- **ADC is not your `gcloud` account.** On a GCE VM, Application Default Credentials resolve
  to the attached service account (`devbox-runner@…`) via the metadata server, *not* the
  active `gcloud` user. A `roles/owner` grant on the user account does not apply to SDK calls.
  Diagnose with `curl https://oauth2.googleapis.com/tokeninfo?access_token=$(gcloud auth
  application-default print-access-token)` — a service-account token has no `email` claim.
- IAM binding propagation for `roles/aiplatform.user` took several minutes; an initial
  403 `aiplatform.endpoints.predict` resolved with no config change.
- Available model tiers confirmed in `dreva-argolis-1`: `gemini-3.1-pro-preview`,
  `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`.

---

## Verdict

Proceed to Phase 1. No re-scope needed; the `google/robotstxt` fallback is not required.
