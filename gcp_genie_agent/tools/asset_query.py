"""Live GCP asset inventory query (function #3).

Primary path: Cloud Asset Inventory ``searchAllResources`` — the broadest
cross-resource search, the REST equivalent of ``gcloud asset
search-all-resources`` (no gcloud binary needed in the Agent Engine sandbox).

Cloud Asset Inventory is opt-in, though: it must be enabled in the target
project/org and the caller needs ``cloudasset.viewer``. So when CAI is
unavailable (API disabled) or the caller can't use it, we fall back to querying
the common services directly (Compute, Storage, IAM) — those APIs are present
wherever the service is used and need no CAI.

This agent acts **only** with the end-user's own OAuth credentials. It never
uses the agent's service account / Application Default Credentials, so a query
can never exceed what the signed-in user is allowed to see.

Credential source:
  * Gemini Enterprise forwards the user's OAuth access token in the request's
    ``authorizations`` map; the Agent Engine runtime stores it in the session
    state under the *authorization ID* (``GE_AUTH_ID``, default
    ``gcp-genie-oauth``). The tool reads it from ``tool_context.state``.
  * For local testing only, an explicit user token may be supplied via
    ``GCP_USER_ACCESS_TOKEN`` (this is a user token you provide, not the SA).
If no user token is present, the tool returns an "unauthorized" result rather
than falling back to any service identity.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Callable, Optional

import requests

logger = logging.getLogger("gcp_genie.asset_query")

_CLOUDASSET = "https://cloudasset.googleapis.com/v1"
# Authorization ID configured in Gemini Enterprise == the session-state key the
# Agent Engine runtime uses to deliver the user's forwarded OAuth token.
AUTH_ID = os.environ.get("GE_AUTH_ID", "gcp-genie-oauth")


# --------------------------------------------------------------------------- #
# Credentials — end-user OAuth ONLY (never the agent service account)
# --------------------------------------------------------------------------- #
def _looks_like_token(val: Any) -> bool:
    return isinstance(val, str) and (val.startswith("ya29.") or val.startswith("ey")) and len(val) > 40


def _state_dict(tool_context: Any) -> dict:
    """Read ToolContext.state as a plain dict.

    ADK's State object is NOT a plain mapping — ``dict(state)`` raises because it
    falls back to sequence iteration. Use ``State.to_dict()`` (merges base value +
    pending delta); fall back to plain dict for non-ADK contexts (e.g. tests).
    """
    state = getattr(tool_context, "state", None)
    if state is None:
        return {}
    to_dict = getattr(state, "to_dict", None)
    if callable(to_dict):
        try:
            return dict(to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    try:
        return dict(state)
    except Exception:  # pragma: no cover - defensive
        return {}


def state_keys(tool_context: Any) -> list:
    """Names (not values) of session-state keys — for diagnosing token delivery."""
    return sorted(_state_dict(tool_context).keys())


def resolve_user_token(tool_context: Any) -> tuple[Optional[str], str]:
    """Return (token, source) using ONLY the end-user's GE-forwarded OAuth token.

    Never falls back to ADC / the agent's service account.
    """
    state = _state_dict(tool_context)
    # 1. Exact authorization-id key (how Agent Engine injects the GE user token).
    for key, val in state.items():
        if (key == AUTH_ID or key.endswith("/" + AUTH_ID) or key.endswith(":" + AUTH_ID)) and _looks_like_token(val):
            return val, f"ge-user-oauth[{key}]"
    # 2. Any forwarded OAuth token present in session state.
    for key, val in state.items():
        if _looks_like_token(val):
            return val, f"ge-user-oauth[{key}]"
    # 3. Local-testing hook: an explicitly supplied *user* token (not the SA).
    tok = os.environ.get("GCP_USER_ACCESS_TOKEN")
    if tok:
        return tok, "env:GCP_USER_ACCESS_TOKEN"
    return None, "none"


def _headers(token: str, project: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Goog-User-Project": project}


def _record(**kw) -> dict:
    """Uniform structured record across CAI and per-service results."""
    base = {
        "name": None, "displayName": None, "assetType": None, "project": None,
        "location": None, "state": None, "description": None, "labels": None,
        "createTime": None, "updateTime": None, "parentFullResourceName": None,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Cloud Asset Inventory (primary)
# --------------------------------------------------------------------------- #
def _search_cai(
    token: str, project: str, scope: str, query: str, asset_types: str, limit: int
) -> tuple[bool, dict]:
    """Returns (ok, payload). ok=False means caller should consider the fallback."""
    params: list[tuple[str, str]] = [("pageSize", str(limit))]
    if query:
        params.append(("query", query))
    for at in [a.strip() for a in asset_types.split(",") if a.strip()]:
        params.append(("assetTypes", at))

    url = f"{_CLOUDASSET}/{scope}:searchAllResources"
    try:
        resp = requests.get(url, headers=_headers(token, project), params=params, timeout=30)
    except requests.RequestException as exc:
        return False, {"http_status": None, "error": f"Request failed: {exc}"}

    if resp.status_code == 200:
        data = resp.json()
        results = [
            _record(
                name=r.get("name"), displayName=r.get("displayName"),
                assetType=r.get("assetType"), project=r.get("project"),
                location=r.get("location"), state=r.get("state"),
                description=r.get("description"), labels=r.get("labels"),
                createTime=r.get("createTime"), updateTime=r.get("updateTime"),
                parentFullResourceName=r.get("parentFullResourceName"),
            )
            for r in data.get("results", [])
        ]
        return True, {"resources": results, "next_page_token": data.get("nextPageToken")}

    return False, {"http_status": resp.status_code, "error": resp.text[:600]}


def _cai_unavailable(payload: dict) -> bool:
    """True when CAI is disabled or the caller can't use it (so fallback may help)."""
    text = (payload.get("error") or "")
    status = payload.get("http_status")
    markers = ("SERVICE_DISABLED", "has not been used", "is disabled",
               "PERMISSION_DENIED", "does not have permission", "API has not been")
    return status in (403, 404, None) or any(m in text for m in markers)


