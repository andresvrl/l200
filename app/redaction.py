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

"""Scrubs sensitive data before it reaches logs, traces, or memory.

This is not a checkbox. Two real leaks exist in a code-porting agent:

1. **Absolute paths leak identity.** Every tool result in this project carried
   ``/home/admin_dreva_altostrat_com/...``, which publishes an OS username and home
   directory layout into logs that may be shipped to Cloud Logging and BigQuery.
2. **Source code can carry secrets.** The agent reads and writes files wholesale; a key
   committed upstream would be copied verbatim into a log line.

Redaction runs on the way OUT (logging, tracing, memory writes), never on the way in --
the agent still needs real paths and real source to do its job.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Ordered most-specific first: a service-account key is also "a long base64-ish string",
# so the narrow patterns must win.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # PEM private keys, including the body.
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "[REDACTED private key]"),
    # Google API keys.
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[REDACTED api key]"),
    # OAuth / bearer tokens.
    (re.compile(r"\bya29\.[0-9A-Za-z_-]+"), "[REDACTED oauth token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{20,}"), "[REDACTED bearer token]"),
    # GitHub tokens.
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"), "[REDACTED github token]"),
    # Anything self-declaring as a secret: key = "...", password: '...', token=...
    (re.compile(r"""(?i)\b(api[_-]?key|secret|password|passwd|token|credential)\b\s*[:=]\s*["']?[^\s"',;)]{8,}["']?"""),
     r"\1=[REDACTED]"),
    # Email addresses.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED email]"),
]

# Absolute home directories, replaced with a repo-relative marker.
_HOME_PATH = re.compile(r"/(?:home|Users)/[^/\s\"']+")

# Dictionary keys whose VALUE is sensitive regardless of what it looks like. Inline
# patterns above only catch `token=abc` written in prose; structured data instead looks
# like {"token": "abc"}, where the value alone is indistinguishable from ordinary text.
_SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|secret|password|passwd|token|credential|authorization|private[_-]?key)"
)


def redact(text: str) -> str:
    """Removes secrets and identifying paths from a string.

    Args:
        text: Arbitrary text bound for a log line, trace attribute, or memory record.

    Returns:
        The same text with secrets replaced by ``[REDACTED ...]`` markers and absolute
        home paths rewritten relative to the repository root.
    """
    if not text:
        return text

    # Repo paths first, so the remaining patterns see shorter, stable strings.
    text = text.replace(str(ROOT), "<repo>")
    text = _HOME_PATH.sub("<home>", text)

    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: object, _depth: int = 0) -> object:
    """Applies :func:`redact` to every string inside a nested structure.

    Tool arguments and results are dicts of lists of strings, so redacting only top-level
    strings would miss almost everything worth redacting.

    Args:
        value: Any JSON-like value: string, mapping, sequence, or scalar.
        _depth: Internal recursion guard; not part of the public interface.

    Returns:
        The same shape with all strings redacted. Scalars pass through untouched.
    """
    if _depth > 6:  # Deeply nested structures are truncated rather than walked forever.
        return "[REDACTED depth limit]"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if isinstance(k, str) and _SENSITIVE_KEY.search(k)
            else redact_value(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_value(v, _depth + 1) for v in value]
    return value
