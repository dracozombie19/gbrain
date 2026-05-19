"""Brain HTTP MCP client — speaks MCP JSON-RPC 2.0 with OAuth client_credentials."""

import time
import logging
from typing import Any
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_DEFAULT_TIMEOUT = 30
_TOKEN_EXPIRY_BUFFER_SECONDS = 60


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0

    def is_valid(self) -> bool:
        return bool(self.access_token) and time.time() < self.expires_at - _TOKEN_EXPIRY_BUFFER_SECONDS


class BrainClient:
    """Client for Brain's HTTP MCP server.

    Handles OAuth token acquisition/caching and wraps all operations
    the Bee pipeline needs.
    """

    def __init__(self, brain_url: str, client_id: str, client_secret: str) -> None:
        self._url = brain_url.rstrip("/")
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = _TokenCache()
        self._request_id = 0
        self._session = requests.Session()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _acquire_token(self) -> None:
        resp = self._session.post(
            f"{self._url}/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "read write",
            },
            timeout=_DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token.access_token = body["access_token"]
        expires_in = body.get("expires_in", 3600)
        self._token.expires_at = time.time() + expires_in

    def _bearer(self) -> str:
        if not self._token.is_valid():
            self._acquire_token()
        return self._token.access_token

    # ── JSON-RPC transport ────────────────────────────────────────────────────

    def _call(self, operation: str, retries: int = 3, **kwargs: Any) -> Any:
        """Send a tools/call MCP request. Returns the parsed result or raises."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": operation, "arguments": kwargs},
        }
        headers = {
            "Authorization": f"Bearer {self._bearer()}",
            "Content-Type": "application/json",
        }

        for attempt in range(retries):
            try:
                resp = self._session.post(
                    f"{self._url}/mcp",
                    json=payload,
                    headers=headers,
                    timeout=_DEFAULT_TIMEOUT,
                )
            except requests.RequestException as exc:
                if attempt == retries - 1:
                    raise
                wait = 2 ** attempt
                logger.warning("Brain request failed (attempt %d/%d), retrying in %ds: %s", attempt + 1, retries, wait, exc)
                time.sleep(wait)
                continue

            if resp.status_code == 401:
                # Token expired mid-run; refresh and retry once
                self._token = _TokenCache()
                headers["Authorization"] = f"Bearer {self._bearer()}"
                continue

            if resp.status_code in _RETRY_STATUSES and attempt < retries - 1:
                wait = 2 ** attempt
                logger.warning("Brain returned %d (attempt %d/%d), retrying in %ds", resp.status_code, attempt + 1, retries, wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            rpc = resp.json()

            if "error" in rpc:
                raise BrainError(operation, rpc["error"])

            result = rpc.get("result", {})
            # MCP wraps results in content[0].text for tool responses
            if isinstance(result, dict) and result.get("isError"):
                content = result.get("content", [{}])
                msg = content[0].get("text", "unknown error") if content else "unknown error"
                raise BrainError(operation, {"message": msg})

            # Unwrap text content if present
            if isinstance(result, dict) and "content" in result:
                import json as _json
                text = result["content"][0].get("text", "") if result["content"] else ""
                try:
                    return _json.loads(text)
                except Exception:
                    return text

            return result

        raise BrainError(operation, {"message": f"All {retries} retries exhausted"})

    # ── Operations ────────────────────────────────────────────────────────────

    def get_page(self, slug: str) -> dict | None:
        """Return page dict (including frontmatter) or None if not found."""
        try:
            return self._call("get_page", slug=slug)
        except BrainError as exc:
            if exc.is_not_found():
                return None
            raise

    def put_page(self, slug: str, content: str) -> dict:
        """Create or update a page. Content is full markdown with YAML frontmatter."""
        return self._call("put_page", slug=slug, content=content)

    def add_timeline_entry(
        self,
        slug: str,
        date: str,
        summary: str,
        detail: str | None = None,
        source: str | None = None,
    ) -> None:
        """Add a timeline entry to a page. date must be YYYY-MM-DD."""
        kwargs: dict[str, Any] = {"slug": slug, "date": date, "summary": summary}
        if detail:
            kwargs["detail"] = detail
        if source:
            kwargs["source"] = source
        self._call("add_timeline_entry", **kwargs)

    def add_tag(self, slug: str, tag: str) -> None:
        self._call("add_tag", slug=slug, tag=tag)

    def list_pages(
        self,
        tag: str | None = None,
        type: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        kwargs: dict[str, Any] = {"limit": limit}
        if tag:
            kwargs["tag"] = tag
        if type:
            kwargs["type"] = type
        result = self._call("list_pages", **kwargs)
        if isinstance(result, list):
            return result
        return result.get("pages", []) if isinstance(result, dict) else []


class BrainError(Exception):
    def __init__(self, operation: str, error: dict) -> None:
        self.operation = operation
        self.error = error
        super().__init__(f"Brain operation '{operation}' failed: {error}")

    def is_not_found(self) -> bool:
        msg = str(self.error.get("message", "")).lower()
        code = str(self.error.get("code", "")).lower()
        return "not_found" in code or "not found" in msg