# --------------------------------------------------------------------------- #
# Per-service fallback (no CAI required)
# --------------------------------------------------------------------------- #
def _svc_compute_instances(token: str, project: str, limit: int):
    url = f"https://compute.googleapis.com/compute/v1/projects/{project}/aggregated/instances"
    r = requests.get(url, headers=_headers(token, project), params={"maxResults": limit}, timeout=30)
    if r.status_code != 200:
        return [], f"compute.googleapis.com/Instance: {r.status_code} {r.text[:160]}"
    out = []
    for zone, blk in (r.json().get("items") or {}).items():
        for inst in blk.get("instances", []) or []:
            out.append(_record(
                name=inst.get("name"), displayName=inst.get("name"),
                assetType="compute.googleapis.com/Instance", project=project,
                location=zone.replace("zones/", ""), state=inst.get("status"),
                labels=inst.get("labels"), createTime=inst.get("creationTimestamp"),
            ))
            if len(out) >= limit:
                return out, None
    return out, None


def _svc_storage_buckets(token: str, project: str, limit: int):
    url = "https://storage.googleapis.com/storage/v1/b"
    r = requests.get(url, headers=_headers(token, project),
                     params={"project": project, "maxResults": limit}, timeout=30)
    if r.status_code != 200:
        return [], f"storage.googleapis.com/Bucket: {r.status_code} {r.text[:160]}"
    out = [
        _record(
            name=b.get("name"), displayName=b.get("name"),
            assetType="storage.googleapis.com/Bucket", project=project,
            location=(b.get("location") or "").lower(), labels=b.get("labels"),
            createTime=b.get("timeCreated"), updateTime=b.get("updated"),
        )
        for b in r.json().get("items", []) or []
    ]
    return out, None


def _svc_service_accounts(token: str, project: str, limit: int):
    url = f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts"
    r = requests.get(url, headers=_headers(token, project), params={"pageSize": limit}, timeout=30)
    if r.status_code != 200:
        return [], f"iam.googleapis.com/ServiceAccount: {r.status_code} {r.text[:160]}"
    out = [
        _record(
            name=sa.get("email"), displayName=sa.get("displayName") or sa.get("email"),
            assetType="iam.googleapis.com/ServiceAccount", project=project,
            location="global", state="DISABLED" if sa.get("disabled") else "ENABLED",
            description=sa.get("description"),
        )
        for sa in r.json().get("accounts", []) or []
    ]
    return out, None


_FALLBACK: dict[str, Callable] = {
    "compute.googleapis.com/Instance": _svc_compute_instances,
    "storage.googleapis.com/Bucket": _svc_storage_buckets,
    "iam.googleapis.com/ServiceAccount": _svc_service_accounts,
}


