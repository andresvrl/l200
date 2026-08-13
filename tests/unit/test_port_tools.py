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

"""Deterministic tests for the port tools.

Nothing here calls a model. LLM output is non-deterministic, so agent BEHAVIOUR is
measured by the conformance oracle (`npm run conformance`), not by pytest. These tests
cover the parts that must behave identically every time: path safety, error shape, and
the tool schemas the model actually sees.
"""

from __future__ import annotations

import inspect
import re

import pytest

from app.tools import port_tools

# Every tool the agent is given. Keep in sync with app/agent.py.
ALL_TOOLS = [
    port_tools.list_upstream_go_modules,
    port_tools.read_upstream_go_source,
    port_tools.write_ported_typescript_module,
    port_tools.read_ported_typescript_module,
    port_tools.edit_ported_typescript_module,
    port_tools.restore_best_port,
    port_tools.verify_ported_interpreter,
]


# --- path safety ------------------------------------------------------------
# The agent may write only inside ported/. Everything else -- the harness, the vendored
# conformance suite, the agent's own source -- must be unreachable, or it could edit the
# oracle that grades it.


@pytest.mark.parametrize(
    "escape",
    [
        "../harness/contract.ts",
        "../../etc/passwd",
        "sub/../../vendor/starlark-testdata/bool.star",
    ],
)
def test_writes_outside_ported_are_refused(escape: str) -> None:
    result = port_tools.write_ported_typescript_module(escape, "export const x = 1;")
    assert result["status"] == "error"
    assert result["error_code"] == "path_escape"


def test_reads_outside_upstream_are_refused() -> None:
    result = port_tools.read_upstream_go_source("../../../etc/passwd")
    assert result["status"] == "error"
    assert result["error_code"] in {"path_escape", "module_not_found"}


def test_safe_path_accepts_normal_paths_and_rejects_escapes() -> None:
    assert port_tools._safe_path(port_tools.PORTED, "index.ts") is not None
    assert port_tools._safe_path(port_tools.PORTED, "syntax/parser.ts") is not None
    assert port_tools._safe_path(port_tools.PORTED, "../harness/contract.ts") is None


# --- input validation -------------------------------------------------------


def test_non_typescript_extension_is_refused() -> None:
    result = port_tools.write_ported_typescript_module("index.py", "x = 1")
    assert result["error_code"] == "bad_extension"


def test_empty_source_is_refused() -> None:
    # This tool overwrites rather than patches, so an empty write silently destroys a
    # module. Refusing is safer than obeying.
    result = port_tools.write_ported_typescript_module("scratch.ts", "   \n  ")
    assert result["error_code"] == "empty_source"


# --- edit semantics ---------------------------------------------------------


def test_edit_requires_an_exactly_unique_fragment(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(port_tools, "PORTED", tmp_path)
    (tmp_path / "sample.ts").write_text("const a = 1;\nconst b = 1;\n")

    ambiguous = port_tools.edit_ported_typescript_module("sample.ts", "= 1;", "= 2;")
    assert ambiguous["error_code"] == "fragment_ambiguous"
    assert ambiguous["occurrences"] == 2

    missing = port_tools.edit_ported_typescript_module("sample.ts", "nope", "x")
    assert missing["error_code"] == "fragment_not_found"

    ok = port_tools.edit_ported_typescript_module("sample.ts", "const a = 1;", "const a = 42;")
    assert ok["status"] == "ok"
    assert "const a = 42;" in (tmp_path / "sample.ts").read_text()


def test_edit_on_missing_module_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(port_tools, "PORTED", tmp_path)
    result = port_tools.edit_ported_typescript_module("absent.ts", "a", "b")
    assert result["error_code"] == "module_not_found"


# --- error shape ------------------------------------------------------------
# Rubric row 1.4: an error must tell the model what to do next, not just what went wrong.


def test_every_error_carries_recovery_guidance() -> None:
    errors = [
        port_tools.write_ported_typescript_module("../escape.ts", "x"),
        port_tools.write_ported_typescript_module("bad.py", "x"),
        port_tools.write_ported_typescript_module("empty.ts", ""),
        port_tools.read_upstream_go_source("does/not/exist.go"),
        port_tools.read_ported_typescript_module("does/not/exist.ts"),
    ]
    for error in errors:
        assert error["status"] == "error"
        assert error["error_code"]
        assert error["message"]
        assert len(error["recovery_hint"]) > 20, "a hint must actually guide, not just label"


def test_missing_module_suggests_valid_alternatives() -> None:
    # A bare "not found" makes the model guess again; listing real options ends the loop.
    result = port_tools.read_upstream_go_source("syntax/nosuchfile.go")
    assert result["error_code"] == "module_not_found"
    assert result["available_paths"], "should suggest paths that do exist"


# --- tool schemas -----------------------------------------------------------
# ADK builds each tool's JSON schema from its type hints and docstring, so an
# undocumented or untyped parameter becomes a schema the model cannot use correctly.
# These tests enforce that, rather than restating the schemas by hand.


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.__name__)
def test_tool_has_a_docstring_with_a_returns_section(tool) -> None:
    doc = inspect.getdoc(tool)
    assert doc, f"{tool.__name__} has no docstring, so the model gets no description"
    assert "Returns:" in doc, f"{tool.__name__} must document what it returns"


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.__name__)
def test_every_parameter_is_typed_and_documented(tool) -> None:
    doc = inspect.getdoc(tool) or ""
    signature = inspect.signature(tool)

    for name, param in signature.parameters.items():
        assert param.annotation is not inspect.Parameter.empty, (
            f"{tool.__name__}.{name} is untyped, so ADK cannot infer its schema"
        )
        assert re.search(rf"^\s*{re.escape(name)}:", doc, re.MULTILINE), (
            f"{tool.__name__}.{name} is missing from the docstring Args section"
        )


@pytest.mark.parametrize("tool", ALL_TOOLS, ids=lambda t: t.__name__)
def test_tool_names_say_what_they_act_on(tool) -> None:
    # Rubric row 1.2: `write_ported_typescript_module`, not `write_file`. A name that
    # names its object stops the model reaching for the wrong tool.
    assert "_" in tool.__name__ and len(tool.__name__) > 12
