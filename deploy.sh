#!/usr/bin/env bash
#
# GCP Genie — one-shot deploy.
# Deploys the ADK agent to Vertex AI Agent Engine and registers it to a
# Gemini Enterprise app, prompting for any configuration it needs.
#
#   ./deploy.sh
#
# Every prompt can be pre-set via an environment variable of the same name
# (e.g. GOOGLE_CLOUD_PROJECT=my-proj GE_APP_ID=... ./deploy.sh) for non-
# interactive runs. Secrets are read hidden and never written to disk.

set -euo pipefail
cd "$(dirname "$0")"

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
info() { printf '  %s\n' "$1"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$1" >&2; exit 1; }

# prompt VAR "Label" "default"  — uses existing env value if already set.
# Tolerates a closed stdin (non-interactive runs): falls back to the default.
prompt() {
  local var="$1" label="$2" default="${3:-}" current="${!1:-}" reply=""
  [ -n "$current" ] && { info "$label: $current"; return; }
  read -r -p "$label${default:+ [$default]}: " reply || reply=""
  reply="${reply:-$default}"
  printf -v "$var" '%s' "$reply"
}
prompt_secret() {
  local var="$1" label="$2" current="${!1:-}" reply=""
  [ -n "$current" ] && { info "$label: (provided via env)"; return; }
  read -r -s -p "$label: " reply || reply=""; echo
  printf -v "$var" '%s' "$reply"
}

command -v gcloud >/dev/null || die "gcloud CLI is required (https://cloud.google.com/sdk/docs/install)."
command -v python3 >/dev/null || die "python3 is required."

bold "GCP Genie deployment"
echo

# ---- Configuration -------------------------------------------------------- #
: "${GOOGLE_CLOUD_PROJECT:=$(gcloud config get-value project 2>/dev/null || true)}"
prompt GOOGLE_CLOUD_PROJECT "GCP project id" "$GOOGLE_CLOUD_PROJECT"
[ -n "$GOOGLE_CLOUD_PROJECT" ] || die "A GCP project id is required."

PROJECT_NUMBER="${PROJECT_NUMBER:-$(gcloud projects describe "$GOOGLE_CLOUD_PROJECT" --format='value(projectNumber)' 2>/dev/null || true)}"
[ -n "$PROJECT_NUMBER" ] || die "Could not resolve the project number for $GOOGLE_CLOUD_PROJECT."
info "Project number: $PROJECT_NUMBER"

prompt AGENT_ENGINE_LOCATION   "Agent Engine region" "us-central1"
prompt STAGING_BUCKET          "Staging bucket" "gs://${GOOGLE_CLOUD_PROJECT}-agent-staging"
prompt GCP_GENIE_MODEL         "Gemini model" "gemini-3.5-flash"
prompt GCP_GENIE_MODEL_LOCATION "Model region" "global"
prompt GE_APP_ID               "Gemini Enterprise app id"
[ -n "$GE_APP_ID" ] || die "A Gemini Enterprise app id is required."
prompt GE_ENDPOINT_LOCATION    "Gemini Enterprise endpoint location" "global"
prompt GE_AUTH_ID              "OAuth authorization id" "gcp-genie-oauth"
prompt OAUTH_CLIENT_ID         "OAuth client id"
[ -n "$OAUTH_CLIENT_ID" ] || die "An OAuth client id is required."
prompt_secret OAUTH_CLIENT_SECRET "OAuth client secret"
[ -n "$OAUTH_CLIENT_SECRET" ] || die "An OAuth client secret is required."
prompt OAUTH_SCOPES "OAuth scopes (space separated)" \
  "https://www.googleapis.com/auth/cloud-platform https://www.googleapis.com/auth/userinfo.email"
prompt AGENT_ENGINE_RESOURCE "Existing reasoning engine to UPDATE (blank = create new)" ""

export GOOGLE_CLOUD_PROJECT PROJECT_NUMBER AGENT_ENGINE_LOCATION STAGING_BUCKET \
  GCP_GENIE_MODEL GCP_GENIE_MODEL_LOCATION GE_APP_ID GE_ENDPOINT_LOCATION GE_AUTH_ID \
  OAUTH_CLIENT_ID OAUTH_CLIENT_SECRET OAUTH_SCOPES AGENT_ENGINE_RESOURCE

echo
bold "About to deploy (this creates billable resources and registers to Gemini Enterprise)."
if [ -z "${ASSUME_YES:-}" ]; then
  read -r -p "Continue? [y/N]: " ok; case "$ok" in y|Y|yes) ;; *) die "Aborted." ;; esac
fi

# ---- Enable required APIs -------------------------------------------------- #
bold "Enabling required APIs (idempotent)..."
gcloud services enable \
  aiplatform.googleapis.com discoveryengine.googleapis.com cloudasset.googleapis.com \
  cloudresourcemanager.googleapis.com iam.googleapis.com serviceusage.googleapis.com \
  compute.googleapis.com storage.googleapis.com \
  --project "$GOOGLE_CLOUD_PROJECT"

# ---- Staging bucket ------------------------------------------------------- #
if ! gcloud storage buckets describe "$STAGING_BUCKET" --project "$GOOGLE_CLOUD_PROJECT" >/dev/null 2>&1; then
  bold "Creating staging bucket $STAGING_BUCKET ..."
  gcloud storage buckets create "$STAGING_BUCKET" \
    --project "$GOOGLE_CLOUD_PROJECT" --location "$AGENT_ENGINE_LOCATION"
fi

# ---- Bundle gcloud's static CLI tree (for offline command validation) ------ #
SDK_ROOT="$(gcloud info --format='value(installation.sdk_root)' 2>/dev/null || true)"
SRC="$SDK_ROOT/data/cli/gcloud_completions.py"
[ -f "$SRC" ] || die "Could not find gcloud_completions.py in the Cloud SDK ($SRC). Reinstall the Cloud SDK."
cp "$SRC" gcp_genie_agent/data/gcloud_completions.py
info "Bundled gcloud CLI tree from $SRC"

# ---- Python environment --------------------------------------------------- #
bold "Setting up Python environment..."
[ -d .venv ] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ---- Deploy to Agent Engine ----------------------------------------------- #
bold "Deploying to Vertex AI Agent Engine (this can take several minutes)..."
RESOURCE_FILE="$(mktemp)"
AGENT_ENGINE_RESOURCE_FILE="$RESOURCE_FILE" PYTHONPATH=. \
  python scripts/deploy_agent_engine.py
AGENT_ENGINE_RESOURCE="$(tail -n1 "$RESOURCE_FILE")"; rm -f "$RESOURCE_FILE"
export AGENT_ENGINE_RESOURCE
[ -n "$AGENT_ENGINE_RESOURCE" ] || die "Deployment did not return a reasoning engine resource."
bold "Deployed: $AGENT_ENGINE_RESOURCE"

# ---- Register to Gemini Enterprise ---------------------------------------- #
bold "Registering to Gemini Enterprise..."
PYTHONPATH=. python scripts/register_gemini_enterprise.py

echo
bold "Done. GCP Genie is deployed and registered."
info "Reasoning engine: $AGENT_ENGINE_RESOURCE"
info "Open your Gemini Enterprise app, authorize the agent when prompted, and start chatting."
