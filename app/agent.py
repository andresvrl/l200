# ruff: noqa
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

"""Walking skeleton: the thinnest end-to-end port loop.

Phase 1 of the plan. One agent, six tools, no memory, no routing, no guardrails --
its only job is to prove the loop closes: read the gap report, write TypeScript, verify
against the oracle, repair from real failures, and watch the score rise.

If this does not work, the architecture is wrong and we re-scope here rather than after
building ten steps of infrastructure on top of it.
"""

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
import logging
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)
from google.cloud import bigquery

from .tools import (
    edit_ported_typescript_module,
    list_upstream_go_modules,
    read_ported_typescript_module,
    read_upstream_go_source,
    restore_best_port,
    verify_ported_interpreter,
    write_ported_typescript_module,
)


MODEL = "gemini-3.6-flash"


SKELETON_INSTRUCTION = """\
You port the Starlark interpreter from Go to TypeScript, one increment at a time, guided
by an executable conformance oracle.

# How you work

1. Call `verify_ported_interpreter` FIRST, every session. It tells you the measured state
   and what to do next. Never guess at what is failing.
2. Choose the next increment by VALUE, using two signals:
   - `immediate_work` -- failing probes in the lowest incomplete ladder tier. Tiers build
     on each other, so a later tier passing while an earlier one fails is an accident.
   - `conformance_blockers` -- single defects grouped across upstream files, each labelled
     with how many assertions it gates.
   Ladder probes are our own progress signal; only the upstream `.star` suite establishes
   conformance. So when a blocker gates hundreds of assertions, fix it before a cosmetic
   probe, even if the probe sits in a lower tier.
3. Read the relevant Go source before porting semantics you are unsure of. Upstream is the
   specification.
4. For a NEW module use `write_ported_typescript_module`. For any change to an existing
   module use `read_ported_typescript_module` then `edit_ported_typescript_module` --
   rewriting a whole file to fix a few lines is slow and regularly regresses code that was
   already correct. Verify immediately after either; type checking takes under a second
   while generation takes minutes, so never batch writes before verifying.
5. Repeat until the target tier passes, or until you have made three consecutive attempts
   with no improvement -- at which point stop and report precisely what is blocking you.

# Non-negotiables

- `ported/index.ts` MUST export `execFile(filename, src, predeclared, thread)` and return
  the module's global bindings. This is defined in `harness/contract.ts`.
- Starlark integers are ARBITRARY PRECISION. Represent them as TypeScript `bigint`, never
  `number`. Using `number` passes early tests and fails later ones, which is the worst
  possible failure mode.
- Go strings are BYTE sequences; TypeScript strings are UTF-16. Where upstream operates on
  bytes, use `Uint8Array` and handle the encoding explicitly.
- Value mapping is fixed by the contract: None -> null, bool -> boolean, int -> bigint,
  float -> number, string -> string, bytes -> Uint8Array, list -> array, tuple -> Tuple,
  dict -> Map, set -> Set.
- Relative imports MUST carry an explicit `.js` extension, because the output runs under
  Node's ESM loader: write `from "./eval.js"`, never `from "./eval"`. The compiler enforces
  this, and getting it wrong means the port type-checks but fails to load, scoring zero
  everywhere with no obvious cause.
- Error message text is observable behaviour. Upstream tests assert on it, so preserve it.
- You may write ONLY inside `ported/`. Never modify the harness, the vendored conformance
  suite, or the agent itself. If a conformance test looks wrong, the port is wrong.
- The code must type-check under `strict` with `noUncheckedIndexedAccess`. Do not weaken
  the TypeScript configuration; the type checker is the cheapest verifier available.

# Reporting

When you stop, state the measured numbers -- probes passed, tiers clean, assertions earned
-- and what the next increment should be. Do not describe the port as working unless the
oracle says so.
"""


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=SKELETON_INSTRUCTION,
    tools=[
        verify_ported_interpreter,
        list_upstream_go_modules,
        read_upstream_go_source,
        write_ported_typescript_module,
        read_ported_typescript_module,
        edit_ported_typescript_module,
        restore_best_port,
    ],
)
import os

# Initialize BigQuery Analytics
_plugins = []
_project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
_dataset_id = os.environ.get("BQ_ANALYTICS_DATASET_ID", "adk_agent_analytics")
_location = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

if _project_id:
    try:
        bq = bigquery.Client(project=_project_id)
        bq.create_dataset(f"{_project_id}.{_dataset_id}", exists_ok=True)

        _plugins.append(
            BigQueryAgentAnalyticsPlugin(
                project_id=_project_id,
                dataset_id=_dataset_id,
                location=_location,
                config=BigQueryLoggerConfig(
                    gcs_bucket_name=os.environ.get("BQ_ANALYTICS_GCS_BUCKET"),
                    connection_id=os.environ.get("BQ_ANALYTICS_CONNECTION_ID"),
                ),
            )
        )
    except Exception as e:
        logging.warning(f"Failed to initialize BigQuery Analytics: {e}")

app = App(
    root_agent=root_agent,
    name="app",
    plugins=_plugins,
)
