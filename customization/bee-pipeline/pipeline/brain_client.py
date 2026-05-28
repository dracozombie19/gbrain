"""Brain HTTP MCP client — speaks MCP JSON-RPC 2.0 with OAuth client_credentials.

The Brain HTTP server uses the MCP Streamable HTTP transport (spec 2025-03-26).
Clients must send `Accept: application/json, text/event-stream` on every request;
without it the server returns 406. The server may respond with either:
  - Content-Type: application/json  (single synchronous response)
  - Content-Type: text/event-stream (SSE stream; each `data:` line is a JSON-RPC message)
"""

import json as _json
import time
import logging
from typing import Any
from dataclasses import dataclass, field

import requests

logger = logging.getLogger(__name__)

_RETRY_STATUSES = {429, 500, 502, 503, 504}


def _parse_sse_response(text: str) -> dict:
    """Extract the first JSON-RPC message from an SSE response body.

    SSE format:
        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

    Skips comment lines (`:`) and the `[DONE]` sentinel.
    """
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            return _json.loads(data)
        except _json.JSONDecodeError as exc:
            logger.warning("Skipping unparseable SSE data line: %s — %s", data[:120], exc)
    raise ValueError(f"No valid JSON-RPC message found in SSE response: {text[:300]!r}")
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
            # MCP Streamable HTTP transport requires both types in Accept.
            # Without this the server returns 406 Not Acceptable.
            "Accept": "application/json, text/event-stream",
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

            # MCP server may respond with plain JSON or an SSE stream.
            content_type = resp.headers.get("Content-Type", "")
            if "text/event-stream" in content_type:
                rpc = _parse_sse_response(resp.text)
            else:
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

    def remove_tag(self, slug: str, tag: str) -> None:
        self._call("remove_tag", slug=slug, tag=tag)

    def add_link(self, from_slug: str, to_slug: str, link_type: str = "mentions") -> None:
        self._call("add_link", **{"from": from_slug, "to": to_slug, "link_type": link_type})

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

    def search(self, query: str, limit: int = 10) -> list[dict]:
        result = self._call("search", query=query, limit=limit)
        if isinstance(result, list):
            return result
        return result.get("results", []) if isinstance(result, dict) else []


class BrainError(Exception):
    def __init__(self, operation: str, error: dict) -> None:
        self.operation = operation
        self.error = error
        super().__init__(f"Brain operation '{operation}' failed: {error}")

    def is_not_found(self) -> bool:
        msg = str(self.error.get("message", "")).lower()
        code = str(self.error.get("code", "")).lower()
        return "not_found" in code or "not found" in msg


class DryRunBrainClient:
    """Brain client that logs writes instead of executing them.

    Read operations (get_page, list_pages) are proxied to a real BrainClient
    when brain_url is provided, so the resolver can load real aliases. If
    brain_url is empty, reads return empty results and all facts route to
    pending-review (which are then logged, not written).

    Write operations (put_page, add_timeline_entry, add_tag) are logged only.
    """

    def __init__(self, brain_url: str = "", client_id: str = "", client_secret: str = "") -> None:
        self._real: BrainClient | None = None
        if brain_url:
            logger.info("DryRunBrainClient: read ops will proxy to %s", brain_url)
            self._real = BrainClient(brain_url, client_id, client_secret)
        else:
            logger.info("DryRunBrainClient: no BRAIN_URL — reads return empty, all facts → pending-review")

    def get_page(self, slug: str) -> dict | None:
        if self._real:
            return self._real.get_page(slug)
        return None

    def list_pages(self, tag: str | None = None, type: str | None = None, limit: int = 200) -> list[dict]:
        if self._real:
            return self._real.list_pages(tag=tag, type=type, limit=limit)
        return []

    def search(self, query: str, limit: int = 10) -> list[dict]:
        if self._real:
            return self._real.search(query, limit=limit)
        return []

    def put_page(self, slug: str, content: str) -> dict:
        logger.info("[DRY RUN] Would put_page slug=%s\n%s", slug, content[:300])
        return {}

    def add_timeline_entry(
        self,
        slug: str,
        date: str,
        summary: str,
        detail: str | None = None,
        source: str | None = None,
    ) -> None:
        logger.info(
            "[DRY RUN] Would add_timeline_entry slug=%s date=%s source=%s\n  %s",
            slug, date, source, summary,
        )

    def add_tag(self, slug: str, tag: str) -> None:
        logger.info("[DRY RUN] Would add_tag slug=%s tag=%s", slug, tag)

    def remove_tag(self, slug: str, tag: str) -> None:
        logger.info("[DRY RUN] Would remove_tag slug=%s tag=%s", slug, tag)

    def add_link(self, from_slug: str, to_slug: str, link_type: str = "mentions") -> None:
        logger.info("[DRY RUN] Would add_link from=%s to=%s link_type=%s", from_slug, to_slug, link_type)


def create_brain_client(config) -> "BrainClient | DryRunBrainClient":
    """Return the right brain client based on config."""
    if config.dry_run:
        return DryRunBrainClient(
            brain_url=config.brain_url,
            client_id=config.brain_client_id,
            client_secret=config.brain_client_secret,
        )
    return BrainClient(config.brain_url, config.brain_client_id, config.brain_client_secret)
