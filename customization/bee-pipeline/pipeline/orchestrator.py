"""Pipeline orchestrator: coordinates Bee → extraction → Brain writes."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .bee_client import BeeClient, Conversation, BeeCliError
from .brain_client import BrainClient, DryRunBrainClient, BrainError, create_brain_client
from .config import Config
from .extractor import Extractor, LocalExtractor, Extraction, ExtractionError, create_extractor
from .resolver import Resolver, ResolveResult
from .state import StateManager, LocalStateManager, create_state_manager

logger = logging.getLogger(__name__)

# Slug-safe character replacement
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    normalized = text.lower().strip()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug[:max_len]


def _pending_review_slug(conv_date: str, conv_id: str, fact_index: int) -> str:
    conv_suffix = conv_id[-6:] if len(conv_id) >= 6 else conv_id
    return f"pending-review/{conv_date}-{conv_suffix}-{fact_index}"



def _pending_review_content(
    extraction: Extraction,
    resolve: ResolveResult,
    conv_id: str,
    conv_date: str,
) -> str:
    proposed = resolve.candidate_slug or resolve.slug or "unknown"
    title_fact = extraction.fact[:60].replace("\n", " ")
    return (
        f"---\n"
        f"title: \"Pending Review: {title_fact}\"\n"
        f"type: note\n"
        f"tags: [pending-review]\n"
        f"bee_conversation_id: \"{conv_id}\"\n"
        f"bee_conversation_date: \"{conv_date}\"\n"
        f"---\n"
        f"**Proposed entity** ({extraction.entity_type}): {proposed}  \n"
        f"**Fact**: {extraction.fact}  \n"
        f"**Confidence reason**: {extraction.attribution_reasoning}  \n"
        f"**Snippet**: > {extraction.transcript_snippet}\n"
    )


@dataclass
class RunSummary:
    conversations_fetched: int = 0
    conversations_skipped: int = 0
    conversations_processed: int = 0
    facts_extracted: int = 0
    timeline_entries_written: int = 0
    pending_review_written: int = 0
    errors: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        config: Config,
        *,
        brain: "BrainClient | DryRunBrainClient | None" = None,
        extractor: "Extractor | LocalExtractor | None" = None,
        state: "StateManager | LocalStateManager | None" = None,
    ) -> None:
        self._config = config
        self._brain = brain if brain is not None else create_brain_client(config)
        self._bee = BeeClient(config.bee_token)
        self._extractor = extractor if extractor is not None else create_extractor(config)
        self._resolver = Resolver(self._brain)
        self._state = state if state is not None else create_state_manager(config)

    def run(self) -> RunSummary:
        summary = RunSummary()

        state = self._state.load()
        self._resolver.load()

        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            result = self._bee.get_changed(cursor=state.cursor)
        except BeeCliError as exc:
            logger.error("Failed to fetch from Bee CLI: %s", exc)
            summary.errors.append(f"Bee CLI error: {exc}")
            return summary

        conversations = result.conversations
        next_cursor = result.next_cursor

        summary.conversations_fetched = len(conversations)
        logger.info("Fetched %d usable conversations from Bee", len(conversations))

        if not conversations:
            logger.info("No new conversations — nothing to process")
            # Advance cursor even if no conversations (Bee may return a new cursor)
            if next_cursor and next_cursor != state.cursor:
                self._state.save(next_cursor, fetched_at)
            return summary

        for conv in conversations:
            self._process_conversation(conv, summary)
            summary.conversations_processed += 1

        # Save cursor only after all conversations processed successfully
        self._state.save(next_cursor, fetched_at)

        logger.info(
            "Run complete: %d processed, %d timeline entries, %d pending-review",
            summary.conversations_processed,
            summary.timeline_entries_written,
            summary.pending_review_written,
        )
        return summary

    def _process_conversation(self, conv: Conversation, summary: RunSummary) -> None:
        conv_date = conv.date_str
        logger.info("Processing conversation %s (%s)", conv.id_str, conv_date)

        transcript = conv.transcript
        if not transcript.strip():
            logger.info("Conversation %s has empty transcript after assembly — skipping", conv.id_str)
            summary.conversations_skipped += 1
            return

        try:
            extractions = self._extractor.extract(transcript, conv_date)
        except ExtractionError as exc:
            logger.error("Extraction failed for conversation %s: %s", conv.id_str, exc)
            summary.errors.append(f"Extraction error for {conv.id_str}: {exc}")
            self._state.log_failed({
                "conversation_id": conv.id_str,
                "date": conv_date,
                "error": str(exc),
                "phase": "extraction",
            })
            return

        summary.facts_extracted += len(extractions)

        for idx, ext in enumerate(extractions):
            try:
                self._route_extraction(ext, conv, conv_date, idx, summary)
            except Exception as exc:
                logger.error(
                    "Unexpected error routing extraction %d from %s: %s",
                    idx, conv.id_str, exc,
                )
                summary.errors.append(f"Routing error for {conv.id_str}[{idx}]: {exc}")

    def _route_extraction(
        self,
        ext: Extraction,
        conv: Conversation,
        conv_date: str,
        idx: int,
        summary: RunSummary,
    ) -> None:
        resolve = self._resolver.resolve(ext.subject_name, ext.entity_type)

        high_confidence = (
            ext.attribution_confidence == "high"
            and not ext.name_ambiguous
            and resolve.confidence == "high"
            and resolve.slug is not None
        )

        if high_confidence:
            self._write_timeline_entry(ext, resolve, resolve.slug, conv, conv_date, idx, summary)
        else:
            logger.info(
                "Routing to pending-review: %s (confidence=%s, ambiguous=%s, resolve=%s)",
                ext.subject_name, ext.attribution_confidence,
                ext.name_ambiguous, resolve.confidence,
            )
            self._write_pending_review(ext, resolve, conv, conv_date, idx, summary)

    def _write_timeline_entry(
        self,
        ext: Extraction,
        resolve: ResolveResult,
        slug: str,
        conv: Conversation,
        conv_date: str,
        idx: int,
        summary: RunSummary,
    ) -> None:
        # Verify the page still exists — it should, since the resolver loaded
        # aliases from Brain at startup, but guard against race conditions.
        if self._brain.get_page(slug) is None:
            logger.warning(
                "Resolved slug %s has no Brain page — routing to pending-review", slug
            )
            self._write_pending_review(ext, resolve, conv, conv_date, idx, summary)
            return

        source = f"bee:{conv.id_str}"
        detail = f"Source snippet: {ext.transcript_snippet}" if ext.transcript_snippet else None

        try:
            self._brain.add_timeline_entry(
                slug=slug,
                date=conv_date,
                summary=ext.fact,
                detail=detail,
                source=source,
            )
            logger.info("Timeline entry written to %s: %s", slug, ext.fact[:60])
            summary.timeline_entries_written += 1
        except BrainError as exc:
            logger.error("Failed to write timeline entry to %s: %s", slug, exc)
            raise

    def _write_pending_review(
        self,
        ext: Extraction,
        resolve: ResolveResult,
        conv: Conversation,
        conv_date: str,
        idx: int,
        summary: RunSummary,
    ) -> None:
        slug = _pending_review_slug(conv_date, conv.id_str, idx)
        content = _pending_review_content(ext, resolve, conv.id_str, conv_date)

        try:
            self._brain.put_page(slug, content)
            self._brain.add_tag(slug, "pending-review")
            logger.info("Pending-review page written: %s", slug)
            summary.pending_review_written += 1
        except BrainError as exc:
            logger.error("Failed to write pending-review page %s: %s", slug, exc)
            raise
