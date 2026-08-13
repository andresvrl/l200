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

"""Tests for the observability plugin.

The plugin's hooks are called directly with stand-ins rather than through a Runner, so
these stay deterministic and make no model calls. What matters is the shape of what gets
logged: an intent line before the call, a matching outcome line after it, and nothing
sensitive in either.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from app.plugins.observability import ObservabilityPlugin


class _CapturingHandler(logging.Handler):
    """Collects formatted log lines so their JSON can be inspected."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture()
def plugin_and_logs():
    plugin = ObservabilityPlugin()
    handler = _CapturingHandler()
    handler.setFormatter(plugin._log.handlers[0].formatter)
    plugin._log.addHandler(handler)
    yield plugin, handler
    plugin._log.removeHandler(handler)


def _tool(name: str = "verify_ported_interpreter") -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _context(invocation_id: str = "inv-1") -> SimpleNamespace:
    return SimpleNamespace(invocation_id=invocation_id)


def _events(handler: _CapturingHandler) -> list[dict]:
    return [json.loads(line) for line in handler.lines]


def test_intent_is_logged_before_the_call(plugin_and_logs) -> None:
    plugin, handler = plugin_and_logs
    asyncio.run(
        plugin.before_tool_callback(
            tool=_tool("read_upstream_go_source"),
            tool_args={"module_path": "syntax/scan.go"},
            tool_context=_context(),
        )
    )
    events = _events(handler)
    assert events[0]["event"] == "tool.intent"
    assert events[0]["tool"] == "read_upstream_go_source"
    assert events[0]["args"]["module_path"] == "syntax/scan.go"


def test_outcome_pairs_with_intent_and_records_latency(plugin_and_logs) -> None:
    # Intent alone answers "what did it try"; the pair answers "and what happened".
    plugin, handler = plugin_and_logs
    tool, context = _tool(), _context()

    asyncio.run(plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=context))
    asyncio.run(
        plugin.after_tool_callback(
            tool=tool,
            tool_args={},
            tool_context=context,
            result={"status": "ok", "conformance": {"assertionsPassed": 261},
                    "ladder": {"passed": 49}},
        )
    )

    intent, outcome = _events(handler)
    assert intent["event"] == "tool.intent"
    assert outcome["event"] == "tool.outcome"
    assert intent["tool"] == outcome["tool"]
    assert outcome["latency_ms"] is not None
    # Progress numbers are promoted to fields so they can be queried, not grepped.
    assert outcome["assertions_passed"] == 261
    assert outcome["probes_passed"] == 49


def test_large_arguments_are_summarised_not_dumped(plugin_and_logs) -> None:
    # A whole ported module can be tens of kilobytes; logging it wholesale would bury the
    # signal and multiply storage cost for no diagnostic gain.
    plugin, handler = plugin_and_logs
    asyncio.run(
        plugin.before_tool_callback(
            tool=_tool("write_ported_typescript_module"),
            tool_args={"relative_path": "eval.ts", "source_code": "x" * 5000},
            tool_context=_context(),
        )
    )
    logged = _events(handler)[0]["args"]
    assert logged["relative_path"] == "eval.ts"
    assert logged["source_code"] == "<5000 chars>"


def test_error_codes_reach_the_log(plugin_and_logs) -> None:
    plugin, handler = plugin_and_logs
    tool, context = _tool("write_ported_typescript_module"), _context()
    asyncio.run(plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=context))
    asyncio.run(
        plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=context,
            result={"status": "error", "error_code": "path_escape"},
        )
    )
    outcome = _events(handler)[-1]
    assert outcome["status"] == "error"
    assert outcome["error_code"] == "path_escape"


def test_logs_are_redacted(plugin_and_logs) -> None:
    plugin, handler = plugin_and_logs
    asyncio.run(
        plugin.before_tool_callback(
            tool=_tool(),
            tool_args={"path": "/home/someuser/l200/x.ts", "api_key": "sk-abcdef123456"},
            tool_context=_context(),
        )
    )
    line = handler.lines[0]
    assert "someuser" not in line
    assert "sk-abcdef123456" not in line


def test_every_line_is_valid_json_with_a_severity(plugin_and_logs) -> None:
    # Cloud Logging parses stdout JSON by these field names; malformed lines are dropped
    # to plain text and lose their structure.
    plugin, handler = plugin_and_logs
    tool, context = _tool(), _context()
    asyncio.run(plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=context))
    for event in _events(handler):
        assert event["severity"] == "INFO"
        assert "message" in event


def test_unexpected_exceptions_are_recorded(plugin_and_logs) -> None:
    # Tools return structured errors rather than raising, so anything here is a tool bug.
    plugin, handler = plugin_and_logs
    asyncio.run(
        plugin.on_tool_error_callback(
            tool=_tool(), tool_args={}, tool_context=_context(),
            error=ValueError("boom in /home/someuser/x"),
        )
    )
    event = _events(handler)[-1]
    assert event["event"] == "tool.exception"
    assert event["error_type"] == "ValueError"
    assert "someuser" not in json.dumps(event)