def _run_fallback(token: str, project: str, asset_types: str, limit: int) -> dict:
    requested = [a.strip() for a in asset_types.split(",") if a.strip()]
    notes: list[str] = []
    selected: list[tuple[str, Callable]] = []
    if requested:
        for at in requested:
            fn = _FALLBACK.get(at)
            if fn:
                selected.append((at, fn))
            else:
                notes.append(f"No direct fallback for asset type '{at}' (needs Cloud Asset Inventory).")
    else:
        selected = list(_FALLBACK.items())  # default common set

    resources: list[dict] = []
    errors: list[str] = []
    for _at, fn in selected:
        try:
            recs, err = fn(token, project, limit)
        except requests.RequestException as exc:
            err, recs = f"{_at}: request failed: {exc}", []
        if err:
            errors.append(err)
        resources.extend(recs)

    return {
        "resources": resources[:limit],
        "queried_services": [at for at, _ in selected],
        "service_errors": errors,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Tool entry point
# --------------------------------------------------------------------------- #
def query_gcp_assets(
    query: str = "",
    asset_types: str = "",
    scope: str = "",
    limit: int = 25,
    tool_context: Any = None,
) -> dict[str, Any]:
    """Search the user's existing GCP resources.

    Tries Cloud Asset Inventory first (broad, cross-resource). If CAI is not
    enabled in the target project, automatically falls back to querying common
    services directly (Compute instances, Storage buckets, IAM service accounts).
    All queries run with the user's permissions; never fabricate resource data —
    if the result is empty, say so.

    Args:
        query: Cloud Asset Inventory query, e.g. "state:RUNNING",
            "location:us-central1", "name:prod*". Used by the CAI path only.
        asset_types: Optional comma-separated asset types, e.g.
            "compute.googleapis.com/Instance,storage.googleapis.com/Bucket".
            Also selects which services the fallback queries.
        scope: "projects/ID", "folders/NUM", or "organizations/NUM"
            (defaults to the agent's project). The fallback supports project
            scope only.
        limit: Max resources to return (1-100).

    Returns:
        Structured dict: status, query_backend ("cloud-asset-inventory" or
        "per-service-fallback"), credential_source, scope, result_count,
        resources, plus notes/service_errors when the fallback was used.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project and not scope:
        return {"scope": None, "query": query, "credential_source": "none",
                "status": "error", "query_backend": None,
                "error": "GOOGLE_CLOUD_PROJECT is not set in the agent runtime.",
                "result_count": 0, "resources": []}
    scope = scope or f"projects/{project}"
    limit = max(1, min(int(limit or 25), 100))

    token, source = resolve_user_token(tool_context)
    base = {"scope": scope, "query": query, "credential_source": source}
    if not token:
        return {
            **base,
            "status": "unauthorized",
            "query_backend": None,
            "error": (
                "No end-user OAuth token was forwarded by Gemini Enterprise. This "
                "agent acts only with the user's own credentials (it never uses a "
                f"service account). Please authorize the agent (authorization id "
                f"'{AUTH_ID}') in Gemini Enterprise and retry."
            ),
            "available_state_keys": state_keys(tool_context),
            "result_count": 0,
            "resources": [],
        }

    ok, payload = _search_cai(token, project, scope, query, asset_types, limit)
    if ok:
        res = payload["resources"]
        return {**base, "status": "success", "query_backend": "cloud-asset-inventory",
                "result_count": len(res), "resources": res,
                "next_page_token": payload.get("next_page_token")}

    # CAI failed. If it's an unavailability/permission case, try the fallback.
    if not _cai_unavailable(payload):
        return {**base, "status": "error", "query_backend": "cloud-asset-inventory",
                "http_status": payload.get("http_status"),
                "error": f"Cloud Asset API error: {payload.get('error')}",
                "result_count": 0, "resources": []}

    # Per-service fallback (project scope only).
    fb_project = scope.split("/", 1)[1] if scope.startswith("projects/") else project
    extra_note = []
    if not scope.startswith("projects/"):
        extra_note.append(
            f"Fallback supports project scope only; queried project '{fb_project}' "
            f"instead of '{scope}'."
        )
    fb = _run_fallback(token, fb_project, asset_types, limit)
    return {
        **base,
        "status": "success" if fb["resources"] else "empty",
        "query_backend": "per-service-fallback",
        "cai_status": f"Cloud Asset Inventory unavailable ({payload.get('http_status')}). "
                      "Enable it for full coverage: "
                      "gcloud services enable cloudasset.googleapis.com",
        "queried_services": fb["queried_services"],
        "service_errors": fb["service_errors"],
        "notes": fb["notes"] + extra_note,
        "result_count": len(fb["resources"]),
        "resources": fb["resources"],
    }
