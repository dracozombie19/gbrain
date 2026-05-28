"""Agent-based conversation processor using Claude tool-use.

Replaces the extractor + resolver + heuristic routing with a single Claude
loop that reads the transcript, checks Brain for existing content, and writes
with full context and reasoning.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import anthropic

from .bee_client import Conversation
from .brain_client import BrainClient, DryRunBrainClient, BrainError
from .config import Config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tool definitions                                                             #
# --------------------------------------------------------------------------- #

BRAIN_TOOLS = [
    {
        "name": "search",
        "description": (
            "Search Brain pages by keyword. Use before creating entity pages "
            "to check if the entity already exists. Returns matching pages with "
            "their slugs and titles."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_page",
        "description": (
            "Get a Brain page by slug. Use to read existing content before "
            "writing, or to check whether an entity page already contains a fact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
            },
            "required": ["slug"],
        },
    },
    {
        "name": "list_pages",
        "description": "List Brain pages filtered by tag or type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string"},
                "type": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "add_timeline_entry",
        "description": (
            "Add a durable fact as a timeline entry on an existing entity page. "
            "Use for facts about people, pets, places, companies, or projects. "
            "Confirm the entity page exists with get_page or search first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Entity page slug"},
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "summary": {"type": "string", "description": "The fact as a present-tense statement"},
                "detail": {"type": "string", "description": "Optional context or source snippet"},
                "source": {"type": "string", "description": "Source identifier e.g. bee:conv-id"},
            },
            "required": ["slug", "date", "summary"],
        },
    },
    {
        "name": "put_page",
        "description": (
            "Create or update a Brain page. Use for: "
            "(1) memories at memories/YYYY-MM-DD-slug, "
            "(2) action items at tasks/YYYY-MM-DD-slug, "
            "(3) uncertain entity facts — ONE PAGE PER INDIVIDUAL FACT — at "
            "pending-review/YYYY-MM-DD-{conv_suffix}-N, incrementing N for each fact, "
            "(4) new entity stub pages. "
            "Content must be full markdown with YAML frontmatter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "content": {"type": "string", "description": "Full markdown with YAML frontmatter"},
            },
            "required": ["slug", "content"],
        },
    },
    {
        "name": "add_tag",
        "description": "Add a tag to a Brain page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "slug": {"type": "string"},
                "tag": {"type": "string"},
            },
            "required": ["slug", "tag"],
        },
    },
    {
        "name": "add_link",
        "description": "Link one Brain page to another, e.g. a memory to the people it involves.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_slug": {"type": "string"},
                "to_slug": {"type": "string"},
                "link_type": {"type": "string", "default": "mentions"},
            },
            "required": ["from_slug", "to_slug"],
        },
    },
]


# --------------------------------------------------------------------------- #
# System prompt                                                                #
# --------------------------------------------------------------------------- #

_SYSTEM_PROMPT = """\
You are processing a transcript from a Bee wearable device worn by Zac Dowd. \
Your job is to extract anything worth preserving in his personal knowledge Brain \
and write it there using the tools provided.

## What to extract

**Entity facts** — durable, specific facts about named entities: people, pets, places, \
companies, or projects. A job change, a health note, a relationship detail, a preference, \
a milestone, something developmental about Ella, James, or any future kids.
→ Use add_timeline_entry on the entity's existing page. If the entity page doesn't exist \
yet, create a stub with put_page first, then add_timeline_entry.

**Memories** — episodic moments worth remembering that aren't durable facts. \
Funny observations, notable firsts, things that capture a point in time. \
Be especially generous with children (Ella, James, or any future kids) — developmental milestones, funny moments, size observations, \
things that will be meaningful to look back on.
→ Write to memories/YYYY-MM-DD-brief-slug

**Action items** — tasks or follow-ups explicitly mentioned or clearly implied.
→ Write to tasks/YYYY-MM-DD-brief-slug. Include due_date in frontmatter if one was mentioned.

**Skip**: small talk, generic filler, vague statements, things obviously already known.

## Workflow

1. Read the transcript carefully.
2. For each entity fact: use search or get_page to find the entity first. \
   If found and the fact is new, use add_timeline_entry. \
   If the entity doesn't exist and you're confident about the identity, create a stub with put_page \
   then add_timeline_entry. \
   If you're uncertain who the entity is, write a pending-review page instead. \
   Each uncertain fact gets its own pending-review page — do not bundle multiple facts into one page.
