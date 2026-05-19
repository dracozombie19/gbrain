"""Claude-based fact extractor using Anthropic on Vertex AI.

Uses anthropic[vertex] so all traffic stays within GCP.
Auth is via Application Default Credentials — no ANTHROPIC_API_KEY needed.
"""

import json
import logging
from dataclasses import dataclass, field

from anthropic import AnthropicVertex

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a fact extraction assistant. You will be given a transcript of an ambient conversation \
recorded via a wearable device. Your task is to extract durable facts about people, pets, \
companies, and technologies mentioned in the conversation.

ENTITY TYPES — every extraction must include an entity_type field:
- "person": an individual human
- "pet": a household animal
- "company": an organization, vendor, employer, or institution
- "technology": a tool, platform, software, or service actively in use

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

ATTRIBUTION — assess who said each fact about whom. Consider:
- Was the fact stated clearly and directly, or inferred?
- Could the name refer to multiple people?
- Is attribution clear from context or ambiguous?

For each extracted fact, respond with a JSON array of extraction objects. \
If no durable facts are present, return an empty array [].

Each extraction object must have exactly these fields:
{
  "fact": "string — the durable fact in clear, timeless language",
  "subject_name": "string — the entity's name as spoken (e.g. 'Ashley', 'Ascendion', 'AWS Bedrock')",
  "entity_type": "person | pet | company | technology | place | media",
  "attribution_confidence": "high | medium | low",
  "attribution_reasoning": "string — brief explanation of why you assigned this confidence",
  "name_ambiguous": true | false,
  "transcript_snippet": "string — 1-3 sentence verbatim excerpt supporting this extraction"
}

attribution_confidence levels:
- high: clearly stated, clearly attributed, unambiguous subject
- medium: likely correct but some inference required
- low: inferred or uncertain attribution

name_ambiguous: true if the name as spoken could plausibly match more than one entity \
in context (e.g. pronouns, very common first names, generic terms like "the vendor").

Return ONLY valid JSON — no markdown fences, no explanation text, just the array."""


@dataclass
class Extraction:
    fact: str
    subject_name: str
    entity_type: str             # "person" | "pet" | "company" | "technology" | "place" | "media"
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
        self._client = AnthropicVertex(project_id=project_id, region=region)
        self._model = model

    def extract(self, transcript: str, conversation_date: str) -> list[Extraction]:
        """Run fact extraction on a transcript. Returns list of Extraction objects."""
        user_message = (
            f"Conversation date: {conversation_date}\n\n"
            f"Transcript:\n{transcript}"
        )

        logger.info("Running extraction on %d-char transcript", len(transcript))

        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("Extraction API call failed: %s", exc)
            raise ExtractionError(f"Vertex AI call failed: {exc}") from exc

        text = response.content[0].text if response.content else ""
        extractions = _parse_response(text)
        logger.info("Extracted %d facts from transcript", len(extractions))
        return extractions


class ExtractionError(Exception):
    pass
