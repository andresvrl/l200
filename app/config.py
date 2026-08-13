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

"""What is being ported, and which model does which part of the work.

Two configurations live here, and everything else in the agent reads them rather than
hard-coding the answer:

* :class:`PortTarget` -- the job. Swap this object and the same agent ports a different
  project. Starlark-to-TypeScript is one instance, not a built-in assumption.
* :class:`ModelTiers` -- who does what. Different steps of a port have genuinely
  different difficulty, and paying top-tier prices for mechanical translation is waste.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PortTarget:
    """A single cross-language porting job.

    The fields are deliberately concrete: paths, globs, and sentences. Anything the agent
    needs to know that is specific to *this* port belongs here, so that retargeting is one
    object rather than a search through prompts and tools.
    """

    name: str
    """Short identifier used in logs and memory keys, e.g. ``"starlark-go-to-ts"``."""

    source_language: str
    """Language being ported FROM, as the model should refer to it. e.g. ``"Go"``."""

    target_language: str
    """Language being ported TO. e.g. ``"TypeScript"``."""

    upstream_dir: pathlib.Path
    """Vendored, pinned copy of the source being ported. Read-only to the agent."""

    source_glob: str
    """Glob matching source files worth porting, e.g. ``"*.go"``."""

    output_dir: pathlib.Path
    """Where ported code is written. The only directory the agent may modify."""

    output_suffix: str
    """Extension the agent must write, e.g. ``".ts"``."""

    entry_module: str
    """Module the oracle imports, relative to ``output_dir``, e.g. ``"index.ts"``."""

    entry_signature: str
    """The entry point's exact signature, quoted in the prompt so it cannot drift."""

    contract_path: pathlib.Path
    """Hand-written interface the port must satisfy. Written before the port existed."""

    oracle: str
    """One sentence naming what grades the port. This is the whole method: the agent is
    steered by an external, executable verifier rather than by its own judgement."""

    conventions: tuple[str, ...]
    """Non-negotiable rules for this language pair, rendered verbatim into the prompt.

    Each entry states the rule AND the consequence of breaking it. A rule without a
    consequence reads as a preference, and the model trades preferences away under
    pressure -- these are the rules where a violation is silent and expensive.
    """


STARLARK_TO_TYPESCRIPT = PortTarget(
    name="starlark-go-to-ts",
    source_language="Go",
    target_language="TypeScript",
    upstream_dir=ROOT / "vendor" / "starlark-go",
    source_glob="*.go",
    output_dir=ROOT / "ported",
    output_suffix=".ts",
    entry_module="index.ts",
    entry_signature="execFile(filename, src, predeclared, thread)",
    contract_path=ROOT / "harness" / "contract.ts",
    oracle=(
        "Starlark's own conformance suite: 24 .star files containing 2,136 self-checking "
        "assertions, plus a 51-probe ladder and the TypeScript compiler in strict mode."
    ),
    conventions=(
        "Starlark integers are ARBITRARY PRECISION. Represent them as TypeScript `bigint`, "
        "never `number`. Using `number` passes early tests and fails late ones, which is the "
        "worst possible failure mode.",
        "Go strings are BYTE sequences; TypeScript strings are UTF-16. Where upstream "
        "operates on bytes, use `Uint8Array` and handle the encoding explicitly.",
        "Value mapping is fixed by the contract: None -> null, bool -> boolean, int -> bigint, "
        "float -> number, string -> string, bytes -> Uint8Array, list -> array, tuple -> Tuple, "
        "dict -> Map, set -> Set.",
        "Native functions are called as `fn(args, kwargs)` -- a single array and a single "
        "object -- NOT as `fn(...args)`. Getting this wrong type-checks and then fails at "
        "runtime on every builtin at once.",
        "Relative imports MUST carry an explicit `.js` extension, because the output runs "
        "under Node's ESM loader: write `from \"./eval.js\"`, never `from \"./eval\"`. Getting "
        "it wrong means the port type-checks but cannot load, scoring zero everywhere.",
        "Error message text is observable behaviour. Upstream tests assert on it, so "
        "preserve it exactly.",
        "The code must type-check under `strict` with `noUncheckedIndexedAccess`. Never "
        "weaken the TypeScript configuration; the type checker is the cheapest verifier "
        "available, at under a second per run.",
    ),
)

