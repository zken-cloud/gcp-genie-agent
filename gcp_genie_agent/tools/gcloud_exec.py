"""Confirmed execution of a curated, extensible set of non-destructive gcloud ops.

No gcloud binary runs here (the SDK is 1.4 GB; the runtime is a Python sandbox).
Each supported operation is mapped to a Google REST API and executed with the
END-USER's OAuth token only — never the agent service account.

Safety model (layered):
  * Destructive operations (delete/destroy/remove/...) are NEVER executed — the
    validated command is returned for the user to run manually.
  * Only operations with a known REST mapping can run. Each mapping is either
    allowed by default or "gated" (sensitive, e.g. IAM changes) and must be
    enabled at runtime by the user via `allow_gcloud_operation` (with their
    confirmation). Enabled ops are stored in session state for the conversation.
  * Execution itself requires a two-step human confirmation: the tool refuses
    unless user_acknowledged_impact=True AND user_confirmed_execution=True.
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Callable

import requests

from .asset_query import _state_dict, resolve_user_token
from .gcloud_validator import validate_gcloud_command

_DESTRUCTIVE_PREFIXES = ("delete", "destroy", "remove", "abandon", "purge", "uninstall")
_ALLOWED_OPS_STATE_KEY = "gcp_genie_extra_allowed_ops"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _parse(command: str) -> tuple[list[str], dict[str, str]]:
    """Return (non-flag tokens [command path + positionals], flags)."""
    toks = shlex.split(command)
    if toks and toks[0] == "gcloud":
        toks = toks[1:]
    tokens: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("--"):
            name, eq, val = t.partition("=")
            if eq:
                flags[name] = val
            elif i + 1 < len(toks) and not toks[i + 1].startswith("--"):
                flags[name] = toks[i + 1]
                i += 1
            else:
                flags[name] = ""
        else:
            tokens.append(t)
        i += 1
    return tokens, flags


def _is_destructive(tokens: list[str]) -> bool:
    return any(any(t.startswith(p) for p in _DESTRUCTIVE_PREFIXES) for t in tokens)


def _project(flags: dict[str, str], fallback: str | None = None) -> str:
    return flags.get("--project") or fallback or os.environ.get("GOOGLE_CLOUD_PROJECT") or ""


def _post(url: str, token: str, project: str, body: dict | None = None) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}", "X-Goog-User-Project": project}
    return requests.post(url, headers=headers, json=body or {}, timeout=60)


def _tokeninfo(token: str) -> dict[str, Any]:
    """Report an access token's scopes/audience/identity (never the token itself)."""
    try:
        r = requests.get("https://oauth2.googleapis.com/tokeninfo",
                         params={"access_token": token}, timeout=15)
    except requests.RequestException as exc:
        return {"error": f"tokeninfo request failed: {exc}"}
    if r.status_code != 200:
        return {"error": f"tokeninfo {r.status_code}: {r.text[:200]}"}
    d = r.json()
    return {
        "scopes": (d.get("scope") or "").split(),
        "audience": d.get("aud"),
        "issued_to": d.get("azp") or d.get("issued_to"),
        "email": d.get("email"),
        "expires_in": d.get("expires_in"),
        "has_cloud_platform": "https://www.googleapis.com/auth/cloud-platform" in (d.get("scope") or ""),
    }


def _result(resp: requests.Response, summary: str) -> dict[str, Any]:
    ok = resp.status_code < 300
    out: dict[str, Any] = {"status": "success" if ok else "error",
                           "http_status": resp.status_code, "operation": summary}
    try:
        out["response"] = resp.json()
    except ValueError:
        out["response"] = resp.text[:600]
    if not ok:
        out["error"] = (resp.text or "")[:600]
    return out


# --------------------------------------------------------------------------- #
# Operation handlers (non-destructive only)
# --------------------------------------------------------------------------- #
def _h_instance_power(tokens, flags, token):  # start | stop | reset
    verb, name, zone = tokens[2], (tokens[3] if len(tokens) > 3 else None), flags.get("--zone")
    if not name or not zone:
        return {"status": "error", "error": "Need an instance name and --zone."}
    project = _project(flags)
    url = (f"https://compute.googleapis.com/compute/v1/projects/{project}"
           f"/zones/{zone}/instances/{name}/{verb}")
    return _result(_post(url, token, project), f"{verb} instance '{name}' in {zone}")


