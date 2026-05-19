"""Entity resolver: maps spoken names to Brain page slugs.

Resolution order:
1. Exact alias match (case-insensitive) from Brain page frontmatter
2. Page title match (case-insensitive)
3. Pronoun / vague reference → pending
4. Unknown name → pending (with candidate slug suggestion)

Aliases are stored in page frontmatter as:
  aliases: ["Ashley", "Mama", "my wife"]

At startup the resolver discovers all person/pet pages via list_pages and
loads their aliases — no hardcoded slug list required. Adding a new person
or pet page to Brain makes them automatically resolvable on the next run.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Page types to load aliases from
_ENTITY_TYPES = ("person", "pet", "company")

# Pronouns and vague references that should always go to pending-review
_VAGUE_REFERENCES = {
    "she", "he", "they", "her", "him", "them",
    "my wife", "my husband", "my partner",
    "the baby", "the kid", "the kids", "the children",
    "my sister", "my brother", "my mom", "my dad",
    "my mother", "my father", "my parents",
    "someone", "somebody", "a friend", "my friend",
    "the dog", "the cat", "the pet",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _name_to_candidate_slug(name: str, entity_type: str = "person") -> str:
    normalized = name.lower().strip()
    parts = _SLUG_RE.sub("-", normalized).strip("-")
    prefix_map = {
        "person": "people",
        "pet": "pets",
        "company": "companies",
        "technology": "tech",
    }
    prefix = prefix_map.get(entity_type, "people")
    return f"{prefix}/{parts}"


@dataclass
class ResolveResult:
    slug: Optional[str]            # None if pending-review
    confidence: str                # "high" | "medium" | "pending"
    is_new_entity: bool = False
    candidate_slug: Optional[str] = None


class Resolver:
    """Discovers entity aliases from Brain and resolves spoken names to slugs."""

    def __init__(self, brain_client) -> None:
        self._brain = brain_client
        self._alias_map: dict[str, str] = {}  # alias.lower() → slug
        self._title_map: dict[str, str] = {}  # title.lower() → slug

    def load(self) -> None:
        """Discover all entity pages from Brain and build alias maps.

        Fetches all pages of type person, pet, and company via list_pages,
        then reads each page's frontmatter for aliases. No slug list needed.
        """
        self._alias_map = {}
        self._title_map = {}

        slugs_seen: set[str] = set()

        for entity_type in _ENTITY_TYPES:
            pages = self._brain.list_pages(type=entity_type, limit=500)
            for summary in pages:
                slug = summary.get("slug")
                if not slug or slug in slugs_seen:
                    continue
                slugs_seen.add(slug)
                self._load_slug(slug)

        # Technology pages are stored as type "concept" with tag "technology"
        tech_pages = self._brain.list_pages(tag="technology", limit=500)
        for summary in tech_pages:
            slug = summary.get("slug")
            if slug and slug not in slugs_seen:
                slugs_seen.add(slug)
                self._load_slug(slug)

        logger.info(
            "Resolver loaded: %d alias entries, %d title entries across %d entity pages",
            len(self._alias_map),
            len(self._title_map),
            len(slugs_seen),
        )

    def _load_slug(self, slug: str) -> None:
        """Load aliases from a single Brain page."""
        page = self._brain.get_page(slug)
        if page is None:
            logger.debug("Page not found during alias load: %s", slug)
            return

        title = page.get("title", "")
        if title:
            self._title_map[title.lower()] = slug

        frontmatter = page.get("frontmatter", {})
        aliases = frontmatter.get("aliases", [])

        if not isinstance(aliases, list):
            return

        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                key = alias.strip().lower()
                if key in self._alias_map and self._alias_map[key] != slug:
                    logger.warning(
                        "Alias collision: '%s' maps to both %s and %s — keeping first",
                        alias, self._alias_map[key], slug,
                    )
                else:
                    self._alias_map[key] = slug

    def resolve(self, spoken_name: str, entity_type: str = "person") -> ResolveResult:
        """Resolve a spoken name to a Brain slug."""
        if not spoken_name or not spoken_name.strip():
            return ResolveResult(slug=None, confidence="pending")

        normalized = spoken_name.strip().lower()

        if normalized in _VAGUE_REFERENCES:
            logger.debug("Vague reference '%s' → pending", spoken_name)
            return ResolveResult(slug=None, confidence="pending")

        if normalized in self._alias_map:
            slug = self._alias_map[normalized]
            logger.debug("Alias match: '%s' → %s", spoken_name, slug)
            return ResolveResult(slug=slug, confidence="high")

        if normalized in self._title_map:
            slug = self._title_map[normalized]
            logger.debug("Title match: '%s' → %s", spoken_name, slug)
            return ResolveResult(slug=slug, confidence="medium")

        candidate = _name_to_candidate_slug(spoken_name, entity_type)
        logger.debug("Unknown '%s' (%s) → pending (candidate: %s)", spoken_name, entity_type, candidate)
        return ResolveResult(
            slug=None,
            confidence="pending",
            is_new_entity=True,
            candidate_slug=candidate,
        )
