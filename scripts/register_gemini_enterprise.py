"""Register the deployed agent to a Gemini Enterprise app, with an OAuth
authorization resource so the agent acts on behalf of the signed-in user.

All configuration comes from environment variables (set by deploy.sh):
  GOOGLE_CLOUD_PROJECT, PROJECT_NUMBER, GE_APP_ID, GE_ENDPOINT_LOCATION,
  GE_AUTH_ID, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_SCOPES (space sep),
  AGENT_ENGINE_RESOURCE, AGENT_DISPLAY_NAME.
"""

import json
import os
import subprocess
import sys
import urllib.parse

import requests

REDIRECT_URI = "https://vertexaisearch.cloud.google.com/static/oauth/oauth.html"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: required environment variable {name} is not set.")
    return val


def _access_token() -> str:
    return subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()


def _headers(project_id: str) -> dict:
    return {
        "Authorization": f"Bearer {_access_token()}",
        "Content-Type": "application/json",
        "X-Goog-User-Project": project_id,
    }


def _authorization_uri(client_id: str, scopes: list[str]) -> str:
    encoded_redirect = urllib.parse.quote(REDIRECT_URI, safe="")
    scope = "%20".join(scopes)
    params = (
        f"client_id={client_id}"
        f"&redirect_uri={encoded_redirect}"
        f"&response_type=code&access_type=offline&prompt=consent"
        f"&include_granted_scopes=true&scope={scope}"
    )
    return "https://accounts.google.com/o/oauth2/v2/auth?" + params


def _base(location: str) -> str:
    prefix = "" if location == "global" else f"{location}-"
    return f"https://{prefix}discoveryengine.googleapis.com/v1alpha"


def main() -> None:
    project_id = _require("GOOGLE_CLOUD_PROJECT")
    project_number = _require("PROJECT_NUMBER")
    app_id = _require("GE_APP_ID")
    auth_id = os.environ.get("GE_AUTH_ID", "gcp-genie-oauth")
    endpoint_location = os.environ.get("GE_ENDPOINT_LOCATION", "global")
    client_id = _require("OAUTH_CLIENT_ID")
    client_secret = _require("OAUTH_CLIENT_SECRET")
    scopes = os.environ.get(
        "OAUTH_SCOPES",
        "https://www.googleapis.com/auth/cloud-platform "
        "https://www.googleapis.com/auth/userinfo.email",
    ).split()
    reasoning_engine = _require("AGENT_ENGINE_RESOURCE")
    display_name = os.environ.get("AGENT_DISPLAY_NAME", "GCP Genie")

    base = _base(endpoint_location)
    authorization = f"projects/{project_number}/locations/global/authorizations/{auth_id}"

    # 1. Create (or reuse) the OAuth authorization resource.
    url = (f"{base}/projects/{project_number}/locations/global/authorizations"
           f"?authorizationId={auth_id}")
    body = {
        "name": authorization,
        "serverSideOauth2": {
            "clientId": client_id,
            "clientSecret": client_secret,
            "authorizationUri": _authorization_uri(client_id, scopes),
            "tokenUri": TOKEN_URI,
        },
    }
    print(f"Creating authorization {auth_id} ...", file=sys.stderr)
    r = requests.post(url, headers=_headers(project_id), data=json.dumps(body), timeout=60)
    if r.status_code == 409 or "ALREADY_EXISTS" in r.text:
        print("  authorization already exists — reusing.", file=sys.stderr)
    elif r.status_code >= 300:
        sys.exit(f"  FAILED to create authorization ({r.status_code}): {r.text}")
    else:
        print("  created.", file=sys.stderr)

    # 2. Register the agent (idempotent: reuse an existing agent for this engine).
    url = (f"{base}/projects/{project_id}/locations/global/collections/default_collection"
           f"/engines/{app_id}/assistants/default_assistant/agents")
    existing = requests.get(url, headers=_headers(project_id), timeout=60)
    if existing.status_code < 300:
        for agent in existing.json().get("agents", []):
            re = (agent.get("adkAgentDefinition", {})
                  .get("provisionedReasoningEngine", {}).get("reasoningEngine"))
            if re == reasoning_engine:
                print(f"  agent already registered for this engine: {agent.get('name')}",
                      file=sys.stderr)
                print("DONE")
                return
    body = {
        "displayName": display_name,
        "description": ("GCP assistant: documentation Q&A, validated gcloud script "
                        "generation, confirmed execution, and live asset queries."),
        "adkAgentDefinition": {
            "provisionedReasoningEngine": {"reasoningEngine": reasoning_engine},
        },
        "authorizationConfig": {"toolAuthorizations": [authorization]},
    }
    print(f"Registering agent to Gemini Enterprise app {app_id} ...", file=sys.stderr)
    r = requests.post(url, headers=_headers(project_id), data=json.dumps(body), timeout=60)
    if r.status_code >= 300:
        sys.exit(f"  FAILED to register agent ({r.status_code}): {r.text}")
    print("  registered agent: " + r.json().get("name", "(unknown)"), file=sys.stderr)
    print("DONE")


if __name__ == "__main__":
    main()
