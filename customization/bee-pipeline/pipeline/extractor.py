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
You are a knowledge extraction assistant. You will be given a transcript of an ambient \
conversation recorded via a wearable device. These transcripts are captured passively — they \
rarely have speaker labels, may contain ASR noise or repeated segments, and speakers are \
usually unnamed. Your task is to extract facts, memories, and action items worth preserving \
for future recall.

ENTITY TYPES — every extraction must include an entity_type field:
- "person": an individual human
- "pet": a household animal
- "company": an organization, vendor, employer, or institution
- "technology": a tool, platform, software, or service actively in use
- "place": a named or memorable physical location
- "media": a book, film, TV show, podcast, or article
- "project": a named initiative, build, or renovation
- "memory": a notable moment, experience, or episode worth preserving
- "action_item": something that needs to be done or followed up on

WHAT TO EXTRACT by entity type:

person:
- Biographical facts: birthdays, relationships, occupations, where someone lives
- Health facts: allergies, medical conditions, dietary restrictions
- Food preferences and dislikes (restaurants, cuisines, specific foods they love or hate)
- Preferences across any domain: music, hobbies, sports, activities, brands
- Significant life events: marriages, births, moves, job changes, deaths
- Worldview and values: religious beliefs, deeply held convictions, identity-level opinions
- Opinions on topics (politics, culture, ideas) that reveal character or persistent beliefs
- Relationship context: how people know each other
- For children (under ~5 years old): developmental milestones, physical details, and \
  contextual facts even if they'll only be true for a few months. Childhood changes fast — \
  capture the texture of this stage. Examples: clothing sizes, foods they've just discovered, \
  skills recently acquired, funny habits, height/weight, how they're sleeping, what they're \
  saying, quirky details that will be interesting to look back on.
- EXTRACT even if not "forever durable" for children: "Ella still fits in her 0-3 month \
  outfit at 9 months", "James is going through a phase of only eating beige foods"

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

memory:
- Notable moments, experiences, or episodes worth preserving — things you'd want to look \
  back on in a year or five years
- EXTRACT: firsts and milestones ("Ella swam for the first time", "James tried sushi")
- EXTRACT: funny, sweet, or surprising moments that capture a period of life
- EXTRACT: meaningful family or personal experiences (trips, celebrations, shared moments)
- EXTRACT: observations that will only be true for a short window ("at 9 months, Ella \
  still fits in the 0-3 month outfit we use for her monthly photos — makes the photos funnier \
  every month")
- subject_name: a short phrase describing what the memory is about (e.g. "Ella's first swim", \
  "family walk at Wild Lake Sanctuary", "James tries sushi for the first time")
- fact: write as a complete sentence capturing the moment and its context
- SKIP: routine everyday activities with nothing remarkable about them
- SKIP: moments you'd only care about for the next day or two with no lasting significance

action_item:
- Things that need to be done, followed up on, or remembered to act on
- EXTRACT: clear tasks with an identifiable action ("call the pediatrician to schedule \
  Ella's 9-month checkup", "order more formula", "text Sarah about the playdate")
- EXTRACT: time-sensitive reminders, especially when a date or timeframe is mentioned \
  ("pick up the photos before Friday", "renew the car registration before end of month")
- EXTRACT: follow-ups from conversations ("circle back with James's teacher about the \
  reading program next week")
- subject_name: a short phrase describing the task (e.g. "schedule Ella's checkup", \
  "pick up photos")