def _h_instance_create(tokens, flags, token):
    name, zone = (tokens[3] if len(tokens) > 3 else None), flags.get("--zone")
    if not name or not zone:
        return {"status": "error", "error": "Need an instance name and --zone."}
    project = _project(flags)
    mtype = flags.get("--machine-type", "e2-medium")
    fam = flags.get("--image-family", "debian-12")
    fam_proj = flags.get("--image-project", "debian-cloud")
    network = flags.get("--network", "default")
    body: dict[str, Any] = {
        "name": name,
        "machineType": f"zones/{zone}/machineTypes/{mtype}",
        "disks": [{"boot": True, "autoDelete": True,
                   "initializeParams": {"sourceImage": f"projects/{fam_proj}/global/images/family/{fam}"}}],
        "networkInterfaces": [{"network": f"global/networks/{network}"}],
    }
    if flags.get("--subnet"):
        body["networkInterfaces"][0]["subnetwork"] = flags["--subnet"]
    if flags.get("--tags"):
        body["tags"] = {"items": [t for t in flags["--tags"].split(",") if t]}
    url = f"https://compute.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances"
    return _result(_post(url, token, project, body), f"create instance '{name}' ({mtype}) in {zone}")


def _h_bucket_create(tokens, flags, token):
    target = tokens[3] if len(tokens) > 3 else ""
    name = target.replace("gs://", "").strip("/")
    if not name:
        return {"status": "error", "error": "Need a bucket name (gs://NAME)."}
    project = _project(flags)
    body: dict[str, Any] = {"name": name}
    if flags.get("--location"):
        body["location"] = flags["--location"].upper()
    if flags.get("--default-storage-class"):
        body["storageClass"] = flags["--default-storage-class"]
    url = f"https://storage.googleapis.com/storage/v1/b?project={project}"
    return _result(_post(url, token, project, body), f"create bucket '{name}'")


def _h_services_enable(tokens, flags, token):
    services = tokens[2:]
    if not services:
        return {"status": "error", "error": "Need at least one service to enable."}
    project = _project(flags)
    if len(services) == 1:
        url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services/{services[0]}:enable"
        resp = _post(url, token, project)
    else:
        url = f"https://serviceusage.googleapis.com/v1/projects/{project}/services:batchEnable"
        resp = _post(url, token, project, {"serviceIds": services})
    return _result(resp, f"enable service(s): {', '.join(services)}")


def _h_sa_create(tokens, flags, token):
    account_id = tokens[3] if len(tokens) > 3 else None
    if not account_id:
        return {"status": "error", "error": "Need a service account id."}
    project = _project(flags)
    body = {"accountId": account_id,
            "serviceAccount": {"displayName": flags.get("--display-name", account_id)}}
    url = f"https://iam.googleapis.com/v1/projects/{project}/serviceAccounts"
    return _result(_post(url, token, project, body), f"create service account '{account_id}'")


def _h_network_create(tokens, flags, token):
    name = tokens[3] if len(tokens) > 3 else None
    if not name:
        return {"status": "error", "error": "Need a network name."}
    project = _project(flags)
    mode = flags.get("--subnet-mode", "auto")
    body: dict[str, Any] = {"name": name, "autoCreateSubnetworks": mode == "auto"}
    if flags.get("--bgp-routing-mode"):
        body["routingConfig"] = {"routingMode": flags["--bgp-routing-mode"].upper()}
    url = f"https://compute.googleapis.com/compute/v1/projects/{project}/global/networks"
    return _result(_post(url, token, project, body), f"create network '{name}' ({mode})")


def _h_project_add_binding(tokens, flags, token):
    proj = tokens[2] if len(tokens) > 2 else _project(flags)
    member, role = flags.get("--member"), flags.get("--role")
    if not member or not role:
        return {"status": "error", "error": "Need --member and --role."}
    headers = {"Authorization": f"Bearer {token}", "X-Goog-User-Project": proj}
    base = f"https://cloudresourcemanager.googleapis.com/v1/projects/{proj}"
    get = requests.post(f"{base}:getIamPolicy", headers=headers,
                        json={"options": {"requestedPolicyVersion": 3}}, timeout=60)
    if get.status_code >= 300:
        return _result(get, f"read IAM policy of '{proj}'")
    policy = get.json()
    for b in policy.setdefault("bindings", []):
        if b.get("role") == role:
            if member not in b.setdefault("members", []):
                b["members"].append(member)
            break
    else:
        policy["bindings"].append({"role": role, "members": [member]})
    setp = requests.post(f"{base}:setIamPolicy", headers=headers, json={"policy": policy}, timeout=60)
    return _result(setp, f"grant {role} to {member} on '{proj}'")