3. Write memories and action items directly without entity resolution.
4. After writing memory and task pages, use add_link to connect them to relevant entity pages.
5. When done, stop. Don't fabricate tool calls or pad with unnecessary writes.

## Brain conventions

Slug patterns:
- people/firstname-lastname  (e.g. people/ashley-dowd, people/ella-dowd)
- pets/name
- places/place-name
- companies/company-name
- projects/project-name
- memories/YYYY-MM-DD-brief-slug
- tasks/YYYY-MM-DD-brief-slug
- pending-review/YYYY-MM-DD-{conv_suffix}-N

Page types: person, company, project, media. Pets use type: note + tags: [pet]. \
Places use type: note + tags: [place].

Frontmatter for memories:
```yaml
---
title: "Memory title"
type: note
tags: [memory, bee-extracted, bee-unreviewed]
bee_conversation_id: "{conv_id}"
memory_date: "YYYY-MM-DD"
---
```

Frontmatter for action items:
```yaml
---
title: "Task description"
type: note
tags: [task, bee-extracted, bee-unreviewed]
status: open
bee_conversation_id: "{conv_id}"
created_date: "YYYY-MM-DD"
due_date: "YYYY-MM-DD"   # only if mentioned
---
```

Frontmatter for pending-review (uncertain entity attribution):
```yaml
---
title: "Pending Review: brief description"
type: note
tags: [pending-review, bee-extracted]
bee_conversation_id: "{conv_id}"
bee_conversation_date: "YYYY-MM-DD"
---
```

Frontmatter for a new entity stub:
```yaml
---
title: "Full Name"
type: person          # or company, project, etc.
aliases: ["Name as spoken"]
---
Stub created via Bee pipeline on YYYY-MM-DD.
```

Source on all timeline entries: bee:{conv_id}

## Key people

- **Ashley Dowd** (people/ashley-dowd) — Zac's wife. "my wife", "babe", "mama" → Ashley.
- **Ella Dowd** (people/elowen-dowd) — Zac's infant daughter. "the baby", "she" in baby \
context → Ella. Capture her developmental details generously.
- **James Dowd** (people/james-dowd) — Zac's son. "my son", "the boy" → James.
- **Zac Dowd** (people/zac-dowd) — the wearer. Facts explicitly about him go here.
"""

_PENDING_REVIEW_OVERRIDE = """
## AUDIT MODE — pending-review only