- fact: write the action item as a clear, actionable sentence
- due_date: include if a specific date or timeframe was mentioned (YYYY-MM-DD if exact; \
  natural language like "end of week" or "before Friday" if relative; null if not mentioned
- SKIP: vague intentions with no clear action ("we should do that sometime")
- SKIP: professional meeting logistics with no personal follow-up needed

SITUATIONAL facts to SKIP (for entity types person, pet, company, technology, place, media, project):
- Temporary emotional states ("she seemed tired today")
- One-time logistical complaints ("the service was slow tonight")
- Work meeting logistics with no lasting relevance
- Anything clearly only relevant for the next 24-48 hours and with no lasting significance
- NOTE: action items and memories have their own types above — use those instead of skipping

OPINION THRESHOLD for person type — ask: would this be interesting to recall in 3-6 months?
- EXTRACT: "Ashley doesn't like Taco Bell" → food preference, likely stable
- EXTRACT: "He thinks the immigration policy is wrong" → persistent political view
- EXTRACT: "She loves hiking" → hobby/preference
- EXTRACT (for children): "Ella still fits in the 0-3 month outfit at 9 months" → \
  developmental/contextual detail, worth capturing even though it'll change
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

For memory and action_item types: attribution_confidence refers to how clearly the \
memory or task is stated in the transcript, not to speaker identity. name_ambiguous \
should be false for these types unless there is genuine ambiguity about which person \
or event is being referenced.

name_ambiguous: true if the name as spoken could plausibly match more than one entity \
in context (e.g. pronouns only, very common first names with no other context, \
generic terms like "the vendor").

For each extracted item, respond with a JSON array of extraction objects. \
If nothing worth extracting is present, return an empty array [].

Each extraction object must have exactly these fields:
{
  "fact": "string — the fact, memory, or action item in clear language",
  "subject_name": "string — entity name or short phrase describing what this is about",
  "entity_type": "person | pet | company | technology | place | media | project | memory | action_item",
  "attribution_confidence": "high | medium | low",
  "attribution_reasoning": "string — brief explanation of confidence",
  "name_ambiguous": true | false,
  "transcript_snippet": "string — 1-3 sentence verbatim excerpt supporting this extraction",
  "due_date": "YYYY-MM-DD or short phrase or null — only for action_item type; null for all others",
  "participants": ["list of entity names (people, pets, places) involved — for memory and action_item types only; empty array for all other types"]
}

Return ONLY valid JSON — no markdown fences, no explanation text, just the array."""


@dataclass
class Extraction:
    fact: str
    subject_name: str
    entity_type: str             # "person" | "pet" | "company" | "technology" | "place" | "media" | "project" | "memory" | "action_item"
    attribution_confidence: str  # "high" | "medium" | "low"
    attribution_reasoning: str
    name_ambiguous: bool
    transcript_snippet: str
    due_date: str | None = None          # only for action_item type
    participants: list[str] = field(default_factory=list)  # entity names involved; memory and action_item types only


def _salvage_truncated_json(text: str) -> list | None:
    """Try to recover complete objects from a truncated JSON array.

    When the model hits max_tokens mid-array, the response is valid JSON up to
    the cut point. Find the last complete object (closing `}`) and close the
    array there. Returns the parsed list, or None if nothing is recoverable.
    """
    last_brace = text.rfind("}")
    if last_brace == -1:
        return None
    candidate = text[: last_brace + 1] + "]"
    # Strip any leading non-`[` characters to find the array open
    open_bracket = candidate.find("[")
    if open_bracket == -1:
        return None
    try:
        data = json.loads(candidate[open_bracket:])
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return None


def _parse_response(text: str, truncated: bool = False) -> list[Extraction]:
    """Parse Claude's JSON response into Extraction objects."""
    text = text.strip()

    # Strip accidental markdown fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        if truncated:
            data = _salvage_truncated_json(text)
            if data is None:
                raise ExtractionError(
                    f"max_tokens reached and truncated JSON is unrecoverable "
                    f"(increase extraction_max_tokens). Raw tail: {text[-200:]!r}"
                )
            logger.warning(
                "max_tokens truncation: salvaged %d complete object(s) from partial response",
                len(data),
            )
        else:
            logger.warning("Failed to parse extractor response as JSON. Raw: %.200s", text)
            return []

    if not isinstance(data, list):
        logger.warning("Extractor returned non-list JSON type: %s", type(data).__name__)
        return []

    extractions = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            raw_due = item.get("due_date")
            due_date = str(raw_due) if raw_due and str(raw_due).lower() not in ("null", "none", "") else None
            raw_participants = item.get("participants", [])
            participants = [
                str(p).strip() for p in raw_participants
                if isinstance(p, str) and str(p).strip()
            ] if isinstance(raw_participants, list) else []
            extractions.append(Extraction(
                fact=str(item["fact"]),
                subject_name=str(item["subject_name"]),
                entity_type=str(item.get("entity_type", "person")),
                attribution_confidence=str(item.get("attribution_confidence", "low")),
                attribution_reasoning=str(item.get("attribution_reasoning", "")),
                name_ambiguous=bool(item.get("name_ambiguous", True)),
                transcript_snippet=str(item.get("transcript_snippet", "")),
                due_date=due_date,
                participants=participants,
            ))
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping malformed extraction item (%s): %s", exc, item)

    return extractions


class Extractor:
    def __init__(self, project_id: str, region: str, model: str, max_tokens: int = 8192) -> None:
        from anthropic import AnthropicVertex
        self._client = AnthropicVertex(project_id=project_id, region=region)
        self._model = model
        self._max_tokens = max_tokens

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
                max_tokens=self._max_tokens,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("Extraction API call failed: %s", exc)
            raise ExtractionError(f"Vertex AI call failed: {exc}") from exc

        truncated = response.stop_reason == "max_tokens"
        if truncated:
            logger.error(
                "Extraction hit max_tokens limit (%d) — response truncated. "
                "Attempting salvage. Consider increasing extraction_max_tokens.",
                self._max_tokens,
            )

        text = response.content[0].text if response.content else ""
        extractions = _parse_response(text, truncated=truncated)
        logger.info("Extracted %d facts from transcript", len(extractions))
        if truncated:
            logger.warning("Truncated extraction yielded %d salvaged fact(s)", len(extractions))
        elif not extractions:
            logger.debug("Empty extraction — raw response: %.300s", text)
        return extractions


class ExtractionError(Exception):
    pass


class LocalExtractor:
    """Fact extractor using the Anthropic API directly (no Vertex AI, no GCP).

    Requires ANTHROPIC_API_KEY. Uses the same prompt and output shape as Extractor.
    """

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", max_tokens: int = 8192) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def extract(self, transcript: str, conversation_date: str, summary: str = "") -> list[Extraction]:
        user_message = f"Conversation date: {conversation_date}\n\n"
        if summary:
            user_message += f"Bee AI summary (pre-processed interpretation of this conversation):\n{summary}\n\n"
        user_message += f"Raw transcript:\n{transcript}"

        logger.info("Running extraction (direct API) on %d-char transcript", len(transcript))
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception as exc:
            logger.error("Extraction API call failed: %s", exc)
            raise ExtractionError(f"Anthropic API call failed: {exc}") from exc

        truncated = response.stop_reason == "max_tokens"
        if truncated:
            logger.error(
                "Extraction hit max_tokens limit (%d) — response truncated. "
                "Attempting salvage. Consider increasing extraction_max_tokens.",
                self._max_tokens,
            )

        text = response.content[0].text if response.content else ""
        extractions = _parse_response(text, truncated=truncated)
        logger.info("Extracted %d facts from transcript", len(extractions))
        if truncated:
            logger.warning("Truncated extraction yielded %d salvaged fact(s)", len(extractions))
        elif not extractions:
            logger.debug("Empty extraction — raw response: %.300s", text)
        return extractions


def create_extractor(config) -> "Extractor | LocalExtractor":
    """Return the right extractor based on config."""
    max_tokens = config.extraction_max_tokens
    if config.anthropic_api_key:
        logger.info("Using LocalExtractor (direct Anthropic API, model: claude-sonnet-4-6, max_tokens: %d)", max_tokens)
        return LocalExtractor(api_key=config.anthropic_api_key, max_tokens=max_tokens)
    logger.info("Using Extractor (Vertex AI, model: %s, max_tokens: %d)", config.vertex_model, max_tokens)
    return Extractor(config.gcp_project, config.gcp_region, config.vertex_model, max_tokens=max_tokens)