# op_key -> (matcher, handler, default_allowed, human description)
def _pfx(tokens, *parts):
    return tokens[: len(parts)] == list(parts)


_OPERATIONS: dict[str, dict[str, Any]] = {
    "compute.instances.power": {
        "match": lambda t: _pfx(t, "compute", "instances") and len(t) > 2 and t[2] in ("start", "stop", "reset"),
        "handler": _h_instance_power, "default": True, "desc": "start/stop/reset a Compute instance"},
    "compute.instances.create": {
        "match": lambda t: _pfx(t, "compute", "instances", "create"),
        "handler": _h_instance_create, "default": True, "desc": "create a Compute Engine VM"},
    "storage.buckets.create": {
        "match": lambda t: _pfx(t, "storage", "buckets", "create"),
        "handler": _h_bucket_create, "default": True, "desc": "create a Cloud Storage bucket"},
    "services.enable": {
        "match": lambda t: _pfx(t, "services", "enable"),
        "handler": _h_services_enable, "default": True, "desc": "enable a Google Cloud API/service"},
    "iam.serviceAccounts.create": {
        "match": lambda t: _pfx(t, "iam", "service-accounts", "create"),
        "handler": _h_sa_create, "default": True, "desc": "create an IAM service account"},
    "compute.networks.create": {
        "match": lambda t: _pfx(t, "compute", "networks", "create"),
        "handler": _h_network_create, "default": True, "desc": "create a VPC network"},
    "projects.iam.addBinding": {
        "match": lambda t: _pfx(t, "projects", "add-iam-policy-binding"),
        "handler": _h_project_add_binding, "default": False,
        "desc": "grant an IAM role on a project (sensitive — gated by default)"},
}


def _match_op(tokens: list[str]) -> str | None:
    for key, spec in _OPERATIONS.items():
        if spec["match"](tokens):
            return key
    return None


def _extra_allowed(tool_context: Any) -> set[str]:
    val = _state_dict(tool_context).get(_ALLOWED_OPS_STATE_KEY)
    return set(val) if isinstance(val, list) else set()


def _is_allowed(op_key: str, tool_context: Any) -> bool:
    return _OPERATIONS[op_key]["default"] or op_key in _extra_allowed(tool_context)


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def list_executable_operations(tool_context: Any = None) -> dict[str, Any]:
    """List which gcloud operations this agent can execute.

    Returns the operations allowed by default, any extra operations the user has
    enabled this session, and gated operations that are available to enable via
    `allow_gcloud_operation`.
    """
    extra = _extra_allowed(tool_context)
    default = {k: s["desc"] for k, s in _OPERATIONS.items() if s["default"]}
    enabled = {k: _OPERATIONS[k]["desc"] for k in extra if k in _OPERATIONS}
    gateable = {k: s["desc"] for k, s in _OPERATIONS.items() if not s["default"] and k not in extra}
    return {"default_allowed": default, "enabled_this_session": enabled,
            "available_to_enable": gateable,
            "note": "Destructive operations (delete/remove/destroy) are never executable."}


