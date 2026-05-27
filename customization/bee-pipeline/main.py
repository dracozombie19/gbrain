"""Entry point for the Bee → Brain pipeline Cloud Run Job.

Triggered by Cloud Scheduler every 4 hours via the Cloud Run Jobs API.
Exits 0 on success (including partial errors), 1 on hard failure.
"""

import json
import logging
import sys

from pipeline.config import load_config
from pipeline.orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> int:
    logger.info("Bee → Brain pipeline triggered")

    try:
        config = load_config()
    except Exception as exc:
        logger.error("Config load failed: %s", exc)
        return 1

    try:
        orchestrator = Orchestrator(config)
        summary = orchestrator.run()
    except Exception as exc:
        logger.exception("Pipeline run failed with unhandled exception")
        return 1

    logger.info(
        "Run complete: fetched=%d processed=%d skipped=%d facts=%d "
        "timeline=%d memories=%d tasks=%d pending_review=%d bee_facts=%d errors=%d",
        summary.conversations_fetched,
        summary.conversations_processed,
        summary.conversations_skipped,
        summary.facts_extracted,
        summary.timeline_entries_written,
        summary.memories_written,
        summary.action_items_written,
        summary.pending_review_written,
        summary.bee_facts_written,
        len(summary.errors),
    )

    if summary.errors:
        logger.warning("Completed with %d error(s):", len(summary.errors))
        for err in summary.errors:
            logger.warning("  %s", err)

    # Exit 0 even with partial errors — the cursor was saved and the job
    # completed its work. Errors are logged above for visibility.
    return 0


if __name__ == "__main__":
    sys.exit(main())
