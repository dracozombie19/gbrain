"""Claude-based fact extractor.

Two implementations:
- Extractor: uses Anthropic on Vertex AI (production — auth via Application Default Credentials)
- LocalExtractor: uses the Anthropic API directly (dry-run / local dev — needs ANTHROPIC_API_KEY)

Use create_extractor(config) to get the right one automatically.
"""

import json
import logging
from dataclasses import dataclass, field

import anthropic

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a fact extraction assistant. You will be given a transcript of an ambient conversation \
recorded via a wearable device. These transcripts are captured passively — they rarely have \
speaker labels, may contain ASR noise or repeated segments, and speakers are usually unnamed. \
Your task is to extract durable facts about people, pets, companies, technologies, places, \
media, and projects mentioned in the conversation.

ENTITY TYPES — every extraction must include an entity_type field:
- "person": an individual human
- "pet": a household animal
- "company": an organization, vendor, employer, or institution
- "technology": a tool, platform, software, or service actively in use
- "place": a named or memorable physical location
- "media": a book, film, TV show, podcast, or article
- "project": a named initiative, build, or renovation

DURABLE facts to extract by entity type:

person:
- Biographical facts: birthdays, relationships, occupations, where someone lives
- Health facts: allergies, medical conditions, dietary restrictions
- Food preferences and dislikes (restaurants, cuisines, specific foods they love or hate)
- Preferences across any domain: music, hobbies, sports, activities, brands
- Significant life events: marriages, births, moves, job changes, deaths
- Worldview and values: religious beliefs, deeply held convictions, identity-level opinions
- Opinions on topics (politics, culture, ideas) that reveal character or persistent beliefs
- Relationship context: how people know each other

pet:
- Species, breed, age, health conditions, dietary needs
- Behavioral traits and preferences

company:
- What the company does or provides ("Ascendion provides managed IT services")
- Vendor/partner relationships ("Schreiber Foods is transitioning from TCS to Ascendion")
- Company capabilities or specializations ("Arthya Tech specializes in SAP support")
- subject_name: use the company or organization name (e.g. "Ascendion", "AWS")
- SKIP: contract values, project timelines, quarterly targets

technology:
- Tools, platforms, or services actively in use ("Zac's team uses AWS Bedrock for LLM inference")
- Organizational deployments ("GitHub Copilot is deployed for the dev team")
- subject_name: use the technology name (e.g. "AWS Bedrock", "DynamoDB", "GitHub Copilot")
- SKIP: technologies merely mentioned, evaluated, or discussed but not actively in use

place:
- Named or describable physical locations with memorable characteristics
- EXTRACT: parks, playgrounds, restaurants, cafes, venues, neighbourhoods with specific \
notable features ("Greenfield Park has an in-ground merry-go-round")
- EXTRACT: associations that aid future recall ("the sushi place on Oak Street with the \
tatami room", "the coffee shop where we always work with the quiet upstairs")
- subject_name: use the place's name, or a short descriptive phrase when no name is known \
(e.g. "Greenfield Park", "the coffee shop on 5th", "the mall playground")
- SKIP: places mentioned only in passing logistics ("the meeting is at the Marriott")
- SKIP: vague references with no memorable detail ("we went somewhere nice")

media:
- Books, films, TV shows, podcasts, or articles explicitly recommended or discussed in depth
- EXTRACT: media with memorable context ("Thinking in Bets — poker book on decision-making \
under uncertainty that Ashley recommended")
- EXTRACT: media associated with a strong opinion, recommendation, or notable detail
- subject_name: use the title as given (e.g. "Atomic Habits", "The Wire", "Lex Fridman Podcast")
- SKIP: media mentioned only in passing ("we watched something last night", "some podcast")
- SKIP: vague references with no title or distinguishing detail

project:
- Named initiatives, migrations, builds, or renovations with their own identity
- EXTRACT: what the project is and its purpose ("the Ascendion migration moves Schreiber Foods \
from TCS to Ascendion for managed IT services")
- EXTRACT: which companies or people are involved and in what role
- EXTRACT: which technologies are central to the project
- subject_name: use the project's name or a short descriptive phrase (e.g. "the Ascendion \
migration", "the SAP upgrade", "the kitchen renovation")
- SKIP: project timelines, deadlines, budgets, and status updates ("goes live in Q4")
- SKIP: one-off task assignments or meeting action items

SITUATIONAL facts to SKIP (do not extract):
- Project timelines, meeting logistics, task assignments
- Temporary emotional states ("she seemed tired today")
- One-time logistical complaints ("the service was slow tonight")
- Anything that would only matter in the next few days

OPINION THRESHOLD — ask: would this still be true about this person next year?
- EXTRACT: "Ashley doesn't like Taco Bell" → food preference, likely stable
- EXTRACT: "He thinks the immigration policy is wrong" → persistent political view
- EXTRACT: "She loves hiking" → hobby/preference
- SKIP: "She's skeptical about the project timeline" → situational work concern
- SKIP: "He said the meeting ran too long" → one-time complaint
- EXTRACT if repeated or stated as general preference; SKIP if clearly a one-time reaction

ATTRIBUTION — IMPORTANT GUIDANCE FOR SPEAKER-UNLABELED TRANSCRIPTS:

These transcripts typically have no speaker labels. attribution_confidence refers to \
confidence in the FACT ABOUT THE SUBJECT, not to knowing which speaker said it. \
A fact can be high-confidence even when you don't know who spoke the words.

