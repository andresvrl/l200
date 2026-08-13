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

"""Tests for what gets remembered, and for the fact that nobody waits for it.

Async fire-and-forget is easy to write and easy to get wrong in ways that never raise: a
task that is garbage-collected mid-flight, or a memory failure that takes down the
increment that had already succeeded. Both are covered below.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.memory import MEMORY_TOPIC, _distil, remember_port_conventions


class _FakeContext:
    """Stands in for an ADK callback context, recording what memory received."""

    def __init__(self, state: dict | None = None, fail: bool = False) -> None:
        self.state = state or {}
        self.written: list = []
        self._fail = fail

    def add_memory(self, *, memories, custom_metadata=None) -> None:
        if self._fail:
            raise RuntimeError("memory bank unavailable")
        self.written.extend(memories)


async def _drain() -> None:
    """Hands control to the event loop so the scheduled write can finish."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def _run_and_drain(context) -> None:
    """Calls the callback once, then lets its background write complete."""
    remember_port_conventions(context)
    await _drain()


_STATE = {
    "port_plan": "TARGET MODULE: syntax/parser.ts",
    "port_conventions": "Integers are IntValue wrapping bigint; errors use StarlarkError.",
}


def test_the_callback_returns_before_the_write_completes() -> None:
    # The property that makes this worth doing at all. If it awaited, every increment
    # would pay a round trip to Memory Bank for something no one is waiting to read.
    async def scenario():
        context = _FakeContext(_STATE)
        remember_port_conventions(context)
        assert context.written == [], "the callback awaited the write"
        await _drain()
        assert len(context.written) == 1, "and then the write must actually happen"

    asyncio.run(scenario())


def test_the_callback_does_not_override_the_agent_output() -> None:
    # ADK treats a non-None return from after_agent_callback as a replacement response.
    async def scenario():
        assert remember_port_conventions(_FakeContext(_STATE)) is None

    asyncio.run(scenario())


def test_what_is_written_is_the_conventions_not_the_transcript() -> None:
    async def scenario():
        context = _FakeContext(_STATE)
        await _run_and_drain(context)
        entry = context.written[0]
        text = entry.content.parts[0].text
        assert "IntValue" in text
        assert entry.custom_metadata["topic"] == MEMORY_TOPIC

    asyncio.run(scenario())


def test_memory_is_redacted_on_the_way_out() -> None:
    # Memory outlives the run, so an absolute path written here leaks for longer than a
    # log line does.
    async def scenario():
        context = _FakeContext({
            "port_plan": "TARGET MODULE: /home/someuser/l200/ported/eval.ts",
            "port_conventions": "token = abcdefghijklmnop",
        })
        await _run_and_drain(context)
        text = context.written[0].content.parts[0].text
        assert "someuser" not in text
        assert "abcdefghijklmnop" not in text

    asyncio.run(scenario())


def test_a_memory_failure_does_not_fail_the_increment() -> None:
    # The port is the product; memory is an optimisation. Losing a note costs consistency
    # on the next module. Raising would cost work that was already finished and verified.
    async def scenario():
        context = _FakeContext(_STATE, fail=True)
        await _run_and_drain(context)  # must not raise

    asyncio.run(scenario())


def test_nothing_is_written_when_there_is_nothing_to_say() -> None:
    async def scenario():
        context = _FakeContext({})
        await _run_and_drain(context)
        assert context.written == []

    asyncio.run(scenario())


def test_without_an_event_loop_it_declines_rather_than_crashing() -> None:
    # Called from a synchronous host or a plain unit test there is nothing to schedule
    # onto. Declining is correct; raising would make memory a hard dependency.
    assert remember_port_conventions(_FakeContext(_STATE)) is None


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, ""),
        ({"port_plan": "   "}, ""),
        ({"port_plan": "port int.ts"}, "Increment: port int.ts"),
    ],
)
def test_distil_skips_empty_fields(state, expected) -> None:
    assert _distil(SimpleNamespace(state=state)) == expected


def test_distil_is_bounded() -> None:
    # A session can carry whole source files. An unbounded note would make recall return a
    # wall of code with the one useful sentence invisible inside it.
    huge = _distil(SimpleNamespace(state={"port_plan": "x" * 20000}))
    assert len(huge) <= 4000