def allow_gcloud_operation(operation_key: str, user_confirmed: bool = False,
                           tool_context: Any = None) -> dict[str, Any]:
    """Enable an additional (gated) operation type for this conversation.

    Use when the user wants the agent to be able to execute an operation that is
    not allowed by default (e.g. IAM changes). Call once with user_confirmed=False
    to describe what enabling it permits; only after the user explicitly agrees,
    call again with user_confirmed=True to enable it. Enabling does NOT execute
    anything — every execution still requires the full per-command confirmation.

    Args:
        operation_key: One of the keys from list_executable_operations
            (e.g. "projects.iam.addBinding").
        user_confirmed: True only after the user agreed to enable this op type.
    """
    if operation_key not in _OPERATIONS:
        return {"status": "unknown_operation", "operation_key": operation_key,
                "message": ("No execution mapping exists for this operation, so it cannot be "
                            "enabled for direct execution. It can still be generated for manual run."),
                "available": list(_OPERATIONS.keys())}
    spec = _OPERATIONS[operation_key]
    if spec["default"]:
        return {"status": "already_allowed", "operation_key": operation_key,
                "message": f"'{operation_key}' ({spec['desc']}) is already allowed by default."}
    if not user_confirmed:
        return {"status": "confirmation_required", "operation_key": operation_key,
                "description": spec["desc"],
                "message": ("Ask the user to confirm enabling this operation type for the session. "
                            "Enabling it lets the agent EXECUTE such commands (still with per-command "
                            "confirmation, and still only with the user's own permissions).")}
    cur = _extra_allowed(tool_context)
    cur.add(operation_key)
    try:
        tool_context.state[_ALLOWED_OPS_STATE_KEY] = sorted(cur)
    except Exception:  # pragma: no cover - state may be read-only in some contexts
        return {"status": "error", "operation_key": operation_key,
                "message": "Could not persist the enabled operation in this session."}
    return {"status": "enabled", "operation_key": operation_key,
            "enabled_this_session": sorted(cur),
            "message": f"'{operation_key}' is now executable this session (with per-command confirmation)."}


def execute_gcloud_command(
    command: str,
    user_acknowledged_impact: bool = False,
    user_confirmed_execution: bool = False,
    tool_context: Any = None,
) -> dict[str, Any]:
    """Execute a non-destructive gcloud operation with the user's own permissions.

    STRICT process — follow it exactly; do not skip steps:
      1. First call this with both flags FALSE to get the operation summary/impact.
         Show the user the exact command, explain what it does and what it will
         change, ask clarifying questions, and warn it modifies their live GCP env.
      2. Only after the user explicitly confirms the command AND acknowledges it
         will change their environment, set user_acknowledged_impact=True.
      3. Only after the user then explicitly confirms execution NOW, also set
         user_confirmed_execution=True to actually run it.
    Handle returned status:
      * blocked_destructive / unsupported -> do NOT execute; give the user the
        manual_command to run themselves.
      * operation_not_enabled -> the op is gated; offer to enable it via
        `allow_gcloud_operation` (which requires the user's confirmation), then retry.

    Args:
        command: The full gcloud command to execute.
        user_acknowledged_impact: True only after step 2.
        user_confirmed_execution: True only after step 3.
    """
    v = validate_gcloud_command(command)
    base = {"command": command, "resolved_command": v.get("resolved_command")}
    if not v["valid"]:
        return {**base, "status": "invalid", "errors": v["errors"]}

    tokens, flags = _parse(command)

    if _is_destructive(tokens):
        return {**base, "status": "blocked_destructive",
                "message": ("Destructive operation — this agent never executes these. Run it "
                            "yourself after review:"), "manual_command": command}

    op_key = _match_op(tokens)
    if not op_key:
        return {**base, "status": "unsupported",
                "message": ("No execution mapping for this operation yet. It is validated and "
                            "safe to run manually:"), "manual_command": command}

    if not _is_allowed(op_key, tool_context):
        return {**base, "status": "operation_not_enabled", "operation_key": op_key,
                "description": _OPERATIONS[op_key]["desc"],
                "message": (f"Execution of '{op_key}' is gated and not enabled for this session. "
                            f"Ask the user, then enable it via allow_gcloud_operation before executing.")}

    if not (user_acknowledged_impact and user_confirmed_execution):
        missing = []
        if not user_acknowledged_impact:
            missing.append("user must confirm the command AND acknowledge it will change their GCP environment")
        if not user_confirmed_execution:
            missing.append("user must then explicitly confirm execution")
        return {**base, "status": "confirmation_required", "operation_key": op_key,
                "is_mutating": True, "pending_confirmations": missing,
                "message": ("Do NOT execute yet. Show the command, explain its impact, get the "
                            "user's acknowledgement, then explicit execution confirmation, before "
                            "calling again with both flags true.")}

    token, source = resolve_user_token(tool_context)
    if not token:
        return {**base, "status": "unauthorized", "credential_source": source,
                "error": ("No end-user OAuth token was forwarded. This agent executes only with the "
                          "user's own credentials. Please authorize the agent and retry.")}

    result = _OPERATIONS[op_key]["handler"](tokens, flags, token)
    result.update(base)
    result["operation_key"] = op_key
    result["credential_source"] = source

    if result.get("status") == "error":
        err = result.get("error") or ""
        stale = result.get("http_status") == 401 or any(
            m in err for m in ("ACCESS_TOKEN_TYPE_UNSUPPORTED", "UNAUTHENTICATED",
                               "invalid authentication", "invalid_token", "Invalid Credentials"))
        if stale:
            # Forwarded token is expired/stale — common in older GE chats, NOT a
            # permission or code problem. Give the user a clear, plain next step.
            result["status"] = "authorization_stale"
            result["user_message"] = (
                "Your authorization for this chat looks expired or stale (this often happens in "
                "older chats). Please start a NEW chat — or re-authorize the agent — and try again. "
                "Your Google Cloud permissions are fine; only the session's sign-in needs refreshing.")
            result["token_diagnostics"] = _tokeninfo(token)
        elif result.get("http_status") == 403:
            result["user_message"] = (
                "Permission denied: your account doesn't have the IAM role required for this "
                "operation on this project or scope.")
    return result


