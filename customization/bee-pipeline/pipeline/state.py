"""GCS-backed pipeline state for Bee cursor tracking."""

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Optional

from google.cloud import storage

logger = logging.getLogger(__name__)


@dataclass
class PipelineState:
    cursor: Optional[str] = None          # Bee next_cursor from last successful run
    last_processed_at: Optional[str] = None  # ISO 8601 timestamp of last save


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
            )
            logger.info("Loaded pipeline state: cursor=%s", self._state.cursor)
        except Exception as exc:
            logger.info("No existing state (%s) — starting fresh", exc)
            self._state = PipelineState()
        return self._state

    def save(self, next_cursor: Optional[str], fetched_at: str) -> None:
        """Persist state to GCS with the new cursor.

        fetched_at should be the timestamp captured just before calling
        `bee changed`, so last_processed_at reflects when the data snapshot
        was taken rather than when processing finished.
        """
        if self._state is None:
            raise RuntimeError("Call load() before save()")
        self._state.cursor = next_cursor
        self._state.last_processed_at = fetched_at
        blob = self._bucket.blob(self._object)
        blob.upload_from_string(
            json.dumps({
                "cursor": self._state.cursor,
                "last_processed_at": self._state.last_processed_at,
            }, indent=2),
            content_type="application/json",
        )
        logger.info("Saved pipeline state: cursor=%s", self._state.cursor)

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
