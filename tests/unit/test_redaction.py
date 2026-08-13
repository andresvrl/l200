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

"""Tests for the redaction applied to logs, traces, and memory writes.

Redaction is only worth having if it is tested, so each case below is a leak that could
plausibly occur in this project rather than a generic example.
"""

from __future__ import annotations

import pytest

from app.redaction import ROOT, redact, redact_value


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        # The leak this project actually had: absolute paths in every tool result.
        ("/home/someuser/l200/ported/index.ts", "someuser"),
        ("/Users/someone/project/file.ts", "someone"),
        # Credentials that could ride along inside source code the agent reads.
        ('api_key = "sk-abcd1234efgh5678"', "sk-abcd1234efgh5678"),
        ("AIzaSyA1234567890123456789012345678901", "AIzaSyA"),
        ("Authorization: Bearer ya29.a0AfH6SMBexampletokenvalue", "ya29."),
        ("ghp_abcdefghijklmnopqrstuvwxyz012345", "ghp_abcdefghij"),
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("contact person@example.com", "person@example.com"),
    ],
)
def test_secrets_and_identifiers_are_removed(raw: str, must_not_contain: str) -> None:
    assert must_not_contain not in redact(raw)


def test_private_key_body_is_removed_not_just_the_header() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"
        "-----END RSA PRIVATE KEY-----"
    )
    cleaned = redact(pem)
    assert "MIIEowIBAAKCAQEA" not in cleaned
    assert cleaned == "[REDACTED private key]"


def test_repository_paths_become_a_stable_marker() -> None:
    # Repo-relative paths stay useful for debugging; only the machine-specific prefix goes.
    cleaned = redact(f"{ROOT}/ported/syntax/parser.ts")
    assert cleaned == "<repo>/ported/syntax/parser.ts"


def test_ordinary_text_is_left_alone() -> None:
    # Over-redaction destroys the diagnostic value of a log; these must survive intact.
    for benign in [
        "ladder 49/51 probes, highest clean tier 5",
        "got NEWLINE, want primary expression",
        "ported/eval.ts(34,8): error TS2835",
    ]:
        assert redact(benign) == benign


def test_redaction_walks_nested_structures() -> None:
    # Tool arguments and results are nested, so top-level-only redaction would miss most.
    payload = {
        "path": "/home/someuser/x.ts",
        "results": [{"token": "abcdefghijklmnop"}],
    }
    cleaned = redact_value(payload)
    assert "someuser" not in str(cleaned)
    assert "abcdefghijklmnop" not in str(cleaned)


def test_deep_nesting_is_truncated_rather_than_recursed_forever() -> None:
    deep: object = "secret"
    for _ in range(12):
        deep = {"next": deep}
    assert "depth limit" in str(redact_value(deep))


def test_empty_input_is_safe() -> None:
    assert redact("") == ""
    assert redact_value(None) is None
    assert redact_value(42) == 42
