"""Configuration and secret loading for the Bee → Brain pipeline."""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    # GCP
    gcp_project: str
    gcp_region: str

    # Bee CLI token (JWT, no expiry — store in Secret Manager as BEE_API_TOKEN)
    bee_token: str

    # Brain HTTP MCP server
    brain_url: str
    brain_client_id: str
    brain_client_secret: str

    # GCS state bucket (empty string when using local_state_path)
    gcs_bucket: str
    gcs_state_object: str = "bee-pipeline/state.json"
    gcs_failed_prefix: str = "bee-pipeline/failed"

    # Vertex AI (Anthropic model) — only used when anthropic_api_key is not set
    vertex_model: str = "claude-sonnet-4-6@20250514"

    # Pipeline tuning
    max_conversations_per_run: int = 50
    extraction_max_tokens: int = 8192

    # --- Dry-run / local dev options ---

    # When True: extraction runs normally but Brain writes are logged, not executed.
    dry_run: bool = False

    # When True: ALL extractions go to pending-review regardless of confidence.
    # Use this to audit the pipeline output before allowing direct Brain writes.
    pending_review_only: bool = False

    # Path to a local JSON file for cursor state. When set, GCS is not used.
    # Defaults to "./bee-pipeline-state.json" in dry-run mode when GCS_BUCKET is unset.
    local_state_path: Optional[str] = None

    # Direct Anthropic API key. When set, uses the Anthropic API directly instead of
    # Vertex AI. Injected as a Cloud Run secret env var via --set-secrets.
    anthropic_api_key: Optional[str] = None


def load_config_from_env() -> Config:
    """Load config from environment variables.

    Works for both local development and Cloud Run deployment. In Cloud Run,
    all secrets are injected as env vars via --set-secrets.

    Env vars
    --------
    Required:
      BEE_API_TOKEN       — Bee CLI JWT (Secret Manager: BEE_API_TOKEN)
      ANTHROPIC_API_KEY   — Anthropic API key (Secret Manager: ANTHROPIC_API_KEY)

    Required in production (optional in dry-run):
      GBRAIN_URL              — Brain HTTP MCP server URL (Secret Manager: GBRAIN_URL)
      BEE_GBRAIN_CLIENT_ID    — OAuth client ID (Secret Manager: BEE_GBRAIN_CLIENT_ID)
      BEE_GBRAIN_CLIENT_SECRET — OAuth client secret (Secret Manager: BEE_GBRAIN_CLIENT_SECRET)
      GCS_BUCKET              — GCS bucket for cursor state

    Optional:
      GCP_PROJECT         — GCP project (used for GCS; defaults to empty)
      GCP_REGION          — GCP region (default: us-central1)
      LOCAL_STATE_PATH    — path for cursor state file (overrides GCS)
      DRY_RUN             — 1/true/yes to skip Brain writes and auto-use local state
      PENDING_REVIEW_ONLY — 1/true/yes to route all extractions to pending-review
    """
    dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
    pending_review_only = os.environ.get("PENDING_REVIEW_ONLY", "").lower() in ("1", "true", "yes")
    anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

    gcp_project = os.environ.get("GCP_PROJECT", "")

    if not anthropic_api_key and not gcp_project:
        raise ValueError(
            "Either ANTHROPIC_API_KEY or GCP_PROJECT (for Vertex AI) must be set"
        )

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

    # Brain connection: required in full mode, optional in dry-run
    brain_url = os.environ.get("GBRAIN_URL", "")
    brain_client_id = os.environ.get("BEE_GBRAIN_CLIENT_ID", "")
    brain_client_secret = os.environ.get("BEE_GBRAIN_CLIENT_SECRET", "")

    if not dry_run:
        missing = [k for k, v in {
            "GBRAIN_URL": brain_url,
            "BEE_GBRAIN_CLIENT_ID": brain_client_id,
            "BEE_GBRAIN_CLIENT_SECRET": brain_client_secret,
        }.items() if not v]
        if missing:
            raise ValueError(
                f"Missing required env vars in full mode: {', '.join(missing)}. "
                "Set DRY_RUN=1 to skip Brain writes and make these optional."
            )

    return Config(
        gcp_project=gcp_project,
        gcp_region=os.environ.get("GCP_REGION", "us-central1"),
        bee_token=os.environ["BEE_API_TOKEN"],
        brain_url=brain_url,
        brain_client_id=brain_client_id,
        brain_client_secret=brain_client_secret,
        gcs_bucket=gcs_bucket,
        vertex_model=os.environ.get("VERTEX_MODEL", "claude-sonnet-4-6@20250514"),
        dry_run=dry_run,
        pending_review_only=pending_review_only,
        local_state_path=local_state_path,
        anthropic_api_key=anthropic_api_key,
    )


def load_config() -> Config:
    """Load config — same path for local dev and Cloud Run (all secrets via env vars)."""
    return load_config_from_env()
