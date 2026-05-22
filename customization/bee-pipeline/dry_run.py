#!/usr/bin/env python3
"""Local dry-run entrypoint for the Bee → Brain pipeline.

Runs the full pipeline locally — Bee fetch → fact extraction → routing — but
logs Brain writes instead of executing them. No GCS required. Cursor state is
saved to a local JSON file so you can re-run incrementally.

Minimum required env vars
--------------------------
  BEE_API_TOKEN      — JWT from Bee CLI (see README "Getting the Bee Token")
  ANTHROPIC_API_KEY  — direct API key (skips Vertex AI / GCP entirely)

Optional env vars
-----------------
  GBRAIN_URL              — if set, the resolver loads real aliases from Brain so
                            entities resolve correctly instead of all going to
                            pending-review. Requires BEE_GBRAIN_CLIENT_ID + BEE_GBRAIN_CLIENT_SECRET.
  BEE_GBRAIN_CLIENT_ID    — OAuth client ID (from `gbrain auth register-client`)
  BEE_GBRAIN_CLIENT_SECRET
  LOCAL_STATE_PATH        — path for cursor state file (default: ./bee-pipeline-state.json)
  VERTEX_MODEL            — override the Vertex AI model (ignored when ANTHROPIC_API_KEY is set)

Usage
-----
  # 1. Install deps
  pip install -r requirements.txt

  # 2. Copy and fill in the env file
  cp .env.example .env
  # At minimum set BEE_API_TOKEN and ANTHROPIC_API_KEY

  # 3. Run
  DRY_RUN=1 source .env && python dry_run.py

  # Or with dotenv-style loading (if you have python-dotenv installed):
  python -c "from dotenv import load_dotenv; load_dotenv()" && python dry_run.py

  # Run again to fetch only new conversations since last run (cursor is saved locally)
  python dry_run.py
"""

import json
import logging
import os
import sys

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    # Force dry-run regardless of env var (this script is always a dry run)
    os.environ["DRY_RUN"] = "1"

    try:
        from pipeline.config import load_config_from_env
    except ImportError as exc:
        logger.error("Import failed — run from customization/bee-pipeline/: %s", exc)
        return 1

    try:
        config = load_config_from_env()
    except (KeyError, ValueError) as exc:
        logger.error("Config error: %s", exc)
        _print_help()
        return 1

    logger.info("=" * 60)
    logger.info("Bee → Brain pipeline  DRY RUN")
    logger.info("Extraction: %s", "direct Anthropic API" if config.anthropic_api_key else f"Vertex AI ({config.vertex_model})")
    logger.info("State file: %s", config.local_state_path)
    logger.info("Brain URL:  %s", config.brain_url or "(none — all facts → pending-review logged)")
    logger.info("Mode:       %s", "AUDIT (pending-review only)" if config.pending_review_only else "normal")
    logger.info("=" * 60)

    from pipeline.orchestrator import Orchestrator

    try:
        orchestrator = Orchestrator(config)
        summary = orchestrator.run()
    except Exception:
        logger.exception("Pipeline run failed")
        return 1

    result = {
        "dry_run": True,
        "pending_review_only": config.pending_review_only,
        "conversations_fetched": summary.conversations_fetched,
        "conversations_processed": summary.conversations_processed,
        "conversations_skipped": summary.conversations_skipped,
        "facts_extracted": summary.facts_extracted,
        "timeline_entries_written": summary.timeline_entries_written,
        "pending_review_written": summary.pending_review_written,
        "bee_facts_written": summary.bee_facts_written,
        "errors": summary.errors,
    }

    print("\n" + "=" * 60)
    print("DRY RUN SUMMARY")
    print("=" * 60)
    print(json.dumps(result, indent=2))

    if summary.errors:
        logger.warning("%d error(s) occurred — see log above", len(summary.errors))
        return 1
    return 0


def _print_help() -> None:
    print(
        "\nRequired env vars:\n"
        "  BEE_API_TOKEN     — JWT from Bee CLI\n"
        "  ANTHROPIC_API_KEY — direct Anthropic API key  (or GCP_PROJECT for Vertex AI)\n"
        "\nOptional:\n"
        "  GBRAIN_URL / BEE_GBRAIN_CLIENT_ID / BEE_GBRAIN_CLIENT_SECRET — for real alias resolution\n"
        "  LOCAL_STATE_PATH  — cursor state file (default: ./bee-pipeline-state.json)\n"
        "\nSee README.md for full setup instructions.\n"
    )


if __name__ == "__main__":
    sys.exit(main())