# The active job. One line to retarget the agent.
#
# Retargeting also means renaming the tools -- `write_ported_typescript_module` becomes
# `write_ported_rust_module`. That is deliberate: a model chooses a tool by reading its
# name, so a generic `write_file` would be worse at the thing that matters most. The rename
# is a cheap, mechanical edit; a vague tool name is a permanent tax on every call.
TARGET = STARLARK_TO_TYPESCRIPT


@dataclass(frozen=True)
class ModelTiers:
    """Which model handles which step, and why.

    A port is not one kind of work. Translating a switch statement is mechanical; deciding
    which of forty failing assertions to chase is not. Routing by step is the cascade
    pattern from FrugalGPT and RouteLLM: send work to the cheapest model that demonstrably
    suffices, and escalate only where the cheap model measurably plateaus.

    The split below is calibrated to two measurements from the Phase 0 spike:
    generation is 99.4% of cycle time, and Flash already produced zero-error TypeScript
    for a module of this style.
    """

    planner: str
    """Reads the whole gap report and chooses the next increment.

    Called once per increment and cheap in aggregate, but high leverage: a wrong choice
    wastes an entire porter run. This is exactly where the expensive model pays for itself.
    """

    porter: str
    """Bulk translation of one module, source language to target language.

    Almost all the tokens in the project pass through this tier, so it dominates cost. The
    spike showed Flash is sufficient here -- upstream is the specification, and the work is
    faithful transcription rather than invention.
    """

    repairer: str
    """Turns a real failure report into a fix.

    Deliberately a tier above the porter. Repair is where Flash plateaus: the input is a
    type error or a failed assertion whose cause is often several modules away from where
    it surfaced. Note this consumes REAL verifier output, never self-critique -- refinement
    loops only improve with external information (Huang et al., ICLR 2024).
    """

    triager: str
    """Classifies a failure as mechanical or semantic, in one short call.

    A cheap gate in front of an expensive decision: routing every failure to the repairer
    would spend Pro tokens on missing semicolons.
    """


MODELS = ModelTiers(
    planner="gemini-3.1-pro-preview",
    porter="gemini-3.6-flash",
    repairer="gemini-3.1-pro-preview",
    triager="gemini-3.1-flash-lite",
)


# --- loop tunables ---------------------------------------------------------------------
# The verify/repair loop needs a stopping rule. Both limits below exist because an agent
# with no budget will keep trying variations of a fix that cannot work.

MAX_REPAIR_ROUNDS = 5
"""Hard ceiling on verify/repair iterations before the loop gives up and reports."""

STALL_LIMIT = 2
"""Rounds with no measured improvement before the loop stops.

Set low on purpose. A repair that does not move the oracle is not partial progress; it is
evidence the diagnosis is wrong, and another round of the same diagnosis will not help.
"""


# --- context window --------------------------------------------------------------------
# A port session grows much faster than a conversation. A single tool result can be a 20 KB
# module, and the repair loop produces up to MAX_REPAIR_ROUNDS of them per increment.

COMPACTION_INTERVAL = 20
"""Events between compactions.

Roughly one increment's worth: plan, two analyses, a write, and a few verify/repair rounds.
Compacting mid-increment would summarise away a failure the repairer is still working on.
"""

COMPACTION_OVERLAP = 3
"""Events shared across a compaction boundary.

Without overlap the summary starts exactly where the previous one stopped, and a fix that
spans the seam loses the failure it was responding to. Three events is enough to carry a
tool call, its result, and the reasoning that followed.
"""
