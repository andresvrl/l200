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

"""The repair loop's stopping rule, as a small agent that contains no model.

A ``LoopAgent`` needs something to decide when to stop. Asking the repairer to decide puts
the question to the party least able to answer it: a model that has just spent a round on a
fix is poorly placed to judge whether the fix worked.

So the decision is code, and it reads the oracle's own output file rather than the model's
account of it. That is the same principle the whole project runs on -- an external verifier
outranks a plausible claim -- applied to the loop's own control flow.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types

from .config import ROOT, STALL_LIMIT
from .tools.port_tools import score_report

CONFORMANCE_REPORT = ROOT / "reports" / "conformance.json"

# Session-state keys. Namespaced so they cannot collide with anything an agent writes.
BEST_SEEN = "loop:best_score"
STALL_COUNT = "loop:stalls"


def current_score() -> tuple[int, int] | None:
    """Reads the latest conformance run, or None if there is not one yet.

    Returns:
        The run's score as ranked by :func:`app.tools.port_tools.score_report`, or None
        when no report exists -- which is the normal state before the first verification.
    """
    if not CONFORMANCE_REPORT.exists():
        return None
    try:
        return score_report(json.loads(CONFORMANCE_REPORT.read_text()))
    except (json.JSONDecodeError, KeyError):
        # A truncated or half-written report means the run did not finish. Treating that
        # as "no progress" is right: an unreadable measurement is not an improvement.
        return None


class StopWhenStalled(BaseAgent):
    """Ends the repair loop once the oracle stops moving.

    Runs after each repair round and compares the new score against the best seen in this
    session. An improvement resets the patience counter; anything else spends one. After
    :data:`app.config.STALL_LIMIT` unproductive rounds it escalates, which is how a
    ``LoopAgent`` is told to stop.

    The limit is deliberately small. A repair that does not move the oracle is not partial
    progress -- it is evidence the diagnosis was wrong, and the next round will be built on
    the same wrong diagnosis.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        score = current_score()
        best = ctx.session.state.get(BEST_SEEN)
        stalls = ctx.session.state.get(STALL_COUNT, 0)

        improved = score is not None and (best is None or list(score) > list(best))

        if improved:
            assertions, probes = score  # type: ignore[misc]
            state_delta = {BEST_SEEN: list(score), STALL_COUNT: 0}  # type: ignore[arg-type]
            escalate = False
            note = f"improved: {assertions} assertions, {probes} probes. Continuing."
        else:
            stalls += 1
            state_delta = {STALL_COUNT: stalls}
            escalate = stalls >= STALL_LIMIT
            measured = "no measurement" if score is None else f"{score[0]} assertions"
            note = (
                f"no improvement ({measured}), {stalls} of {STALL_LIMIT}. "
                + ("Stopping." if escalate else "One more round.")
            )

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            branch=ctx.branch,
            content=types.Content(role="model", parts=[types.Part(text=note)]),
            actions=EventActions(state_delta=state_delta, escalate=escalate),
        )
