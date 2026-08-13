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

# Secret management for the agent.
#
# The deployed agent has NO long-lived credentials by design. It authenticates to Vertex AI
# with the attached service account through Application Default Credentials, and CI
# authenticates through Workload Identity Federation, so there is no key to leak, rotate,
# or accidentally commit. That absence is the primary control here; everything below exists
# for the one case where a secret is genuinely unavoidable.
#
# That case is the Google AI Studio path. Running against AI Studio instead of Vertex AI
# requires GEMINI_API_KEY, which is a real bearer credential. When someone chooses that
# path, the key must arrive from Secret Manager at runtime rather than as a literal in
# deployment_spec.env, where it would be readable by anyone with view access to the
# resource and would appear in Terraform state.
#
# Opt-in: nothing here is created unless `gemini_api_key_secret_id` is set. The default
# deployment uses ADC and needs none of it.

locals {
  use_api_key_secret = var.gemini_api_key_secret_id != ""
}

# The secret CONTAINER only. No version, and therefore no secret material, is created by
# Terraform -- writing a value here would put the credential into state, which is exactly
# the problem this is meant to avoid. Add versions out of band:
#
#   echo -n "$KEY" | gcloud secrets versions add "$SECRET_ID" --data-file=-
resource "google_secret_manager_secret" "gemini_api_key" {
  count = local.use_api_key_secret ? 1 : 0

  project   = var.project_id
  secret_id = var.gemini_api_key_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.services]
}

# Read access for the agent's own service account, scoped to this one secret rather than
# granted at project level.
resource "google_secret_manager_secret_iam_member" "app_sa_accessor" {
  count = local.use_api_key_secret ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.gemini_api_key[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app_sa.email}"
}
