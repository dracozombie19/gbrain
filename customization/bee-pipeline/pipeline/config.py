"""Configuration and secret loading for the Bee → Brain pipeline."""

import os
from dataclasses import dataclass

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

    # GCS state bucket
    gcs_bucket: str
    gcs_state_object: str = "bee-pipeline/state.json"
    gcs_failed_prefix: str = "bee-pipeline/failed"

    # Vertex AI (Anthropic model)
    vertex_model: str = "claude-sonnet-4-6@20250514"

    # Pipeline tuning
    max_conversations_per_run: int = 50
    extraction_max_tokens: int = 4096



def load_config_from_env() -> Config:
    """Load config from environment variables (for local development)."""
    return Config(
        gcp_project=os.environ["GCP_PROJECT"],
        gcp_region=os.environ.get("GCP_REGION", "us-central1"),
        bee_token=os.environ["BEE_TOKEN"],
        brain_url=os.environ["BRAIN_URL"],
        brain_client_id=os.environ["BRAIN_CLIENT_ID"],
        brain_client_secret=os.environ["BRAIN_CLIENT_SECRET"],
        gcs_bucket=os.environ["GCS_BUCKET"],
        vertex_model=os.environ.get("VERTEX_MODEL", "claude-sonnet-4-6@20250514"),
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