def _tokeninfo_as(kind: str, token: str) -> dict[str, Any]:
    try:
        r = requests.get("https://oauth2.googleapis.com/tokeninfo", params={kind: token}, timeout=15)
    except requests.RequestException as exc:
        return {"valid": False, "error": f"request failed: {exc}"}
    if r.status_code != 200:
        return {"valid": False, "http_status": r.status_code, "error": r.text[:200]}
    d = r.json()
    return {"valid": True, "scopes": (d.get("scope") or "").split() or None,
            "aud": d.get("aud"), "azp": d.get("azp"), "email": d.get("email"),
            "iss": d.get("iss"), "expires_in": d.get("expires_in")}


def _bearer_read(token: str, project: str) -> dict[str, Any]:
    try:
        r = requests.get("https://cloudresourcemanager.googleapis.com/v1/projects",
                         headers={"Authorization": f"Bearer {token}", "X-Goog-User-Project": project},
                         params={"pageSize": 1}, timeout=20)
    except requests.RequestException as exc:
        return {"error": f"request failed: {exc}"}
    return {"http_status": r.status_code, "ok": r.status_code < 300,
            "detail": "OK" if r.status_code < 300 else r.text[:200]}


def inspect_user_token(tool_context: Any = None) -> dict[str, Any]:
    """Diagnostic: characterize the end-user's forwarded credential (never returns it).

    Use ONLY when debugging an authorization/execution problem — not during normal
    requests. When relaying to the user, show only the plain-language `summary`
    field; never show the `details` block. Returns a `healthy` boolean, a `summary`,
    and a `details` object (prefix/length/classification, tokeninfo as access vs id
    token, and a live Bearer read test).
    """
    token, source = resolve_user_token(tool_context)
    if not token:
        return {"status": "unauthorized", "credential_source": source,
                "message": "No end-user credential was forwarded by Gemini Enterprise."}
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or ""
    if token.startswith("ya29."):
        classification = "oauth_access_token (ya29.*)"
    elif token.startswith("ey"):
        classification = "JWT — likely an ID token, NOT an access token (ey*)"
    else:
        classification = "unknown credential type"

    access = _tokeninfo_as("access_token", token)
    read = _bearer_read(token, project)
    healthy = bool(access.get("valid")) and bool(read.get("ok"))
    email = access.get("email")
    if healthy:
        summary = (f"You're signed in{f' as {email}' if email else ''} and your authorization is "
                   "active for Google Cloud actions on your behalf.")
    else:
        summary = ("Your authorization for this chat looks expired or stale. Please start a new chat "
                   "or re-authorize the agent, then try again — your GCP permissions are fine.")

    return {
        "status": "ok",
        # Plain-language line for the end user; surface THIS, not the raw details.
        "summary": summary,
        "healthy": healthy,
        "credential_source": source,
        # Technical details for debugging only — do not show raw to end users.
        "details": {
            "token_prefix": token[:4],
            "token_length": len(token),
            "classification": classification,
            "tokeninfo_as_access_token": access,
            "tokeninfo_as_id_token": _tokeninfo_as("id_token", token),
            "live_read_test_projects_list": read,
        },
    }
