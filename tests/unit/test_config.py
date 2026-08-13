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

"""Tests that the port target is the single source of truth.

Configuration bugs are quiet. A stale path or a convention that drifted out of the prompt
does not raise -- it just makes the agent slightly wrong for the rest of the run. These
check the couplings that would otherwise only show up in a live session.
"""

from __future__ import annotations

import dataclasses

from app.config import MODELS, TARGET, PortTarget
from app.tools import port_tools


def test_tools_read_their_paths_from_the_target() -> None:
    # If these drift apart, the agent writes to one directory and the oracle reads another.
    assert port_tools.PORTED == TARGET.output_dir
    assert port_tools.UPSTREAM == TARGET.upstream_dir


def test_the_contract_the_prompt_promises_actually_exists() -> None:
    # The prompt tells the agent the contract defines its entry point. A stale path here
    # would send it looking for a file that is not there.
    assert TARGET.contract_path.is_file()
    assert TARGET.entry_signature.split("(")[0] in TARGET.contract_path.read_text()


def test_every_convention_states_a_consequence() -> None:
    # A rule without a consequence reads as a preference, and preferences get traded away.
    # Each convention must say what breaking it costs.
    for rule in TARGET.conventions:
        assert len(rule) > 80, f"too terse to carry a consequence: {rule!r}"


def test_retargeting_is_one_object() -> None:
    # The reusability claim, made concrete: a different port is a different PortTarget,
    # with no other change required to construct it.
    rust = dataclasses.replace(
        TARGET, name="other", target_language="Rust", output_suffix=".rs", entry_module="lib.rs"
    )
    assert rust.output_suffix == ".rs"
    assert TARGET.output_suffix == ".ts", "the active target must be unaffected"
    assert isinstance(rust, PortTarget)


def test_the_cascade_actually_cascades() -> None:
    # Routing every step to one model is not routing. The porter carries almost all the
    # tokens and must be cheaper than the tier that repairs its mistakes.
    assert MODELS.porter != MODELS.repairer, "no escalation means no cascade"
    assert MODELS.triager != MODELS.planner, "a triage gate must be cheaper than what it gates"