Route ALL entity facts to pending-review pages regardless of confidence. \
Do not write directly to entity pages or create new entity stubs. \
Memories and action items still write to their normal namespaces.
"""


# --------------------------------------------------------------------------- #
# Result                                                                       #
# --------------------------------------------------------------------------- #

@dataclass
class AgentResult:
    timeline_entries: int = 0
    memories: int = 0
    action_items: int = 0
    pending_review: int = 0
    new_entities: int = 0
    tool_calls: int = 0
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Agent                                                                        #
# --------------------------------------------------------------------------- #

class ConversationAgent:
    """Runs a Claude tool-use loop for a single Bee conversation."""

    MAX_TURNS = 40  # safety limit; a busy conversation needs ~10-20 turns

    def __init__(self, config: Config, brain: "BrainClient | DryRunBrainClient") -> None:
        self._config = config
        self._brain = brain
        self._client = self._build_client()

    def _build_client(self) -> anthropic.Anthropic | anthropic.AnthropicVertex:
        if self._config.anthropic_api_key:
            return anthropic.Anthropic(api_key=self._config.anthropic_api_key, max_retries=6)
        return anthropic.AnthropicVertex(
            project_id=self._config.gcp_project,
            region=self._config.gcp_region,
            max_retries=6,
        )

    def _model(self) -> str:
        if self._config.anthropic_api_key:
            return self._config.agent_model
        return self._config.vertex_model

    def process(self, conv: Conversation) -> AgentResult:
        result = AgentResult()
        conv_id = conv.id_str
        conv_suffix = conv_id[-6:] if len(conv_id) >= 6 else conv_id

        system = (
            _SYSTEM_PROMPT
            + (_PENDING_REVIEW_OVERRIDE if self._config.pending_review_only else "")
        ).replace("{conv_id}", conv_id).replace("{conv_suffix}", conv_suffix)

        summary_block = ""
        if conv.summary:
            summary_block = (
                f"Bee's auto-generated summary (use for orientation only — "
                f"speaker attribution is frequently wrong, especially names; "
                f"always verify facts against the transcript before writing):\n"
                f"{conv.summary}\n\n"
            )

        messages: list[dict] = [{
            "role": "user",
            "content": (
                f"Conversation ID: {conv_id}\n"
                f"Date: {conv.date_str}\n\n"
                f"{summary_block}"
                f"Transcript:\n{conv.transcript}"
            ),
        }]

        for turn in range(self.MAX_TURNS):
            response = self._client.messages.create(
                model=self._model(),
                max_tokens=4096,
                system=system,
                tools=BRAIN_TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                logger.info(
                    "Agent finished conversation %s in %d turns (%d tool calls)",
                    conv_id, turn + 1, result.tool_calls,
                )
                break

            if response.stop_reason != "tool_use":
                logger.warning(
                    "Unexpected stop_reason=%s for conversation %s",
                    response.stop_reason, conv_id,
                )
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result.tool_calls += 1
                outcome = self._execute_tool(block.name, block.input, conv_id, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(outcome),
                })

            messages.append({"role": "user", "content": tool_results})

        else:
            logger.warning(
                "Agent hit MAX_TURNS=%d for conversation %s — output may be incomplete",
                self.MAX_TURNS, conv_id,
            )

        return result

    def _execute_tool(
        self,
        name: str,
        inputs: dict,
        conv_id: str,
        result: AgentResult,
    ) -> Any:
        logger.debug("Tool: %s inputs=%s", name, list(inputs.keys()))
        try:
            return self._dispatch(name, inputs, conv_id, result)
        except BrainError as exc:
            logger.error("Brain tool %s failed: %s", name, exc)
            result.errors.append(f"{name}: {exc}")
            return {"error": str(exc)}
        except Exception as exc:
            logger.error("Unexpected error in tool %s: %s", name, exc)
            result.errors.append(f"{name}: {exc}")
            return {"error": str(exc)}

    def _dispatch(
        self,
        name: str,
        inputs: dict,
        conv_id: str,
        result: AgentResult,
    ) -> Any:
        if name == "search":
            return self._brain.search(inputs["query"], limit=inputs.get("limit", 10))

        if name == "get_page":
            page = self._brain.get_page(inputs["slug"])
            return page if page is not None else {"not_found": True}

        if name == "list_pages":
            return self._brain.list_pages(
                tag=inputs.get("tag"),
                type=inputs.get("type"),
                limit=inputs.get("limit", 50),
            )

        if name == "add_timeline_entry":
            if self._config.pending_review_only:
                return {
                    "error": (
                        "pending_review_only mode is active — "
                        "write entity facts to pending-review/ with put_page instead"
                    )
                }
            self._brain.add_timeline_entry(
                slug=inputs["slug"],
                date=inputs["date"],
                summary=inputs["summary"],
                detail=inputs.get("detail"),
                source=inputs.get("source", f"bee:{conv_id}"),
            )
            result.timeline_entries += 1
            return {"ok": True}

        if name == "put_page":
            slug = inputs["slug"]
            _ALLOWED_PREFIXES = ("memories/", "tasks/", "pending-review/")
            if self._config.pending_review_only and not slug.startswith(_ALLOWED_PREFIXES):
                return {
                    "error": (
                        f"pending_review_only mode is active — "
                        f"slug '{slug}' must start with pending-review/, memories/, or tasks/"
                    )
                }
            self._brain.put_page(slug, inputs["content"])
            if slug.startswith("memories/"):
                result.memories += 1
            elif slug.startswith("tasks/"):
                result.action_items += 1
            elif slug.startswith("pending-review/"):
                result.pending_review += 1
            else:
                result.new_entities += 1
            return {"ok": True, "slug": slug}

        if name == "add_tag":
            self._brain.add_tag(inputs["slug"], inputs["tag"])
            return {"ok": True}

        if name == "add_link":
            self._brain.add_link(
                inputs["from_slug"],
                inputs["to_slug"],
                inputs.get("link_type", "mentions"),
            )
            return {"ok": True}

        return {"error": f"unknown tool: {name}"}
