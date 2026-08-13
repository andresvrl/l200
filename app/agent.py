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

"""The port graph.

::

    port_coordinator .................. talks to the human, owns the goal      [Pro]
     └── port_increment (Sequential) ... one increment, start to finish
          ├── planner ................. reads the gap report, picks ONE thing  [Pro]
          ├── analysts (Parallel) ..... two independent reads
          │    ├── upstream_analyst ... what upstream actually specifies       [Flash]
          │    └── convention_analyst . what the existing port already does    [Flash-Lite]
          ├── porter .................. writes the code                        [Flash]
          └── verify_and_repair (Loop)  until the oracle stops moving
               ├── repairer ........... fixes what the oracle reports          [Pro]
               └── StopWhenStalled .... code, not a model, decides when to quit

Why this shape rather than one capable agent:

* **Separate roles because they need separate tools.** The planner has no write tool, so a
  plan cannot silently become an edit. The analysts cannot write at all.
* **Parallel where the work is genuinely independent.** The two analysts read different
  corpora -- upstream Go and the existing TypeScript -- and neither needs the other's
  output. Splitting them also keeps each context small, which is the practical form of the
  Lost-in-the-Middle result: a long context degrades recall of material in its middle.
* **A loop because repair is iterative, with a stopping rule that is code.** The loop
  consumes real verifier output every round, never self-critique. Refinement improves only
  with external information (Huang et al., ICLR 2024); a model asked to review its own work
  produces confident revisions and no gain.
* **Models by tier.** Bulk translation is faithful transcription and Flash does it well;
  choosing what to port next, and repairing a failure whose cause is three modules away
  from where it surfaced, is where the expensive tier earns its cost. See app/config.py.
"""

import logging
import os

# ADK 2.6 marks SequentialAgent/ParallelAgent/LoopAgent as deprecated in favour of
# google.adk.workflow.Workflow. We stay with them deliberately: the same deprecation notice
# records that a Workflow cannot yet be a sub-agent of an LlmAgent, and a conversational
# coordinator delegating to a pipeline is exactly this graph. Revisit when that lands.
from google.adk.agents import LlmAgent, LoopAgent, ParallelAgent, SequentialAgent
from google.adk.apps import App
from google.adk.apps._configs import EventsCompactionConfig
from google.adk.models import Gemini
from google.adk.tools import load_memory
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)
from google.cloud import bigquery
from google.genai import types

from .config import COMPACTION_INTERVAL, COMPACTION_OVERLAP, MAX_REPAIR_ROUNDS, MODELS
from .escalation import StopWhenStalled
from .memory import remember_port_conventions
from .plugins import GuardrailPlugin, ObservabilityPlugin, build_tools
from .plugins.guardrails import MEASURE_TOOLS, READ_PORT_TOOLS, READ_UPSTREAM_TOOLS, WRITE_TOOL
from .prompts import (
    CONVENTION_ANALYST,
    COORDINATOR,
    PLANNER,
    PORTER,
    REPAIRER,
    UPSTREAM_ANALYST,
)
from .tools import port_tools


def _model(name: str) -> Gemini:
    """Builds a model client with retries.

    Retries matter more here than in a chat agent: a single porter call can be several
    minutes of generation, so losing one to a transient error is expensive.
    """
    return Gemini(model=name, retry_options=types.HttpRetryOptions(attempts=3))


# --- the roles -------------------------------------------------------------------------

planner = LlmAgent(
    name="planner",
    description="Reads the measured gap report and chooses the single next increment.",
    model=_model(MODELS.planner),
    instruction=PLANNER,
    tools=MEASURE_TOOLS,
    output_key="port_plan",
)

upstream_analyst = LlmAgent(
    name="upstream_analyst",
    description="Describes the upstream surface the increment must reproduce.",
    model=_model(MODELS.porter),
    instruction=UPSTREAM_ANALYST,
    tools=READ_UPSTREAM_TOOLS,
    output_key="upstream_surface",
)

convention_analyst = LlmAgent(
    name="convention_analyst",
    description="States the conventions the existing ported code already follows.",
    model=_model(MODELS.triager),
    instruction=CONVENTION_ANALYST,
    # Reads the code AND recalls what earlier increments decided. The code shows what was
    # written; memory carries the reasoning, which is the part that does not survive in
    # a diff -- "int is bigint" is visible, "and here is why float comparison is separate"
    # is not.
    tools=[*READ_PORT_TOOLS, load_memory],
    output_key="port_conventions",
)

analysts = ParallelAgent(
    name="analysts",
    description="Reads upstream and the existing port at the same time.",
    sub_agents=[upstream_analyst, convention_analyst],
)

porter = LlmAgent(
    name="porter",
    description="Writes the target-language code for the planned increment.",
    model=_model(MODELS.porter),
    instruction=PORTER,
    tools=[*MEASURE_TOOLS, *READ_PORT_TOOLS, port_tools.edit_ported_typescript_module, WRITE_TOOL],
    # Records what this increment established, in the background. The porter is finished by
    # the time this runs, so awaiting a round trip to Memory Bank would add latency to
    # every increment and change nothing about the result.
    after_agent_callback=remember_port_conventions,
)

repairer = LlmAgent(
    name="repairer",
    description="Fixes what the oracle reports, one root cause per round.",
    model=_model(MODELS.repairer),
    instruction=REPAIRER,
    tools=build_tools(),
)

verify_and_repair = LoopAgent(
    name="verify_and_repair",
    description="Repairs and re-verifies until the oracle stops improving.",
    sub_agents=[
        repairer,
        # The stopping rule runs after each repair, reads the report file the oracle just
        # wrote, and escalates when the score has not moved. See app/escalation.py.
        StopWhenStalled(
            name="stall_check",
            description="Ends the loop when a round produces no measured improvement.",
        ),
    ],
    max_iterations=MAX_REPAIR_ROUNDS,
)

port_increment = SequentialAgent(
    name="port_increment",
    description="Plans, ports, verifies and repairs one increment of the port.",
    sub_agents=[planner, analysts, porter, verify_and_repair],
)

root_agent = LlmAgent(
    name="port_coordinator",
    description="Owns the port, runs one increment at a time, and reports to the human.",
    model=_model(MODELS.planner),
    instruction=COORDINATOR,
    sub_agents=[port_increment],
)


# --- the app ---------------------------------------------------------------------------

# BigQuery analytics comes from the scaffold and is optional: without a project configured
# the agent still runs, it just has nowhere to ship session analytics.
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

# Plugin order is meaningful. Observability goes first so it records the INTENT of a call
# before the guardrail can block it -- a refused call is exactly the one worth having in
# the log, and a plugin that only sees permitted calls cannot show you what was stopped.
app = App(
    root_agent=root_agent,
    name="app",
    plugins=[ObservabilityPlugin(), GuardrailPlugin(), *_plugins],
    # A port session grows faster than a conversation does: one repair round can carry a
    # 20 KB module in a tool result, and the loop runs up to five of them. Compaction
    # summarises older events so the window holds the current increment rather than the
    # source of a module that was finished an hour ago. The overlap is what keeps it safe --
    # a summary boundary with no shared events loses the thread across the seam.
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=COMPACTION_INTERVAL,
        overlap_size=COMPACTION_OVERLAP,
    ),
)
