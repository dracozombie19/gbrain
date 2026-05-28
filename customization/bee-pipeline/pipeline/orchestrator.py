"""Pipeline orchestrator: fetches from Bee, runs an agent per conversation, saves state."""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .agent import ConversationAgent, AgentResult
from .bee_client import BeeClient, BeeFact, Conversation, BeeCliError
from .brain_client import BrainClient, DryRunBrainClient, BrainError, create_brain_client
from .config import Config
from .state import StateManager, LocalStateManager, create_state_manager

logger = logging.getLogger(__name__)


def _bee_fact_slug(fact: BeeFact) -> str:
    return f"pending-review/bee-fact-{fact.id}"


def _bee_fact_content(fact: BeeFact) -> str:
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
    timeline_entries_written: int = 0
    memories_written: int = 0
    action_items_written: int = 0
    pending_review_written: int = 0
    new_entities_written: int = 0
    bee_facts_written: int = 0
    errors: list[str] = field(default_factory=list)


class Orchestrator:
    def __init__(
        self,
        config: Config,
        *,
        brain: "BrainClient | DryRunBrainClient | None" = None,
        state: "StateManager | LocalStateManager | None" = None,
    ) -> None:
        self._config = config
        self._brain = brain if brain is not None else create_brain_client(config)
        self._bee = BeeClient(config.bee_token)
        self._agent = ConversationAgent(config, self._brain)
        self._state = state if state is not None else create_state_manager(config)
        self._seen_bee_fact_ids: set[int] = set()

    def run(self) -> RunSummary:
        summary = RunSummary()

        state = self._state.load()
        self._seen_bee_fact_ids = set(state.seen_bee_fact_ids)

        fetched_at = datetime.now(timezone.utc).isoformat()
        try:
            result = self._bee.get_changed(cursor=state.cursor)
        except BeeCliError as exc:
            logger.error("Failed to fetch from Bee: %s", exc)
            summary.errors.append(f"Bee fetch error: {exc}")
            return summary

        conversations = result.conversations
        next_cursor = result.next_cursor
        bee_facts = result.bee_facts

        summary.conversations_fetched = len(conversations)
        logger.info("Fetched %d conversations, %d Bee facts", len(conversations), len(bee_facts))

        if not conversations and not bee_facts:
            logger.info("No new data — nothing to process")
            if next_cursor and next_cursor != state.cursor:
                self._state.save(next_cursor, fetched_at, list(self._seen_bee_fact_ids))
            return summary

        for i, conv in enumerate(conversations):
            if i > 0:
                time.sleep(15)
            self._process_conversation(conv, summary)
            summary.conversations_processed += 1

        for fact in bee_facts:
            self._write_bee_fact(fact, summary)

        self._state.save(next_cursor, fetched_at, list(self._seen_bee_fact_ids))
        return summary

    def _process_conversation(self, conv: Conversation, summary: RunSummary) -> None:
        if not conv.transcript.strip():
            logger.info("Conversation %s has empty transcript — skipping", conv.id_str)
            summary.conversations_skipped += 1
            return

        logger.info("Processing conversation %s (%s)", conv.id_str, conv.date_str)
        try:
            result: AgentResult = self._agent.process(conv)
        except Exception as exc:
            logger.exception("Agent failed for conversation %s", conv.id_str)
            summary.errors.append(f"Agent error for {conv.id_str}: {exc}")
            return

        summary.timeline_entries_written += result.timeline_entries
        summary.memories_written += result.memories
        summary.action_items_written += result.action_items
        summary.pending_review_written += result.pending_review
        summary.new_entities_written += result.new_entities
        summary.errors.extend(result.errors)

        logger.info(
            "Conversation %s done: %d timeline, %d memories, %d tasks, "
            "%d pending-review, %d new entities, %d tool calls",
            conv.id_str, result.timeline_entries, result.memories,
            result.action_items, result.pending_review,
            result.new_entities, result.tool_calls,
        )

    def _write_bee_fact(self, fact: BeeFact, summary: RunSummary) -> None:
        if fact.id in self._seen_bee_fact_ids:
            logger.info("Skipping already-processed Bee fact %s", fact.id)
            return

        slug = _bee_fact_slug(fact)
        content = _bee_fact_content(fact)
        try:
            self._brain.put_page(slug, content)
            self._brain.add_tag(slug, "pending-review")
            self._brain.add_tag(slug, "bee-fact")
            summary.bee_facts_written += 1
            self._seen_bee_fact_ids.add(fact.id)
        except BrainError as exc:
            logger.error("Failed to write Bee fact %s: %s", slug, exc)
            summary.errors.append(f"Bee fact write error for {fact.id}: {exc}")
