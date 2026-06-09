# GCP Genie

**English** | [简体中文](README.zh-CN.md)

An [ADK](https://google.github.io/adk-docs/) agent that runs on **Vertex AI Agent
Engine** and registers to a **Gemini Enterprise** app. It helps users work with
Google Cloud in four ways — and every action that touches a user's cloud runs
with **that user's own OAuth permissions**, never a service account.

## What it does

| # | Capability | How it stays trustworthy |
|---|------------|--------------------------|
| 1 | **GCP documentation Q&A** | Grounded in public docs via Google Search; answers cite their sources. |
| 2 | **gcloud script generation** | Always asks for parameters; emits clearly-marked placeholders otherwise. Every command is checked against gcloud's own command/flag tree before it's shown — no hallucinated flags. |
| 3 | **Live asset inventory queries** | Real data from Cloud Asset Inventory (with a per-service REST fallback), scoped to the signed-in user's IAM. |
| 4 | **Confirmed command execution** | Executes a curated set of **non-destructive** operations with the user's token, behind a **3-layer confirmation**. Destructive operations are never executed. |

## Architecture

- **ADK agent** (`gcp_genie_agent/`): a root agent that coordinates a Google-Search
  sub-agent plus function tools for validation, asset queries, and execution.
- **No `gcloud` binary at runtime.** The Agent Engine runtime is a Python sandbox,
  so command **validation** uses gcloud's bundled static command tree and command
  **execution** maps to Google REST APIs — both work without the CLI installed.
- **User-OAuth only.** Gemini Enterprise forwards the end-user's OAuth token; the
  agent reads it from session state and uses it for all cloud calls. If no user
  token is present it returns *unauthorized* — it never falls back to a service
  account.

---

## Prerequisites

1. **A Google Cloud project** with billing enabled.
2. **The [Google Cloud SDK](https://cloud.google.com/sdk/docs/install)** (`gcloud`)
   installed and authenticated: `gcloud auth login`. (The deploy script reads
   gcloud's bundled command tree for offline validation and uses your gcloud
   identity to register the agent.)
3. **Python 3.10+**.
4. **A Gemini Enterprise app** — note its **app id**.
5. **An OAuth 2.0 Client ID** (see below) so the agent can act on behalf of users.

### Create the OAuth client (one-time)

In the GCP console → **APIs & Services → Credentials**:

1. Configure the **OAuth consent screen** (Internal is fine for a single org) and
   add the scope `https://www.googleapis.com/auth/cloud-platform` (plus
   `.../auth/userinfo.email`).
2. Create an **OAuth client ID** of type **Web application**.
3. Add this **Authorized redirect URI** exactly:
   ```
   https://vertexaisearch.cloud.google.com/static/oauth/oauth.html
   ```
4. Copy the **Client ID** and **Client secret** — you'll paste them into the
   deploy script.

---

## Deploy

One interactive command does everything — enables APIs, creates the staging
bucket, bundles the CLI tree, deploys to Agent Engine, and registers to Gemini
Enterprise:

```bash
./deploy.sh
```

It will prompt for (most have sensible defaults):

| Prompt | Default | Notes |
|--------|---------|-------|
| GCP project id | current `gcloud` project | |
| Agent Engine region | `us-central1` | |
| Staging bucket | `gs://<project>-agent-staging` | created if missing |
| Gemini model | `gemini-3.5-flash` | |
| Model region | `global` | gemini-3.x is served from the `global` endpoint |
| Gemini Enterprise app id | — | required |
| OAuth authorization id | `gcp-genie-oauth` | |
| OAuth client id / secret | — | required; secret is read hidden |
| OAuth scopes | `cloud-platform email` | |
| Existing reasoning engine to update | blank | blank = create new; paste a resource name to update in place |

Every prompt can be pre-set with an environment variable of the same name for a
non-interactive run, e.g.:

```bash
GOOGLE_CLOUD_PROJECT=my-proj GE_APP_ID=my-app_123 \
OAUTH_CLIENT_ID=...apps.googleusercontent.com OAUTH_CLIENT_SECRET=... \
ASSUME_YES=1 ./deploy.sh
```

After it finishes, open your Gemini Enterprise app, **authorize the agent** when
prompted (this is what mints the user's delegated token), and start chatting.

---

## Using the agent — sample prompts

**1 · Documentation Q&A**
- "What's the difference between a regional and multi-regional Cloud Storage bucket?"
- "How does Cloud Run autoscaling work, and what are the limits?"

**2 · gcloud generation** (the agent asks for the parameters it needs)
- "Give me a gcloud command to create an e2-medium VM."
- "Script to create a regional GCS bucket with uniform bucket-level access."

**3 · Asset inventory** (uses your permissions)
- "List my service accounts."
- "Which Compute instances are running in us-central1?"
- "Show buckets labelled env=prod."

**4 · Execution** (with confirmation — see below)
- "Create a VM `my-vm` in us-central1-a, e2-medium — and run it."
- "Enable the Cloud Asset API for me."
- "Stop instance `web-1` in us-central1-a."

You can also ask: **"what can you execute?"** to see the allow-list.

---

## The 3-layer execution confirmation

Execution is deliberately cautious. When you ask the agent to run something:

1. **Explain.** The agent shows the exact command, explains in plain language what
   it does and what it will change, asks clarifying questions, and warns it will
   modify your live GCP environment. (Destructive operations stop here — they are
   never executed; you get the command to run yourself.)
2. **Acknowledge.** You confirm the command is correct **and** acknowledge it will
   change your environment.
3. **Confirm execution.** Only after a final, explicit "yes, execute now" does the
   agent run it — with your own OAuth permissions.

The agent will not collapse these steps even if you pre-confirm everything in one
message.

## Adding more allowed operations

Execution is limited to a curated allow-list of **non-destructive** operations.
Out of the box:

- `compute instances start | stop | reset`
- `compute instances create`
- `compute networks create`
- `storage buckets create`
- `services enable`
- `iam service-accounts create`

Some sensitive-but-non-destructive operations are **gated** (off by default), e.g.
`projects add-iam-policy-binding`. To enable a gated operation for your
conversation, just ask — the agent will describe what enabling it allows and turn
it on only after you confirm (`allow_gcloud_operation`). It still runs behind the
full 3-layer confirmation and only with your permissions. Ask **"what can you
execute, and what can I enable?"** to see the current state.

To add brand-new operations permanently, add a handler + registry entry in
[`gcp_genie_agent/tools/gcloud_exec.py`](gcp_genie_agent/tools/gcloud_exec.py)
(see `_OPERATIONS`).

## Security model

- **User-OAuth only** — all cloud calls use the signed-in user's forwarded token;
  the agent never uses its service account or ADC. No user token ⇒ *unauthorized*.
- **Destructive operations are never executed** (delete/destroy/remove/…); they are
  returned as a command to run manually.
- **Deterministic validation** of every generated command against gcloud's own
  command/flag tree.
- **No secrets in the repo.** The OAuth client secret is entered at deploy time and
  passed via environment only.

## Troubleshooting

- **"Authorization looks expired/stale" or `ACCESS_TOKEN_TYPE_UNSUPPORTED`** — the
  forwarded token can go stale in older/long-running chats. **Start a new chat** (or
  re-authorize the agent) and retry. (See ADK issue
  [#5556](https://github.com/google/adk-python/issues/5556).) You can ask
  *"inspect my token"* for a plain-language health check.
- **Gemini Enterprise shows "no valid RunAgentResponse … stream data of size 0"** —
  a dependency-version skew. Keep the pinned versions in
  [`requirements.txt`](requirements.txt) (`google-adk==1.34.3`,
  `google-cloud-aiplatform[agent_engines,adk]==1.154.0`); newer aiplatform
  releases pass an argument the pinned ADK's runner doesn't accept on the Gemini
  Enterprise call path.
- **Model 404** — `gemini-3.x` is served from the `global` region; keep model
  region = `global`.

## Repository layout

```
gcp_genie_agent/
  agent.py                 # root agent + search sub-agent + tool wiring
  tools/
    gcloud_validator.py    # deterministic gcloud syntax validation
    asset_query.py         # Cloud Asset Inventory + per-service fallback (user OAuth)
    gcloud_exec.py         # confirmed execution, allow-list, token diagnostics
  data/                    # gcloud_completions.py is fetched here by deploy.sh
scripts/
  deploy_agent_engine.py       # create/update the Agent Engine deployment
  register_gemini_enterprise.py # create OAuth authorization + register the agent
deploy.sh                  # one-shot interactive deploy
requirements.txt
```

## License

Apache License 2.0 — see [LICENSE](LICENSE). GCP Genie bundles Google Cloud SDK
command-tree data (`gcloud_completions.py`) at deploy time from your local SDK
install; that file is Apache-2.0 licensed by Google and is not redistributed in
this repository.
