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

"""Memory of the conventions this port has already committed to.

A port is a long sequence of local decisions that must agree with each other. The porter
sees one module at a time, so without memory it writes a second ``isTruthy`` beside the
first with slightly different behaviour, and the difference surfaces forty assertions later
as an unexplained failure.

What is worth remembering is therefore narrow and specific: the decisions that constrain
future modules. How integers are represented, what the error type is called, which helper
already exists. Not the transcript.

This module owns the policy -- *what* is written and *when*. Where it is kept is decided
in ``app_utils/services.py`` alongside the session and artifact services, since that is one
question ("managed backend or in-process?") that should have one answer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from .config import TARGET
from .redaction import redact

logger = logging.getLogger("starport")

# Tag written onto every entry so a recall can ask for conventions specifically, and so a
# second port target's memories never come back for this one.
MEMORY_TOPIC = f"port-conventions:{TARGET.name}"


# Strong references to in-flight writes. Without this the event loop keeps only a weak
# reference to a bare `create_task` result, and a memory write can be garbage-collected
# halfway through -- a fire-and-forget bug that shows up as memories that intermittently
# fail to appear, which is close to impossible to reproduce on purpose.
_IN_FLIGHT: set[asyncio.Task[Any]] = set()


async def _write_conventions(callback_context: Any, note: str) -> None:
    """Performs the actual memory write. Never raises into the caller's turn."""
    try:
        # add_memory is a coroutine. Calling it without awaiting produces a coroutine
        # object, a RuntimeWarning, and no memory -- silently, since nothing raises. Found
        # by running a real increment, not by the tests, whose fake context was synchronous.
        await callback_context.add_memory(
            memories=[
                MemoryEntry(
                    author="porter",
                    content=types.Content(role="model", parts=[types.Part(text=note)]),
                    custom_metadata={"topic": MEMORY_TOPIC},
                )
            ],
            custom_metadata={"topic": MEMORY_TOPIC},
        )
    except Exception as error:  # noqa: BLE001 -- a memory failure must not fail the port.
        # Memory is an optimisation. Losing a note costs consistency on the next module;
        # raising here would cost the increment that has already been completed.
        logger.warning(
            "memory write failed", extra={"fields": {"event": "memory.write_failed",
                                                     "error": str(error)[:200]}}
        )


def remember_port_conventions(callback_context: Any) -> None:
    """Records what this increment established, without making anyone wait for it.

    Wired as the porter's ``after_agent_callback``. By the time it runs the code is already
    written and verified, so the write has nothing to contribute to the current turn --
    blocking on a round trip to Memory Bank would add latency to every increment and buy
    nothing.

    Scheduling rather than awaiting is the whole point. The task is kept in a module-level
    set so it cannot be collected before it finishes.

    Args:
        callback_context: ADK callback context for the agent that just finished. Provides
            the memory handle and the session whose output is being recorded.

    Returns:
        None, and immediately. ADK treats a ``None`` return as "no override", so the
        agent's own output is passed through untouched.
    """
    note = _distil(callback_context)
    if not note:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop: a synchronous host or a plain unit test. Nothing to schedule
        # onto, and a memory note is not worth spinning up a loop for. Checked BEFORE
        # building the coroutine, since an un-awaited coroutine object is itself a warning.
        return

    task = loop.create_task(_write_conventions(callback_context, note))
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)


def _distil(callback_context: Any) -> str:
    """Extracts the durable part of what just happened, redacted.

    Deliberately not the transcript. A session carries whole source files, and storing
    those would make recall return a wall of code in which the one useful sentence is
    invisible -- while also multiplying the chance of writing something sensitive into a
    store that outlives the run.
    """
    state = getattr(callback_context, "state", {}) or {}
    parts = [
        f"Increment: {state.get('port_plan', '')}",
        f"Conventions in force: {state.get('port_conventions', '')}",
    ]
    note = "\n".join(p for p in parts if p.split(": ", 1)[-1].strip())
    return redact(note)[:4000] if note else ""
