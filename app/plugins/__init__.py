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

"""Runner-wide plugins. Attached to the App, they wrap every agent beneath it."""

from .guardrails import GuardrailPlugin, build_tools
from .observability import ObservabilityPlugin

__all__ = ["GuardrailPlugin", "ObservabilityPlugin", "build_tools"]
