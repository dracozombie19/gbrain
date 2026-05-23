"""Pipeline state management for Bee cursor tracking.

Two implementations:
- StateManager: GCS-backed (production)
- LocalStateManager: local JSON file (dry-run / local dev — no GCP needed)

Use create_state_manager(config) to get the right one automatically.
"""

import datetime
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from google.cloud import storage

logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    cursor: Optional[str] = None          # Bee next_cursor from last successful run
    last_processed_at: Optional[str] = None  # ISO 8601 timestamp of last save
    seen_bee_fact_ids: list = field(default_factory=list)  # Bee fact IDs already written (prevents re-creation after delete)


class StateManager:
    def __init__(self, bucket_name: str, state_object: str, failed_prefix: str) -> None:
        self._client = storage.Client()
        self._bucket = self._client.bucket(bucket_name)
        self._object = state_object
        self._failed_prefix = failed_prefix
        self._state: Optional[PipelineState] = None

    def load(self) -> PipelineState:
        """Load state from GCS. Returns blank state on first run."""
        blob = self._bucket.blob(self._object)
        try:
            data = json.loads(blob.download_as_text())
            self._state = PipelineState(
                cursor=data.get("cursor"),
                last_processed_at=data.get("last_processed_at"),
                seen_bee_fact_ids=data.get("seen_bee_fact_ids", []),
            )
            logger.info(
                "Loaded pipeline state: cursor=%s, %d seen bee fact IDs",
                self._state.cursor, len(self._state.seen_bee_fact_ids),
            )
        except Exception as exc:
            logger.info("No existing state (%s) — starting fresh", exc)
            self._state = PipelineState()
        return self._state

    def save(
        self,
        next_cursor: Optional[str],
        fetched_at: str,
        seen_bee_fact_ids: Optional[list] = None,
    ) -> None:
        """Persist state to GCS with the new cursor.

        fetched_at should be the timestamp captured just before calling
        `bee changed`, so last_processed_at reflects when the data snapshot
        was taken rather than when processing finished.
        """
        if self._state is None:
            raise RuntimeError("Call load() before save()")
        self._state.cursor = next_cursor
        self._state.last_processed_at = fetched_at
        if seen_bee_fact_ids is not None:
            self._state.seen_bee_fact_ids = seen_bee_fact_ids
        blob = self._bucket.blob(self._object)
        blob.upload_from_string(
            json.dumps({
                "cursor": self._state.cursor,
                "last_processed_at": self._state.last_processed_at,
                "seen_bee_fact_ids": self._state.seen_bee_fact_ids,
            }, indent=2),
            content_type="application/json",
        )
        logger.info(
            "Saved pipeline state: cursor=%s, %d seen bee fact IDs",
            self._state.cursor, len(self._state.seen_bee_fact_ids),
        )

    def log_failed(self, entry: dict) -> None:
        """Append a failed extraction to a JSONL file in GCS for later review."""
        date_str = datetime.date.today().isoformat()
        blob = self._bucket.blob(f"{self._failed_prefix}/{date_str}.jsonl")

        existing = ""
        try:
            existing = blob.download_as_text()
        except Exception:
            pass

        line = json.dumps(entry, ensure_ascii=False)
        blob.upload_from_string(
            existing + line + "\n",
            content_type="application/x-ndjson",
        )
        logger.info("Logged failed entry to %s/%s.jsonl", self._failed_prefix, date_str)


class LocalStateManager:
    """Local-file-backed state manager for dry-run / local development.

    Stores cursor state in a JSON file on disk. Failed extractions are
    appended to a sibling JSONL file. No GCP credentials required.
    """

    def __init__(self, state_path: str) -> None:
        self._state_path = state_path
        self._failed_path = state_path.replace(".json", "-failed.jsonl")
        self._state: Optional[PipelineState] = None

    def load(self) -> PipelineState:
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            self._state = PipelineState(
                cursor=data.get("cursor"),
                last_processed_at=data.get("last_processed_at"),
                seen_bee_fact_ids=data.get("seen_bee_fact_ids", []),
            )
            logger.info(
                "Loaded local pipeline state from %s: cursor=%s, %d seen bee fact IDs",
                self._state_path, self._state.cursor, len(self._state.seen_bee_fact_ids),
            )
        except FileNotFoundError:
            logger.info("No local state file at %s — starting fresh", self._state_path)
            self._state = PipelineState()
        except Exception as exc:
            logger.warning("Could not read local state (%s) — starting fresh", exc)
            self._state = PipelineState()
        return self._state

    def save(
        self,
        next_cursor: Optional[str],
        fetched_at: str,
        seen_bee_fact_ids: Optional[list] = None,
    ) -> None:
        if self._state is None:
            raise RuntimeError("Call load() before save()")
        self._state.cursor = next_cursor
        self._state.last_processed_at = fetched_at
        if seen_bee_fact_ids is not None:
            self._state.seen_bee_fact_ids = seen_bee_fact_ids
        with open(self._state_path, "w") as f:
            json.dump({
                "cursor": self._state.cursor,
                "last_processed_at": self._state.last_processed_at,
                "seen_bee_fact_ids": self._state.seen_bee_fact_ids,
            }, f, indent=2)
        logger.info(
            "Saved local pipeline state to %s: cursor=%s, %d seen bee fact IDs",
            self._state_path, self._state.cursor, len(self._state.seen_bee_fact_ids),
        )

    def log_failed(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with open(self._failed_path, "a") as f:
            f.write(line + "\n")
        logger.info("Logged failed entry to %s", self._failed_path)


def create_state_manager(config) -> "StateManager | LocalStateManager":
    """Return the right state manager based on config."""
    if config.local_state_path:
        return LocalStateManager(config.local_state_path)
    return StateManager(config.gcs_bucket, config.gcs_state_object, config.gcs_failed_prefix)