Examples of correct attribution reasoning for unlabeled transcripts:
- "James has constipation" → high confidence. The fact is clearly about James regardless \
  of whether Mama or Daddy mentioned it.
- "Bunny tends to bite when she's being held" → high confidence. The fact is about \
  the pet's behaviour, clearly stated in context.
- "The family prays before bed" → high confidence. Clearly a shared household practice, \
  subject is the family/household.
- "Wild Lake Sanctuary has a walking trail" → high confidence. Fact about the place.
- "James and Ella are siblings" → high confidence. Relationship fact clearly stated.

Use LOW confidence only when the fact itself is ambiguous — e.g. it's unclear whether \
the health fact refers to James or another person, or whether the name could match \
multiple entities. The absence of speaker labels does NOT, by itself, lower confidence.

name_ambiguous: true if the name as spoken could plausibly match more than one entity \
in context (e.g. pronouns only, very common first names with no other context, \
generic terms like "the vendor").

For each extracted fact, respond with a JSON array of extraction objects. \
If no durable facts are present, return an empty array [].

Each extraction object must have exactly these fields:
{
  "fact": "string — the durable fact in clear, timeless language",
  "subject_name": "string — the entity's name as spoken (e.g. 'James', 'Bunny', 'Wild Lake Sanctuary')",
  "entity_type": "person | pet | company | technology | place | media | project",
  "attribution_confidence": "high | medium | low",
  "attribution_reasoning": "string — brief explanation of confidence in the fact about the subject",
  "name_ambiguous": true | false,
  "transcript_snippet": "string — 1-3 sentence verbatim excerpt supporting this extraction"
}

Return ONLY valid JSON — no markdown fences, no explanation text, just the array."""


@dataclass
class Extraction:
    fact: str
    subject_name: str
    entity_type: str             # "person" | "pet" | "company" | "technology" | "place" | "media" | "project"
    attribution_confidence: str  # "high" | "medium" | "low"
    attribution_reasoning: str
    name_ambiguous: bool
    transcript_snippet: str


def _parse_response(text: str) -> list[Extraction]:
    """Parse Claude's JSON response into Extraction objects.

    Falls back gracefully: parse errors produce a single low-confidence
    extraction so the conversation isn't silently lost.
    """
    text = text.strip()

    # Strip accidental markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse extractor response as JSON: %s\nRaw: %.200s", exc, text)
        return []

    if not isinstance(data, list):
        logger.warning("Extractor returned non-list JSON type: %s", type(data).__name__)
        return []

    extractions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            extractions.append(Extraction(
                fact=str(item["fact"]),
                subject_name=str(item["subject_name"]),
                entity_type=str(item.get("entity_type", "person")),
                attribution_confidence=str(item.get("attribution_confidence", "low")),
                attribution_reasoning=str(item.get("attribution_reasoning", "")),
                name_ambiguous=bool(item.get("name_ambiguous", True)),
                transcript_snippet=str(item.get("transcript_snippet", "")),
            ))
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed extraction item (%s): %s", exc, item)

    return extractions


class Extractor:
    def __init__(self, project_id: str, region: str, model: str) -> None:
        from anthropic import AnthropicVertex
        self._client = AnthropicVertex(project_id=project_id, region=region)
        self._model = model

    def extract(self, transcript: str, conversation_date: str, summary: str = "") -> list[Extraction]:
        """Run fact extraction on a transcript. Returns list of Extraction objects."""
        user_message = f"Conversation date: {conversation_date}\n\n"
        if summary:
            user_message += f"Bee AI summary (pre-processed interpretation of this conversation):\n{summary}\n\n"
        user_message += f"Raw transcript:\n{transcript}"

        logger.info("Running extraction on %d-char transcript", len(transcript))

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("Extraction API call failed: %s", exc)
            raise ExtractionError(f"Vertex AI call failed: {exc}") from exc

        text = response.content[0].text if response.content else ""
        extractions = _parse_response(text)
        logger.info("Extracted %d facts from transcript", len(extractions))
        if not extractions:
            logger.debug("Empty extraction — raw response: %.300s", text)
        return extractions


class ExtractionError(Exception):
    pass


class LocalExtractor:
    """Fact extractor using the Anthropic API directly (no Vertex AI, no GCP).

    Requires ANTHROPIC_API_KEY. Uses the same prompt and output shape as Extractor.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def extract(self, transcript: str, conversation_date: str, summary: str = "") -> list[Extraction]:
        user_message = f"Conversation date: {conversation_date}\n\n"
        if summary:
            user_message += f"Bee AI summary (pre-processed interpretation of this conversation):\n{summary}\n\n"
        user_message += f"Raw transcript:\n{transcript}"

        logger.info("Running extraction (direct API) on %d-char transcript", len(transcript))
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("Extraction API call failed: %s", exc)
            raise ExtractionError(f"Anthropic API call failed: {exc}") from exc

        text = response.content[0].text if response.content else ""
        extractions = _parse_response(text)
        logger.info("Extracted %d facts from transcript", len(extractions))
        if not extractions:
            logger.debug("Empty extraction — raw response: %.300s", text)
        return extractions


def create_extractor(config) -> "Extractor | LocalExtractor":
    """Return the right extractor based on config."""
    if config.anthropic_api_key:
        logger.info("Using LocalExtractor (direct Anthropic API, model: claude-sonnet-4-6)")
        return LocalExtractor(api_key=config.anthropic_api_key)
    logger.info("Using Extractor (Vertex AI, model: %s)", config.vertex_model)
    return Extractor(config.gcp_project, config.gcp_region, config.vertex_model)
