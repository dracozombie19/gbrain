"""Configuration and secret loading for the Bee → Brain pipeline."""

import os
from dataclasses import dataclass, field
from typing import Optional

from google.cloud import secretmanager


def _load_secret(client: secretmanager.SecretManagerServiceClient, project: str, name: str) -> str:
    path = f"projects/{project}/secrets/{name}/versions/latest"
    response = client.access_secret_version(request={"name": path})
    return response.payload.data.decode("utf-8").strip()


@dataclass
class Config:
    # GCP
    gcp_project: str
    gcp_region: str

    # Bee CLI token (JWT, no expiry — store in Secret Manager as BEE_TOKEN)
    bee_token: str

    # Brain HTTP MCP server
    brain_url: str
    brain_client_id: str
    brain_client_secret: str

    # GCS state bucket (empty string when using local_state_path)
    gcs_bucket: str
    gcs_state_object: str = "bee-pipeline/state.json"
    gcs_failed_prefix: str = "bee-pipeline/failed"

    # Vertex AI (Anthropic model)
    vertex_model: str = "claude-sonnet-4-6@20250514"

    # Pipeline tuning
    max_conversations_per_run: int = 50
    extraction_max_tokens: int = 4096

    # --- Dry-run / local dev options ---

    # When True: extraction runs normally but Brain writes are logged, not executed.
    dry_run: bool = False

    # Path to a local JSON file for cursor state. When set, GCS is not used.
    # Defaults to "./bee-pipeline-state.json" in dry-run mode when GCS_BUCKET is unset.
    local_state_path: Optional[str] = None

    # Direct Anthropic API key. When set, uses the Anthropic API directly instead of
    # Vertex AI. Useful for local testing without GCP Application Default Credentials.
    anthropic_api_key: Optional[str] = None


def load_config_from_env() -> Config:
    """Load config from environment variables (for local development).

    Dry-run mode (DRY_RUN=1):
      - Brain writes are skipped (logged only).
      - LOCAL_STATE_PATH defaults to ./bee-pipeline-state.json so GCS_BUCKET is not required.
      - ANTHROPIC_API_KEY can be used instead of Vertex AI (no GCP credentials needed).
      - BRAIN_URL / BRAIN_CLIENT_ID / BRAIN_CLIENT_SECRET are optional; when omitted the
        resolver loads no aliases and all facts route to pending-review (logged only).

    Full mode (default):
      - GCS_BUCKET, BRAIN_URL, BRAIN_CLIENT_ID, BRAIN_CLIENT_SECRET are required.
      - Either GCP_PROJECT (Vertex AI) or ANTHROPIC_API_KEY must be set.
    """
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

    # State storage: prefer local file over GCS in dry-run mode
    local_state_path = os.environ.get("LOCAL_STATE_PATH")
    gcs_bucket = os.environ.get("GCS_BUCKET", "")

    if not gcs_bucket and not local_state_path:
        if dry_run:
            local_state_path = "./bee-pipeline-state.json"
        else:
            raise ValueError(
                "GCS_BUCKET must be set (or set LOCAL_STATE_PATH for local state, "
                "or DRY_RUN=1 to auto-use ./bee-pipeline-state.json)"
            )

    # Extraction backend: either Vertex AI (needs GCP_PROJECT) or direct Anthropic API
    gcp_project = os.environ.get("GCP_PROJECT", "")
    if not gcp_project and not anthropic_api_key:
        raise ValueError(
            "Either GCP_PROJECT (for Vertex AI) or ANTHROPIC_API_KEY must be set"
        )

    # Brain connection: required in full mode, optional in dry-run
    brain_url = os.environ.get("BRAIN_URL", "")
    brain_client_id = os.environ.get("BRAIN_CLIENT_ID", "")
    brain_client_secret = os.environ.get("BRAIN_CLIENT_SECRET", "")

    if not dry_run:
        missing = [k for k, v in {
            "BRAIN_URL": brain_url,
            "BRAIN_CLIENT_ID": brain_client_id,
            "BRAIN_CLIENT_SECRET": brain_client_secret,
        }.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required env vars in full mode: {', '.join(missing)}. "
                "Set DRY_RUN=1 to skip Brain writes and make these optional."
            )

    return Config(
        gcp_project=gcp_project,
        gcp_region=os.environ.get("GCP_REGION", "us-central1"),
        bee_token=os.environ["BEE_TOKEN"],
        brain_url=brain_url,
        brain_client_id=brain_client_id,
        brain_client_secret=brain_client_secret,
        gcs_bucket=gcs_bucket,
        vertex_model=os.environ.get("VERTEX_MODEL", "claude-sonnet-4-6@20250514"),
        dry_run=dry_run,
        local_state_path=local_state_path,
        anthropic_api_key=anthropic_api_key,
    )


def load_config_from_secret_manager() -> Config:
    """Load config from GCP Secret Manager (for Cloud Function deployment)."""
    project = os.environ["GCP_PROJECT"]
    region = os.environ.get("GCP_REGION", "us-central1")
    client = secretmanager.SecretManagerServiceClient()

    def secret(name: str) -> str:
        return _load_secret(client, project, name)

    return Config(
        gcp_project=project,
        gcp_region=region,
        bee_token=secret("BEE_API_TOKEN"),
        brain_url=secret("BRAIN_URL"),
        brain_client_id=secret("BRAIN_CLIENT_ID"),
        brain_client_secret=secret("BRAIN_CLIENT_SECRET"),
        gcs_bucket=os.environ["GCS_BUCKET"],
        vertex_model=os.environ.get("VERTEX_MODEL", "claude-sonnet-4-6@20250514"),
    )


def load_config() -> Config:
    """Auto-detect environment: use Secret Manager in GCP, env vars locally."""
    if os.environ.get("K_SERVICE"):
        # Running in Cloud Run / Cloud Functions gen2
        return load_config_from_secret_manager()
    return load_config_from_env()
