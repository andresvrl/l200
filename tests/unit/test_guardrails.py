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

"""Tests for the policy layer.

A guardrail is only as good as its false-positive rate. A rule that blocks everything
passes every "did it block?" test and makes the agent useless, so each blocking case here
is paired with the legitimate call it must still allow.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.plugins.guardrails import (
    GuardrailPlugin,
    build_tools,
    rewrites_a_module_that_is_already_earning,
)


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _before(plugin: GuardrailPlugin, tool_name: str, **args):
    return asyncio.run(
        plugin.before_tool_callback(
            tool=_tool(tool_name), tool_args=args, tool_context=SimpleNamespace()
        )
    )


def _after(plugin: GuardrailPlugin, tool_name: str, result):
    return asyncio.run(
        plugin.after_tool_callback(
            tool=_tool(tool_name), tool_args={}, tool_context=SimpleNamespace(), result=result
        )
    )


@pytest.fixture()
def plugin() -> GuardrailPlugin:
    return GuardrailPlugin()


# --- the oracle is off limits -----------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "harness/contract.ts",
        "vendor/starlark-testdata/bool.star",
        "../harness/ladder/probes.ts",
        "app/agent.py",
        "reports/best.json",
        "./tests/unit/test_port_tools.py",
    ],
)
def test_writes_to_the_grader_are_refused(plugin, path) -> None:
    blocked = _before(plugin, "write_ported_typescript_module", relative_path=path,
                      source_code="export const x = 1;")
    assert blocked is not None, f"{path} should not be writable"
    assert blocked["error_code"] == "protected_path"
    assert "grader" in blocked["recovery_hint"]


@pytest.mark.parametrize("path", ["index.ts", "syntax/parser.ts", "starlark/int.ts"])
def test_ordinary_writes_are_allowed(plugin, path) -> None:
    # The rule is worthless if it also blocks the agent's actual job.
    assert _before(plugin, "write_ported_typescript_module", relative_path=path,
                   source_code="export const x = 1;") is None


def test_reading_the_harness_is_allowed(plugin) -> None:
    # Read-only access to the oracle is legitimate; only writing is not.
    assert _before(plugin, "read_upstream_go_source", module_path="syntax/scan.go") is None


def test_non_string_arguments_do_not_crash_the_check(plugin) -> None:
    assert _before(plugin, "edit_ported_typescript_module", relative_path="eval.ts",
                   line_number=42, dry_run=True) is None


# --- regressions block, they do not warn ------------------------------------


def test_a_regression_becomes_an_error(plugin) -> None:
    # The incident: a local fix cost 228 assertions, the warning was advisory, and the run
    # continued building on top of the damage.
    result = {
        "status": "ok",
        "regression_warning": "This version earns 5 assertions; the best recorded is 233.",
        "ladder": {"passed": 12}, "conformance": {"assertionsPassed": 5},
    }
    blocked = _after(plugin, "verify_ported_interpreter", result)
    assert blocked["status"] == "error"
    assert blocked["error_code"] == "regression_blocked"
    assert "restore_best_port" in blocked["recovery_hint"]


def test_a_blocked_regression_still_carries_the_measurements(plugin) -> None:
    # The model has to be able to explain what was traded away, which needs the numbers.
    blocked = _after(plugin, "verify_ported_interpreter", {
        "status": "ok", "regression_warning": "lost ground",
        "ladder": {"passed": 12}, "conformance": {"assertionsPassed": 5},
    })
    assert blocked["conformance"]["assertionsPassed"] == 5
    assert blocked["ladder"]["passed"] == 12


def test_progress_is_left_alone(plugin) -> None:
    good = {"status": "ok", "is_best_so_far": True, "conformance": {"assertionsPassed": 358}}
    assert _after(plugin, "verify_ported_interpreter", good) is None


def test_a_non_dict_result_is_not_treated_as_a_regression(plugin) -> None:
    assert _after(plugin, "verify_ported_interpreter", "some string") is None


# --- what needs a human -----------------------------------------------------


def test_new_modules_do_not_need_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.port_tools.BEST_DIR", tmp_path)
    assert rewrites_a_module_that_is_already_earning(relative_path="brand_new.ts") is False


def test_overwriting_a_module_that_is_already_earning_needs_approval(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.tools.port_tools.BEST_DIR", tmp_path)
    (tmp_path / "eval.ts").write_text("export const working = true;")
    assert rewrites_a_module_that_is_already_earning(relative_path="eval.ts") is True


def test_the_predicate_tolerates_the_full_tool_arguments(tmp_path, monkeypatch) -> None:
    # ADK calls the predicate with the same keyword arguments as the tool itself, so it
    # must accept source_code without complaining.
    monkeypatch.setattr("app.tools.port_tools.BEST_DIR", tmp_path)
    assert rewrites_a_module_that_is_already_earning(
        relative_path="new.ts", source_code="export const x = 1;"
    ) is False


def test_destructive_tools_are_gated_and_the_rest_are_not() -> None:
    tools = build_tools()
    gated = {getattr(t, "name", getattr(t, "__name__", "")): t for t in tools
             if hasattr(t, "_require_confirmation")}

    assert gated["restore_best_port"]._require_confirmation is True
    assert callable(gated["write_ported_typescript_module"]._require_confirmation)
    # Everything else is a plain function, so it runs without interrupting a human.
    assert len(gated) == 2, "only genuinely destructive actions should stop the run"


def test_measurement_comes_first_in_the_tool_list() -> None:
    # Ordering is a hint the model reads. The first tool should be the one that tells it
    # where it actually stands.
    first = build_tools()[0]
    assert getattr(first, "__name__", "") == "verify_ported_interpreter"
