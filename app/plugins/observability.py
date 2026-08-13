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

"""Structured logging and tracing for every tool call, as one runner-wide plugin.

Attached to the ``App``, this wraps every agent and sub-agent beneath it, so
observability is not something each tool has to remember to do.

It exists because of a specific failure: during a walking-skeleton run the agent sat for
over four minutes at zero CPU and there was no way to see which tool call was in flight.
Logging the INTENT before a call and the OUTCOME after it makes that visible -- and the
pair is also what lets you ask "what did it try, and what actually happened?", which a
single after-the-fact log line cannot answer.

Log lines are JSON on stdout using Cloud Logging's field names, so they are structured
locally and parsed automatically once deployed. That is a deliberate choice over adding a
logging dependency: twenty readable lines beat a library to learn.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool
from google.adk.tools.tool_context import ToolContext
from opentelemetry import trace

from ..redaction import redact, redact_value

_LOGGER_NAME = "starport"


class _CloudJsonFormatter(logging.Formatter):
    """Emits one JSON object per line, using the field names Cloud Logging expects.

    ``severity`` and ``message`` are Cloud Logging's own keys; everything else rides in
    ``jsonPayload``. Trace and span ids are included so a log line can be pivoted straight
    to its trace.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        payload.update(getattr(record, "fields", {}))

        span = trace.get_current_span()
        context = span.get_span_context() if span else None
        if context and context.is_valid:
            payload["logging.googleapis.com/trace"] = format(context.trace_id, "032x")
            payload["logging.googleapis.com/spanId"] = format(context.span_id, "016x")

        return json.dumps(payload, default=str)


def _get_logger() -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_CloudJsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False  # Avoid duplicate lines via the root logger.
    return logger


class ObservabilityPlugin(BasePlugin):
    """Logs the intent and the outcome of every tool call, with trace correlation."""

    def __init__(self) -> None:
        super().__init__(name="observability")
        self._log = _get_logger()
        self._started: dict[str, float] = {}

    def _emit(self, event: str, **fields: Any) -> None:
        """Writes one structured line. All values are redacted before they leave."""
        self._log.info(event, extra={"fields": {"event": event, **redact_value(fields)}})

    @staticmethod
    def _key(tool: BaseTool, tool_context: ToolContext) -> str:
        return f"{tool_context.invocation_id}:{tool.name}"

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        """Records what the agent is ABOUT to do, before it happens."""
        self._started[self._key(tool, tool_context)] = time.monotonic()

        # Argument values can be whole source files; log their size, not their contents.
        summary = {
            key: (f"<{len(value)} chars>" if isinstance(value, str) and len(value) > 200 else value)
            for key, value in tool_args.items()
        }
        self._emit("tool.intent", tool=tool.name, args=summary)

        span = trace.get_current_span()
        if span:
            span.set_attribute("starport.tool", tool.name)
        return None  # Never block; this plugin observes, it does not decide.

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Records what ACTUALLY happened, paired with the intent above."""
        started = self._started.pop(self._key(tool, tool_context), None)
        latency_ms = round((time.monotonic() - started) * 1000) if started else None

        fields: dict[str, Any] = {
            "tool": tool.name,
            "status": result.get("status", "ok") if isinstance(result, dict) else "ok",
            "latency_ms": latency_ms,
        }
        if isinstance(result, dict):
            if result.get("error_code"):
                fields["error_code"] = result["error_code"]
            # The numbers that matter for this agent, promoted to first-class fields so
            # progress is queryable rather than buried in a message string.
            if "conformance" in result:
                fields["assertions_passed"] = result["conformance"].get("assertionsPassed")
                fields["probes_passed"] = result["ladder"].get("passed")

        self._emit("tool.outcome", **fields)

        span = trace.get_current_span()
        if span:
            span.set_attribute("starport.tool.status", str(fields["status"]))
            if latency_ms is not None:
                span.set_attribute("starport.tool.latency_ms", latency_ms)
        return None

    async def on_tool_error_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """Logs an unexpected exception, which by design should be rare.

        Tools return structured errors rather than raising, so anything arriving here is a
        defect in a tool, not an expected failure the agent should handle.
        """
        self._started.pop(self._key(tool, tool_context), None)
        self._emit(
            "tool.exception",
            tool=tool.name,
            error_type=type(error).__name__,
            error=redact(str(error)),
        )
        return None
