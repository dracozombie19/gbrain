#!/usr/bin/env python3
"""
One-time (rerunnable) full historical sync from Bee.

Exports ALL Bee conversations via `bee sync`, then processes each through
the same extraction pipeline as the ongoing 4h Cloud Run job.

Usage:
    python full_sync.py                        # full sync, all conversations
    python full_sync.py --dry-run              # extract only, no Brain writes
    python full_sync.py --pending-review-only  # all to pending-review (safe for first run)
    python full_sync.py --limit 50             # process only the first 50
    python full_sync.py --since 2025-01-01     # only conversations from this date
    python full_sync.py --sync-dir /path/dir   # use existing bee sync output (skip bee sync)
    python full_sync.py --no-resume            # reprocess everything (ignore progress file)

Progress is saved after every conversation to ./bee-full-sync-progress.json.
Interrupt and re-run at any time — already-processed conversations are skipped.

Recommended first run:
    source .env && python full_sync.py --pending-review-only --dry-run

Then when satisfied with the output:
    source .env && python full_sync.py --pending-review-only
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pipeline.agent import ConversationAgent
from pipeline.bee_client import Conversation, Utterance
from pipeline.brain_client import create_brain_client
from pipeline.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("full_sync")

_BEE_SYNC_TIMEOUT = 1800  # 30 minutes; full export can be slow


# ---------------------------------------------------------------------------
# Bee CLI helpers
# ---------------------------------------------------------------------------

def _bee(*args: str) -> list[str]:
    if sys.platform == "win32":
        return ["cmd", "/c", "bee"] + list(args)
    return ["bee"] + list(args)


def run_bee_sync(output_dir: Path, token: str) -> None:
    """Authenticate and run `bee sync --output <output_dir>`."""
    logger.info("Authenticating Bee CLI...")
    result = subprocess.run(
        _bee("login", "--token-stdin"),
        input=token,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"bee login failed: {result.stderr.strip()}")

    logger.info("Running bee sync --output %s ...", output_dir)
    result = subprocess.run(
        _bee("sync", "--output", str(output_dir), "--only", "conversations"),
        capture_output=True,
        text=True,
        timeout=_BEE_SYNC_TIMEOUT,
    )
    if result.returncode != 0:
        # Try without --only in case this version doesn't support it
        logger.warning("bee sync --only failed (%s), retrying without --only", result.stderr.strip()[:100])
        result = subprocess.run(
            _bee("sync", "--output", str(output_dir)),
            capture_output=True,
            text=True,
            timeout=_BEE_SYNC_TIMEOUT,
        )
    if result.returncode != 0:
        raise RuntimeError(f"bee sync failed: {result.stderr.strip()}")

    logger.info("bee sync complete")
    if result.stdout.strip():
        logger.info("bee sync output: %s", result.stdout.strip()[:300])


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

def _strip_frontmatter(text: str) -> tuple[dict, str]:
    """Strip YAML frontmatter if present. Returns (meta_dict, body)."""
    if not text.lstrip().startswith("---"):
        return {}, text
    # Find closing ---
    start = text.find("---")
    end = text.find("\n---", start + 3)
    if end == -1:
        return {}, text
    fm_text = text[start + 3:end].strip()
    body = text[end + 4:].strip()
    meta: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, body


def _parse_conversation_md(md_path: Path, date_str: str) -> Conversation | None:
    """Parse a bee sync conversation markdown file into a Conversation object.

    bee sync writes one file per conversation. The filename contains the
    conversation ID. The body is the transcript text (possibly with frontmatter).
    """
    try:
        raw = md_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not read %s: %s", md_path, exc)
        return None

    meta, body = _strip_frontmatter(raw)

    # Extract numeric conversation ID from filename (e.g. "12345678.md", "conv-12345678.md")
    numeric_match = re.search(r"(\d{5,})", md_path.stem)
    if not numeric_match:
        logger.warning("No numeric ID in filename %s — skipping", md_path.name)
        return None
    conv_id = int(numeric_match.group(1))

    # Date: prefer frontmatter, fall back to directory name
    effective_date = meta.get("date") or meta.get("created_at", "")[:10] or date_str
    # Normalise to YYYY-MM-DD
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", effective_date)
    effective_date = date_match.group(1) if date_match else date_str

    try:
        dt = datetime.strptime(effective_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        start_time_ms = int(dt.timestamp() * 1000)
    except ValueError:
        start_time_ms = 0

    # Build utterances from body lines (skip markdown headings and blank lines)
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue  # skip headings injected by bee sync
        lines.append(stripped)

    if not lines:
        logger.debug("No transcript content in %s", md_path.name)
        return None

    utterances = [
        Utterance(text=line, speaker="Unknown", start_ms=i * 1000)
        for i, line in enumerate(lines)
    ]

    summary = meta.get("summary", "")
    short_summary = meta.get("short_summary", "")

    return Conversation(
        id=conv_id,
        start_time_ms=start_time_ms,
        created_at_ms=start_time_ms,
        state="COMPLETED",
        utterances=utterances,
        summary=summary,
        short_summary=short_summary,
    )


def discover_conversations(sync_dir: Path, since: str | None = None) -> list[tuple[str, Path]]:
    """Walk sync output and return (date_str, md_path) pairs sorted oldest-first.

    `bee sync` actual layout:
        <sync_dir>/conversations/YYYY-MM-DD/*.md

    Legacy fallback (in case the layout ever changes):
        <sync_dir>/daily/YYYY-MM-DD/conversations/*.md
    """
    results: list[tuple[str, Path]] = []

    # Primary: conversations/YYYY-MM-DD/*.md
    conv_root = sync_dir / "conversations"
    if conv_root.is_dir():
        for date_dir in sorted(conv_root.iterdir()):
            if not date_dir.is_dir():
                continue
            date_str = date_dir.name
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
                continue
            if since and date_str < since:
                continue
            for md_file in sorted(date_dir.glob("*.md")):
                results.append((date_str, md_file))

    # Fallback: daily/YYYY-MM-DD/conversations/*.md
    if not results:
        daily_dir = sync_dir / "daily"
        if daily_dir.is_dir():
            for date_dir in sorted(daily_dir.iterdir()):
                if not date_dir.is_dir():
                    continue
                date_str = date_dir.name
                if since and date_str < since:
                    continue
                nested_conv_dir = date_dir / "conversations"
                if not nested_conv_dir.is_dir():
                    continue
                for md_file in sorted(nested_conv_dir.glob("*.md")):
                    results.append((date_str, md_file))

    return results


# ---------------------------------------------------------------------------
# Progress tracking
# ---------------------------------------------------------------------------

@dataclass
class FullSyncProgress:
    # Tracks by relative path string so renaming sync-dir doesn't confuse us
    processed_paths: set[str] = field(default_factory=set)
    last_run_at: str = ""
    total_processed: int = 0
    total_skipped: int = 0
    total_errors: int = 0

    def to_dict(self) -> dict:
        return {
            "processed_paths": sorted(self.processed_paths),
            "last_run_at": self.last_run_at,
            "total_processed": self.total_processed,
            "total_skipped": self.total_skipped,
            "total_errors": self.total_errors,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FullSyncProgress":
        return cls(
            processed_paths=set(d.get("processed_paths", [])),
            last_run_at=d.get("last_run_at", ""),
            total_processed=d.get("total_processed", 0),
            total_skipped=d.get("total_skipped", 0),
            total_errors=d.get("total_errors", 0),
        )


def load_progress(path: Path) -> FullSyncProgress:
    try:
        return FullSyncProgress.from_dict(json.loads(path.read_text()))
    except FileNotFoundError:
        return FullSyncProgress()
    except Exception as exc:
        logger.warning("Could not read progress file (%s) — starting fresh", exc)
        return FullSyncProgress()


def save_progress(path: Path, progress: FullSyncProgress) -> None:
    path.write_text(json.dumps(progress.to_dict(), indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sync-dir",
        help="Use an existing bee sync output directory instead of running bee sync.",
    )
    parser.add_argument(
        "--progress-file",
        default="./bee-full-sync-progress.json",
        help="Path to the progress file (default: ./bee-full-sync-progress.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run extraction but log Brain writes instead of executing them.",
    )
    parser.add_argument(
        "--pending-review-only",
        action="store_true",
        help="Route all extractions to pending-review (recommended for first run).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after processing this many conversations (0 = no limit).",
    )
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Only process conversations on or after this date.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore the progress file and reprocess everything.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=15.0,
        help="Seconds to wait between conversations (default: 15).",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except Exception as exc:
        logger.error("Config load failed: %s", exc)
        return 1

    if args.dry_run:
        config.dry_run = True
    if args.pending_review_only:
        config.pending_review_only = True

    progress_path = Path(args.progress_file)
    progress = FullSyncProgress() if args.no_resume else load_progress(progress_path)

    if progress.total_processed:
        logger.info(
            "Resuming: %d already processed, %d errors so far",
            progress.total_processed, progress.total_errors,
        )

    # Step 1: get the sync output directory
    if args.sync_dir:
        sync_dir = Path(args.sync_dir)
        if not sync_dir.is_dir():
            logger.error("--sync-dir %s does not exist", sync_dir)
            return 1
        logger.info("Using existing sync dir: %s", sync_dir)
    else:
        import tempfile
        sync_dir = Path(tempfile.mkdtemp(prefix="bee-full-sync-"))
        logger.info("Created temp sync dir: %s", sync_dir)
        try:
            run_bee_sync(sync_dir, config.bee_token)
        except RuntimeError as exc:
            logger.error("bee sync failed: %s", exc)
            return 1

    # Step 2: discover conversations
    conv_files = discover_conversations(sync_dir, since=args.since)
    logger.info(
        "Found %d conversation files%s",
        len(conv_files),
        f" (since {args.since})" if args.since else "",
    )
    if not conv_files:
        logger.warning(
            "No conversations found under %s.\n"
            "Expected layout: <sync_dir>/conversations/YYYY-MM-DD/*.md\n"
            "Run with --sync-dir to point at an existing bee sync output and inspect it.",
            sync_dir,
        )
        return 0

    # Step 3: build Brain client + agent
    try:
        brain = create_brain_client(config)
        agent = ConversationAgent(config, brain)
    except Exception as exc:
        logger.error("Failed to initialise Brain client: %s", exc)
        return 1

    # Step 4: process conversations
    processed_this_run = 0
    skipped_this_run = 0
    errors_this_run = 0

    for date_str, md_path in conv_files:
        if args.limit and processed_this_run >= args.limit:
            logger.info("Reached --limit %d — stopping", args.limit)
            break

        # Use path relative to sync_dir as the progress key
        try:
            rel_path = str(md_path.relative_to(sync_dir))
        except ValueError:
            rel_path = str(md_path)

        if not args.no_resume and rel_path in progress.processed_paths:
            logger.debug("Skip (already processed): %s", rel_path)
            skipped_this_run += 1
            continue

        conv = _parse_conversation_md(md_path, date_str)
        if conv is None or not conv.transcript.strip():
            logger.debug("Skip (empty/unparseable): %s", md_path.name)
            skipped_this_run += 1
            progress.total_skipped += 1
            save_progress(progress_path, progress)
            continue

        if processed_this_run > 0:
            time.sleep(args.delay)

        logger.info(
            "[%d] Processing conversation %s (%s, %d lines)",
            processed_this_run + 1,
            conv.id_str,
            conv.date_str,
            len(conv.utterances),
        )

        try:
            result = agent.process(conv)
            logger.info(
                "  → timeline=%d memories=%d tasks=%d pending-review=%d "
                "new-entities=%d tool-calls=%d",
                result.timeline_entries, result.memories, result.action_items,
                result.pending_review, result.new_entities, result.tool_calls,
            )
            if result.errors:
                for err in result.errors:
                    logger.warning("  tool error: %s", err)

            progress.processed_paths.add(rel_path)
            processed_this_run += 1
            progress.total_processed += 1
        except Exception:
            logger.exception("Agent error for conversation %s", conv.id_str)
            errors_this_run += 1
            progress.total_errors += 1

        progress.last_run_at = datetime.now(timezone.utc).isoformat()
        save_progress(progress_path, progress)

    logger.info(
        "Full sync run complete: processed=%d skipped=%d errors=%d "
        "(lifetime: processed=%d errors=%d)",
        processed_this_run, skipped_this_run, errors_this_run,
        progress.total_processed, progress.total_errors,
    )
    logger.info("Progress saved to %s", progress_path)

    if not args.sync_dir:
        logger.info("Sync output left at %s (delete when done)", sync_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
