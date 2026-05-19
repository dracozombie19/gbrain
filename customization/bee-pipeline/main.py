"""Cloud Function entry point for the Bee → Brain pipeline.

Triggered by Cloud Scheduler every 4 hours via HTTP.
"""

import json
import logging
import os
import functions_framework
from flask import Request

from pipeline.config import load_config
from pipeline.orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@functions_framework.http
def run_pipeline(request: Request):
    """HTTP Cloud Function entry point."""
    logger.info("Bee → Brain pipeline triggered")

    try:
        config = load_config()
    except Exception as exc:
        logger.error("Config load failed: %s", exc)
        return (json.dumps({"status": "error", "message": f"Config error: {exc}"}), 500)

    try:
        orchestrator = Orchestrator(config)
        summary = orchestrator.run()
    except Exception as exc:
        logger.exception("Pipeline run failed with unhandled exception")
        return (
            json.dumps({"status": "error", "message": str(exc)}),
            500,
            {"Content-Type": "application/json"},
        )

    result = {
        "status": "ok",
        "conversations_fetched": summary.conversations_fetched,
        "conversations_processed": summary.conversations_processed,
        "conversations_skipped": summary.conversations_skipped,
        "facts_extracted": summary.facts_extracted,
        "timeline_entries_written": summary.timeline_entries_written,
        "pending_review_written": summary.pending_review_written,
        "errors": summary.errors,
    }

    status_code = 200 if not summary.errors else 207  # 207 = partial success
    return (json.dumps(result), status_code, {"Content-Type": "application/json"})
