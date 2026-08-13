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

"""Tests for the graph's structure and its stopping rule.

Structure is worth testing because it is a claim: that the planner cannot write, that the
loop can end, that the cheap model does the bulk work. Each of those silently stops being
true the moment someone adds a tool to the wrong list.

No model is called here. The stopping rule contains no model at all, which is the point of
it, so it can be tested exactly.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import escalation
from app.agent import analysts, planner, port_increment, porter, repairer, root_agent
from app.config import MAX_REPAIR_ROUNDS, MODELS, STALL_LIMIT
from app.escalation import BEST_SEEN, STALL_COUNT, StopWhenStalled, current_score


# --- the shape of the graph -------------------------------------------------


def test_the_planner_cannot_write() -> None:
    # The separation that makes the plan reviewable: if the planner could write, a plan and
    # an edit would arrive together with no verification between them.
    names = {getattr(t, "__name__", getattr(t, "name", "")) for t in planner.tools}
    assert names == {"verify_ported_interpreter"}


def test_the_analysts_cannot_write() -> None:
    for analyst in analysts.sub_agents:
        for tool in analyst.tools:
            name = getattr(tool, "__name__", getattr(tool, "name", ""))
            assert not name.startswith(("write_", "edit_")), f"{analyst.name} can write"


def test_the_analysts_read_different_things() -> None:
    # Parallel is only justified when the work is independent. If both analysts read the
    # same corpus, one of them is redundant and the fan-out is decoration.
    upstream, conventions = (
        {getattr(t, "__name__", "") for t in a.tools} for a in analysts.sub_agents
    )
    assert not upstream & conventions


def test_bulk_translation_runs_on_the_cheap_tier() -> None:
    # Almost every token in the project passes through the porter, so this is where the
    # routing decision actually shows up on the bill.
    assert porter.model.model == MODELS.porter
    assert repairer.model.model == MODELS.repairer
    assert planner.model.model == MODELS.planner


def test_the_repair_loop_can_end() -> None:
    # Two independent ways out: the stopping rule escalates, and the loop has a hard
    # ceiling. Either alone is one bug away from an agent that runs until it is killed.
    loop = port_increment.sub_agents[-1]
    assert loop.max_iterations == MAX_REPAIR_ROUNDS
    assert any(isinstance(a, StopWhenStalled) for a in loop.sub_agents)


def test_the_coordinator_delegates_rather_than_ports() -> None:
    assert not root_agent.tools
    assert [a.name for a in root_agent.sub_agents] == ["port_increment"]


# --- the stopping rule ------------------------------------------------------


def _run(agent: StopWhenStalled, state: dict):
    ctx = SimpleNamespace(
        session=SimpleNamespace(state=state), invocation_id="inv-1", branch=None
    )

    async def collect():
        return [event async for event in agent._run_async_impl(ctx)]

    events = asyncio.run(collect())
    assert len(events) == 1
    return events[0]


@pytest.fixture()
def stall_check() -> StopWhenStalled:
    return StopWhenStalled(name="stall_check", description="test")


@pytest.fixture()
def report(tmp_path, monkeypatch):
    """Writes a conformance report where the stopping rule will look for it."""
    path = tmp_path / "conformance.json"
    monkeypatch.setattr(escalation, "CONFORMANCE_REPORT", path)

    def write(assertions: int, probes: int = 0) -> None:
        path.write_text(json.dumps({
            "conformance": {"assertionsPassed": assertions},
            "ladder": {"passed": probes},
        }))

    return SimpleNamespace(write=write, path=path)


def test_improvement_resets_patience(stall_check, report) -> None:
    report.write(assertions=358, probes=49)
    event = _run(stall_check, {BEST_SEEN: [261, 49], STALL_COUNT: 1})

    assert event.actions.escalate is False
    assert event.actions.state_delta[BEST_SEEN] == [358, 49]
    assert event.actions.state_delta[STALL_COUNT] == 0


def test_more_probes_at_equal_assertions_counts_as_progress(stall_check, report) -> None:
    # Assertions rank first and probes break the tie, matching how the best-port snapshot
    # scores runs. Two definitions of "better" would let the loop and the snapshot disagree.
    report.write(assertions=358, probes=50)
    assert _run(stall_check, {BEST_SEEN: [358, 49]}).actions.escalate is False


def test_a_stalled_round_spends_patience_but_does_not_stop_yet(stall_check, report) -> None:
    report.write(assertions=358, probes=49)
    event = _run(stall_check, {BEST_SEEN: [358, 49], STALL_COUNT: 0})

    assert event.actions.escalate is False
    assert event.actions.state_delta[STALL_COUNT] == 1


def test_the_loop_stops_at_the_limit(stall_check, report) -> None:
    report.write(assertions=358, probes=49)
    event = _run(stall_check, {BEST_SEEN: [358, 49], STALL_COUNT: STALL_LIMIT - 1})

    assert event.actions.escalate is True
    assert "Stopping" in event.content.parts[0].text


def test_going_backwards_is_not_progress(stall_check, report) -> None:
    report.write(assertions=5, probes=12)
    event = _run(stall_check, {BEST_SEEN: [358, 49], STALL_COUNT: 0})

    assert event.actions.state_delta[STALL_COUNT] == 1
    assert BEST_SEEN not in event.actions.state_delta, "a worse run must not become the best"


def test_the_first_round_has_nothing_to_compare_against(stall_check, report) -> None:
    report.write(assertions=40, probes=8)
    event = _run(stall_check, {})

    assert event.actions.escalate is False
    assert event.actions.state_delta[BEST_SEEN] == [40, 8]


def test_a_missing_report_counts_as_no_progress(stall_check, monkeypatch, tmp_path) -> None:
    # The loop must not stall forever waiting for a measurement that never arrives.
    monkeypatch.setattr(escalation, "CONFORMANCE_REPORT", tmp_path / "absent.json")
    event = _run(stall_check, {STALL_COUNT: STALL_LIMIT - 1})
    assert event.actions.escalate is True


def test_a_half_written_report_is_not_read_as_a_score(monkeypatch, tmp_path) -> None:
    # The runner writes this file while the loop may be reading it.
    path = tmp_path / "conformance.json"
    path.write_text('{"conformance": {"assertionsPas')
    monkeypatch.setattr(escalation, "CONFORMANCE_REPORT", path)
    assert current_score() is None
