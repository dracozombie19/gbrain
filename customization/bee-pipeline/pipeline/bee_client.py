"""Bee CLI wrapper: fetches conversations via `bee changed --json`.

Authentication: `bee login --token-stdin` must be called once before any
data fetch. The token is the JWT from BEE_API_TOKEN secret (no expiry).

Data shape (verified from real `bee changed --json` output):
  conversations[]:
    id          int
    start_time  int  (Unix ms)
    created_at  int  (Unix ms)
    state       str  ("COMPLETED" | "PROCESSING" | ...)
    transcriptions[]:
      utterances[]:
        text    str
        speaker str  (always "Unknown")
        start   int  (ms offset from conversation start)

Cursor: meta.next_cursor is an opaque string like "v1-1779159682636".
Pass it to --cursor on the next call to get only newer conversations.
"""

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_CLI_TIMEOUT = 60  # seconds


def _bee(*args: str) -> list[str]:
    """Build a bee CLI invocation that works on both Windows and Unix.

    On Windows, npm global packages install as .cmd wrappers that can't be
    executed directly by subprocess without shell=True. Routing through
    `cmd /c` lets Windows resolve the .cmd extension.
    """
    if sys.platform == "win32":
        return ["cmd", "/c", "bee"] + list(args)
    return ["bee"] + list(args)


class BeeCliError(Exception):
    pass


@dataclass
class Utterance:
    text: str
    speaker: str
    start_ms: int


@dataclass
class Conversation:
    id: int
    start_time_ms: int
    created_at_ms: int
    state: str
    utterances: list[Utterance] = field(default_factory=list)
    summary: str = ""
    short_summary: str = ""

    @property
    def transcript(self) -> str:
        """Assemble utterances into plain text (one line per utterance)."""
        return "\n".join(u.text for u in self.utterances if u.text.strip())

    @property
    def date_str(self) -> str:
        """YYYY-MM-DD from start_time (UTC)."""
        try:
            dt = datetime.fromtimestamp(self.start_time_ms / 1000, tz=timezone.utc)
            return dt.date().isoformat()
        except Exception:
            return datetime.now(tz=timezone.utc).date().isoformat()

    @property
    def id_str(self) -> str:
        return str(self.id)


@dataclass
class ChangedResult:
    conversations: list[Conversation]
    next_cursor: Optional[str]


def _parse_conversation(raw: dict) -> Optional[Conversation]:
    """Parse one conversation dict from `bee changed` output.

    Returns None if the conversation should be skipped (wrong state, no
    transcript, parse error).
    """
    conv_id = raw.get("id")
    state = raw.get("state", "")
    if state != "COMPLETED":
        logger.debug("Skipping conversation %s with state=%s", conv_id, state)
        return None

    utterances = []
    for trans in raw.get("transcriptions", []):
        for u in trans.get("utterances", []):
            text = (u.get("text") or "").strip()
            if text:
                utterances.append(Utterance(
                    text=text,
                    speaker=u.get("speaker", "Unknown"),
                    start_ms=u.get("start", 0),
                ))

    if not utterances:
        logger.debug("Skipping conversation %s with no utterances", conv_id)
        return None

    return Conversation(
        id=int(conv_id),
        start_time_ms=int(raw.get("start_time", raw.get("created_at", 0))),
        created_at_ms=int(raw.get("created_at", 0)),
        state=state,
        utterances=utterances,
        summary=str(raw.get("summary", "") or ""),
        short_summary=str(raw.get("short_summary", "") or ""),
    )


class BeeClient:
    def __init__(self, token: str) -> None:
        self._token = token
        self._logged_in = False

    def login(self) -> None:
        """Authenticate the Bee CLI via `bee login --token-stdin`.

        Idempotent — safe to call multiple times.
        """
        if self._logged_in:
            return
        logger.info("Authenticating Bee CLI")
        try:
            result = subprocess.run(
                _bee("login", "--token-stdin"),
                input=self._token,
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise BeeCliError(f"bee login failed: {exc}") from exc

        if result.returncode != 0:
            raise BeeCliError(
                f"bee login exited {result.returncode}: {result.stderr.strip()}"
            )
        logger.info("Bee CLI authenticated")
        self._logged_in = True

    def get_changed(self, cursor: Optional[str] = None) -> ChangedResult:
        """Run `bee changed [--cursor CURSOR] --json` and return parsed results.

        cursor=None fetches all available conversations (first run).
        cursor=<opaque string> fetches only conversations newer than that cursor.

        Returns conversations sorted oldest-first so the pipeline can process
        them in order. The next_cursor in the result should be saved to GCS
        after all conversations in this batch are processed.
        """
        self.login()

        cmd = _bee("changed", "--json") + (["--cursor", cursor] if cursor else [])

        logger.info("Running: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise BeeCliError(f"bee changed timed out after {_CLI_TIMEOUT}s") from exc
        except FileNotFoundError as exc:
            raise BeeCliError(
                "bee CLI not found. Install with: npm install -g @beeai/cli\n"
                "Then verify: bee --version"
            ) from exc

        if result.returncode != 0:
            raise BeeCliError(
                f"bee changed exited {result.returncode}: {result.stderr.strip()}"
            )

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BeeCliError(
                f"Failed to parse bee changed output: {exc}. "
                f"stdout[:200]: {result.stdout[:200]}"
            ) from exc

        raw_conversations = data.get("conversations", [])
        meta = data.get("meta", {})
        next_cursor = meta.get("next_cursor")

        conversations = []
        for raw in raw_conversations:
            try:
                conv = _parse_conversation(raw)
                if conv is not None:
                    conversations.append(conv)
            except Exception as exc:
                logger.warning("Failed to parse conversation %s: %s", raw.get("id"), exc)

        # Oldest-first so pipeline advances state in chronological order
        conversations.sort(key=lambda c: c.start_time_ms)

        logger.info(
            "bee changed: %d raw → %d usable conversations, next_cursor=%s",
            len(raw_conversations),
            len(conversations),
            next_cursor,
        )
        return ChangedResult(conversations=conversations, next_cursor=next_cursor)
