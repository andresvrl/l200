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

"""Policy for a self-improving agent: what it may not do, and what needs a human.

Guardrails come in two strengths, and choosing between them is the whole design:

* **Refuse.** The action is never acceptable, so no one should be asked about it. Editing
  the oracle that grades the port is in this class.
* **Ask.** The action is sometimes right and sometimes destroys hours of work. Only a
  human has the context to tell which, so the agent pauses.

Every rule below exists because the corresponding failure already happened during this
project. None of them is hypothetical.

The refuse rules live in :class:`GuardrailPlugin`, attached to the ``App`` so they cover
every agent and every tool, including tools added later. The ask rules are declared with
ADK's own ``require_confirmation``, which suspends the run until a human approves.
"""

from __future__ import annotations

import pathlib
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools import BaseTool, FunctionTool
from google.adk.tools.tool_context import ToolContext

from ..config import TARGET
from ..tools import port_tools

# Tools that change the port. Read-only tools are exempt from the path policy below:
# reading the harness is legitimate and occasionally useful, writing to it never is.
_MUTATING_TOOLS = frozenset(
    {"write_ported_typescript_module", "edit_ported_typescript_module"}
)

# Directories that must stay outside the agent's reach. The first two are the reason this
# rule exists at all: an agent scored by a test suite it can edit is not being measured.
_PROTECTED = ("harness/", "vendor/", "reports/", "app/", "tests/", "deployment/")


def _is_path_argument(name: str) -> bool:
    """Whether a tool argument names a filesystem location rather than content."""
    return name == "path" or name.endswith("_path")


class GuardrailPlugin(BasePlugin):
    """Refuses actions that are never acceptable, before the tool runs.

    Attached to the ``App``, so a tool added next month inherits the policy without
    anyone remembering to re-apply it. That is the argument for a plugin over a check
    inside each tool: the tool enforces its own contract, the plugin enforces the
    project's rules across all of them.
    """

    def __init__(self) -> None:
        super().__init__(name="guardrails")

    @staticmethod
    def _refuse(code: str, message: str, recovery_hint: str) -> dict[str, Any]:
        """Builds a refusal in the same shape tools use for their own errors.

        Deliberately identical to ``port_tools._error``: a blocked call should look to the
        model exactly like any other recoverable failure, so it responds by adapting rather
        than by retrying the same call against a wall.
        """
        return {
            "status": "error",
            "error_code": code,
            "message": message,
            "recovery_hint": recovery_hint,
            "blocked_by": "guardrails",
        }

    async def before_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
    ) -> dict[str, Any] | None:
        """Blocks a call by returning a result instead of letting the tool run.

        Returns ``None`` to allow the call through, which is the case for almost every
        call. Only the oracle-tampering rule lives here; a regression is not knowable until
        after the tool has run, so it is handled in :meth:`after_tool_callback`.
        """
        if tool.name not in _MUTATING_TOOLS:
            return None

        for name, value in tool_args.items():
            # Only arguments that ARE paths. Scanning every string would also scan
            # source_code, and a module containing the comment `// see ../syntax/scanner.ts`
            # would be refused as a path escape -- a guardrail that blocks correct work.
            if not _is_path_argument(name) or not isinstance(value, str):
                continue
            normalised = value.replace("\\", "/").lstrip("./")
            if normalised.startswith(_PROTECTED) or ".." in pathlib.PurePosixPath(value).parts:
                return self._refuse(
                    "protected_path",
                    f"{value!r} names a protected part of the repository.",
                    f"Write only inside {TARGET.output_dir.name}/. The harness and the "
                    "vendored conformance suite are the oracle that grades this port; an "
                    "agent that can edit its own grader is not being measured. If a "
                    "conformance test looks wrong, the port is wrong.",
                )
        return None

    async def after_tool_callback(
        self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Turns a measured regression from a warning into a blocking error.

        ``verify_ported_interpreter`` already detects that a change scored below the best
        recorded run, but it reports it as advice, and advice is ignorable -- during Step 4
        a local tuple fix cost 228 assertions and the run continued on top of it.

        Rewriting the result as an error changes what the model must do next. Recovery is
        ``restore_best_port``, which requires human approval, so detection is automatic and
        the destructive half stays with a person.
        """
        if not isinstance(result, dict) or "regression_warning" not in result:
            return None

        return self._refuse(
            "regression_blocked",
            result["regression_warning"],
            "This change lost measured ground. Do not build on it. Either call "
            "restore_best_port to return to the best recorded version and retry in a "
            "smaller step, or explain precisely which assertions were traded away and why "
            "that is correct.",
        ) | {
            # Keep the measurements: the model still needs them to explain the trade.
            "ladder": result.get("ladder"),
            "conformance": result.get("conformance"),
        }


# --- rules that need a human -----------------------------------------------------------


def rewrites_a_module_that_is_already_earning(relative_path: str = "", **_: Any) -> bool:
    """Whether this write would replace a module that is part of the best recorded port.

    A whole-file rewrite regenerates code that was already correct, and the model has no
    way to know which parts were load-bearing. Observed during the walking-skeleton run: a
    rewrite silently undid an import fix that had already been applied, and the correct
    import count went 8 -> 7 -> 8 across three cycles.

    Creating a NEW module is unaffected, which is most writes. This asks only when there is
    something proven to lose.

    Args:
        relative_path: Destination path the agent proposes to write.
        **_: Remaining tool arguments, ignored.

    Returns:
        True if a human should approve the write before it happens.
    """
    if not relative_path:
        return False
    snapshot = port_tools.BEST_DIR / relative_path
    return snapshot.is_file()


# The two destructive tools, each behind ADK's native confirmation gate so the run
# genuinely suspends until a person answers rather than the model asking itself.
WRITE_TOOL = FunctionTool(
    port_tools.write_ported_typescript_module,
    require_confirmation=rewrites_a_module_that_is_already_earning,
)
RESTORE_TOOL = FunctionTool(port_tools.restore_best_port, require_confirmation=True)


# Tools grouped by role. Each agent is given what its job needs and nothing else: a
# planner holding a write tool will eventually use it, and then the plan and the edit
# arrive in the same breath with no verification between them.
MEASURE_TOOLS = [port_tools.verify_ported_interpreter]

READ_UPSTREAM_TOOLS = [
    port_tools.list_upstream_go_modules,
    port_tools.read_upstream_go_source,
]

READ_PORT_TOOLS = [
    port_tools.list_ported_typescript_modules,
    port_tools.read_ported_typescript_module,
]


def build_tools() -> list[Any]:
    """Every tool, in the order the agent should reach for them.

    Ordering is a hint the model reads, so measurement comes first, then orientation, then
    change. Given to the roles that both write and verify.

    Returns:
        The full tool list, with the destructive tools already wrapped in their gates.
    """
    return [
        *MEASURE_TOOLS,
        *READ_UPSTREAM_TOOLS,
        *READ_PORT_TOOLS,
        port_tools.edit_ported_typescript_module,
        WRITE_TOOL,
        RESTORE_TOOL,
    ]
