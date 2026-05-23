"""Pipeline orchestrator: coordinates Bee → extraction → Brain writes."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .bee_client import BeeClient, BeeFact, Conversation, BeeCliError
from .brain_client import BrainClient, DryRunBrainClient, BrainError, create_brain_client
from .config import Config
from .extractor import Extractor, LocalExtractor, Extraction, ExtractionError, create_extractor
from .resolver import Resolver, ResolveResult
from .state import StateManager, LocalStateManager, create_state_manager

logger = logging.getLogger(__name__)

# Slug-safe character replacement
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Non-word characters for fact normalization
_NONWORD_RE = re.compile(r"[^\w\s]")

# Stop words to ignore when checking if a fact is already in a page
_STOP_WORDS = {
    "this", "that", "with", "have", "from", "they", "been", "were",
    "will", "about", "when", "also", "some", "than", "more", "very",
    "just", "into", "your", "which", "there", "their",
}


def _fact_key(subject: str, fact: str) -> str:
    """Compute a deduplication fingerprint for a (subject, fact) pair.

    Used to skip identical facts that appear in multiple conversations within
    the same pipeline run.
    """
    subj = " ".join(_NONWORD_RE.sub(" ", subject.lower().strip()).split())
    fact_short = " ".join(_NONWORD_RE.sub(" ", fact[:100].lower().strip()).split())
    return f"{subj}||{fact_short}"


def _fact_in_page(fact: str, page: dict) -> bool:
    """Check whether the key words from fact are already present in a Brain page.

    Returns True when ≥70% of the fact's meaningful words appear in the page
    body or frontmatter — a heuristic that catches "Ashley is Zac's wife" when
    the page already says "married to Zac Dowd" or has spouse in frontmatter.
    """
    body = page.get("body", "") or ""
    frontmatter = page.get("frontmatter", {}) or {}
    fm_text = " ".join(str(v) for v in frontmatter.values() if v)
    content = _NONWORD_RE.sub(" ", (body + " " + fm_text).lower())

    fact_clean = _NONWORD_RE.sub(" ", fact.lower())
    words = [w for w in fact_clean.split() if len(w) > 3 and w not in _STOP_WORDS]
    if not words:
        return False

    matches = sum(1 for w in words if w in content)
    return matches / len(words) >= 0.7


def _slugify(text: str, max_len: int = 40) -> str:
    normalized = text.lower().strip()
    slug = _SLUG_RE.sub("-", normalized).strip("-")
    return slug[:max_len]


def _pending_review_slug(conv_date: str, conv_id: str, fact_index: int) -> str:
    conv_suffix = conv_id[-6:] if len(conv_id) >= 6 else conv_id
    return f"pending-review/{conv_date}-{conv_suffix}-{fact_index}"



def _format_snippet_blockquote(snippet: str) -> str:
    """Render a multi-line transcript snippet as a markdown blockquote."""
    if not snippet:
        return ""
    lines = snippet.splitlines()
    return "\n".join(f"> {line}" if line.strip() else ">" for line in lines)


def _expand_snippet(transcript: str, anchor: str, context_lines: int = 4) -> str:
    """Return a broader window from the transcript centered on the anchor text.

    Finds the anchor (the short snippet from Claude) in the transcript, then
    returns that line plus `context_lines` lines above and below it — giving
    roughly 5-10 lines of context without inflating extractor output tokens.
    Falls back to the anchor itself if not found.
    """
    if not anchor or not transcript:
        return anchor

    # Normalize whitespace for matching
    anchor_clean = " ".join(anchor.split()).lower()
    transcript_lines = transcript.splitlines()

    best_line = -1
    best_score = 0
    anchor_words = set(anchor_clean.split())

    for i, line in enumerate(transcript_lines):
        line_clean = " ".join(line.split()).lower()
        # Exact substring match wins immediately
        if anchor_clean in line_clean or line_clean in anchor_clean:
            best_line = i
            break
        # Otherwise score by word overlap (for multi-line anchors)
        if anchor_words:
            overlap = len(anchor_words & set(line_clean.split())) / len(anchor_words)
            if overlap > best_score:
                best_score = overlap
                best_line = i

    if best_line == -1 or best_score < 0.3:
        return anchor

    start = max(0, best_line - context_lines)
    end = min(len(transcript_lines), best_line + context_lines + 1)
    return "\n".join(transcript_lines[start:end])


def _pending_review_content(
    extraction: Extraction,
    resolve: ResolveResult,
    conv_id: str,
    conv_date: str,
    pending_review_only: bool = False,
    transcript: str = "",
) -> str:
    proposed = resolve.candidate_slug or resolve.slug or "unknown"
    title_fact = extraction.fact[:60].replace("\n", " ")
    # Expand snippet from the raw transcript when available; fall back to the
    # 1-3 sentence version Claude returned.
    display_snippet = (
        _expand_snippet(transcript, extraction.transcript_snippet)
        if transcript
        else extraction.transcript_snippet
    )
    snippet_block = _format_snippet_blockquote(display_snippet)
    audit_note = "\n**Note**: audit mode — routed to pending-review regardless of confidence.\n" if pending_review_only and resolve.confidence == "high" else ""
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
        f"{audit_note}"
        f"\n**Transcript snippet**:\n\n{snippet_block}\n"
    )


def _bee_fact_pending_review_slug(fact: "BeeFact") -> str:
    return f"pending-review/bee-fact-{fact.id}"


def _bee_fact_pending_review_content(fact: "BeeFact") -> str:
    title = fact.text[:60].replace("\n", " ")
    tags_str = ", ".join(fact.tags) if fact.tags else "general"
    confirmed_str = "yes" if fact.confirmed else "no"
    return (
        f"---\n"
        f"title: \"Bee Fact: {title}\"\n"
        f"type: note\n"
        f"tags: [pending-review, bee-fact]\n"
        f"bee_fact_id: \"{fact.id}\"\n"
        f"bee_fact_date: \"{fact.date_str}\"\n"
        f"bee_fact_confirmed: {confirmed_str}\n"
        f"---\n"
        f"**Source**: Bee AI extraction — speaker labels are frequently inaccurate; verify before trusting.\n\n"
        f"**Fact**: {fact.text}\n\n"
        f"**Bee tags**: {tags_str}\n"
    )


@dataclass
class RunSummary:
    conversations_fetched: int = 0
    conversations_skipped: int = 0
    conversations_processed: int = 0
    facts_extracted: int = 0
    timeline_entries_written: int = 0
    pending_review_written: int = 0
    bee_facts_written: int = 0
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
        # In-run deduplication: tracks (subject, fact) fingerprints seen this run
        self._seen_fact_keys: set[str] = set()
        # Cross-run deduplication: Bee fact IDs written in any prior run
        self._seen_bee_fact_ids: set[int] = set()

    def run(self) -> RunSummary:
        summary = RunSummary()
        self._seen_fact_keys = set()  # reset per run

        state = self._state.load()
        self._seen_bee_fact_ids = set(state.seen_bee_fact_ids)
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
        bee_facts = result.bee_facts

        summary.conversations_fetched = len(conversations)
        logger.info(
            "Fetched %d usable conversations, %d Bee facts",
            len(conversations), len(bee_facts),
        )

        if not conversations and not bee_facts:
            logger.info("No new data — nothing to process")
            if next_cursor and next_cursor != state.cursor:
                self._state.save(next_cursor, fetched_at, list(self._seen_bee_fact_ids))
            return summary

        for conv in conversations:
            self._process_conversation(conv, summary)
            summary.conversations_processed += 1

        for fact in bee_facts:
            self._write_bee_fact(fact, summary)

        # Save cursor and seen Bee fact IDs after all processing
        self._state.save(next_cursor, fetched_at, list(self._seen_bee_fact_ids))

        logger.info(
            "Run complete: %d processed, %d timeline entries, %d pending-review, %d bee facts",
            summary.conversations_processed,
            summary.timeline_entries_written,
            summary.pending_review_written,
            summary.bee_facts_written,
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
            extractions = self._extractor.extract(transcript, conv_date, summary=conv.summary)
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
                self._route_extraction(ext, conv, conv_date, idx, summary, transcript)
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
        transcript: str = "",
    ) -> None:
        # In-run dedup: skip if this (subject, fact) pair was already processed
        # in an earlier conversation of this batch.
        key = _fact_key(ext.subject_name, ext.fact)
        if key in self._seen_fact_keys:
            logger.info(
                "Skipping duplicate fact (already processed in this run): %s — %s",
                ext.subject_name, ext.fact[:60],
            )
            return
        self._seen_fact_keys.add(key)

        resolve = self._resolver.resolve(ext.subject_name, ext.entity_type)

        high_confidence = (
            ext.attribution_confidence == "high"
            and not ext.name_ambiguous
            and resolve.confidence == "high"
            and resolve.slug is not None
        )

        if high_confidence and not self._config.pending_review_only:
            self._write_timeline_entry(ext, resolve, resolve.slug, conv, conv_date, idx, summary)
        else:
            if self._config.pending_review_only and high_confidence:
                logger.info(
                    "Audit mode: routing high-confidence fact to pending-review: %s",
                    ext.subject_name,
                )
            else:
                logger.info(
                    "Routing to pending-review: %s (confidence=%s, ambiguous=%s, resolve=%s)",
                    ext.subject_name, ext.attribution_confidence,
                    ext.name_ambiguous, resolve.confidence,
                )
            self._write_pending_review(ext, resolve, conv, conv_date, idx, summary, transcript)

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
        page = self._brain.get_page(slug)
        if page is None:
            logger.warning(
                "Resolved slug %s has no Brain page — routing to pending-review", slug
            )
            self._write_pending_review(ext, resolve, conv, conv_date, idx, summary)
            return

        # Cross-run dedup: skip if the fact is already captured on this entity's page.
        if _fact_in_page(ext.fact, page):
            logger.info(
                "Fact already present on %s — skipping timeline entry: %s",
                slug, ext.fact[:60],
            )
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
        transcript: str = "",
    ) -> None:
        slug = _pending_review_slug(conv_date, conv.id_str, idx)
        content = _pending_review_content(
            ext, resolve, conv.id_str, conv_date,
            self._config.pending_review_only, transcript,
        )

        try:
            self._brain.put_page(slug, content)
            self._brain.add_tag(slug, "pending-review")
            logger.info("Pending-review page written: %s", slug)
            summary.pending_review_written += 1
        except BrainError as exc:
            logger.error("Failed to write pending-review page %s: %s", slug, exc)
            raise

    def _write_bee_fact(self, fact: BeeFact, summary: RunSummary) -> None:
        if fact.id in self._seen_bee_fact_ids:
            logger.info("Skipping already-processed Bee fact %s: %s", fact.id, fact.text[:60])
            return

        slug = _bee_fact_pending_review_slug(fact)
        content = _bee_fact_pending_review_content(fact)
        try:
            self._brain.put_page(slug, content)
            self._brain.add_tag(slug, "pending-review")
            self._brain.add_tag(slug, "bee-fact")
            logger.info("Bee fact pending-review written: %s (id=%s)", slug, fact.id)
            summary.bee_facts_written += 1
            self._seen_bee_fact_ids.add(fact.id)
        except BrainError as exc:
            logger.error("Failed to write Bee fact page %s: %s", slug, exc)
            summary.errors.append(f"Bee fact write error for {fact.id}: {exc}")
