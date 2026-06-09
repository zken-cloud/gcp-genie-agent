"""Create or update the GCP Genie agent on Vertex AI Agent Engine.

All configuration comes from environment variables (set by deploy.sh). On
success the reasoning engine resource name is printed to stdout as the last line
and written to AGENT_ENGINE_RESOURCE_FILE if set.
"""

import os
import sys

import vertexai
from vertexai import agent_engines
from vertexai.preview.reasoning_engines import AdkApp


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"ERROR: required environment variable {name} is not set.")
    return val


def main() -> None:
    project = _require("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("AGENT_ENGINE_LOCATION", "us-central1")
    bucket = _require("STAGING_BUCKET")
    model = os.environ.get("GCP_GENIE_MODEL", "gemini-3.5-flash")
    model_location = os.environ.get("GCP_GENIE_MODEL_LOCATION", "global")
    update_target = os.environ.get("AGENT_ENGINE_RESOURCE") or None

    requirements = [
        "google-adk==1.34.3",
        "google-cloud-aiplatform[agent_engines,adk]==1.154.0",
        "google-genai==1.75.0",
        "requests>=2.31.0",
    ]
    env_vars = {"GCP_GENIE_MODEL": model, "GCP_GENIE_MODEL_LOCATION": model_location}

    vertexai.init(project=project, location=location, staging_bucket=bucket)

    # Import after init so the agent picks up configuration.
    from gcp_genie_agent.agent import root_agent

    app = AdkApp(agent=root_agent, enable_tracing=True)
    common = dict(
        requirements=requirements,
        extra_packages=["gcp_genie_agent"],
        env_vars=env_vars,
        display_name=os.environ.get("AGENT_DISPLAY_NAME", "GCP Genie"),
        description=(
            "GCP assistant: documentation Q&A, validated gcloud script generation, "
            "confirmed non-destructive execution, and live asset inventory queries."
        ),
    )

    if update_target:
        print(f"Updating Agent Engine: {update_target}", file=sys.stderr)
        remote = agent_engines.update(resource_name=update_target, agent_engine=app, **common)
    else:
        print(f"Creating Agent Engine in {project}/{location} ...", file=sys.stderr)
        remote = agent_engines.create(agent_engine=app, **common)

    out_file = os.environ.get("AGENT_ENGINE_RESOURCE_FILE")
    if out_file:
        with open(out_file, "w") as fh:
            fh.write(remote.resource_name + "\n")
    # Last stdout line = resource name (consumed by deploy.sh).
    print(remote.resource_name)


if __name__ == "__main__":
    main()
