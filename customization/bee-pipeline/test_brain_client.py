"""Smoke test for brain_client.py against a running local Brain server.

Usage:
    # 1. Start Brain server in another terminal:
    #    gbrain serve --http --port 9090
    #
    # 2. Register an OAuth client (once):
    #    gbrain auth register-client bee-pipeline --scopes read write
    #    (copy client_id and client_secret printed)
    #
    # 3. Run this script:
    #    BRAIN_CLIENT_ID=... BRAIN_CLIENT_SECRET=... python test_brain_client.py

import os, sys
"""

import json
import os
import sys

# Allow running from the bee-pipeline directory
sys.path.insert(0, os.path.dirname(__file__))

from pipeline.brain_client import BrainClient, BrainError

BRAIN_URL = os.getenv("BRAIN_URL", "http://localhost:9090")
CLIENT_ID = os.getenv("BRAIN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("BRAIN_CLIENT_SECRET", "")

TEST_SLUG = "pending-review/test-brain-client-smoke"


def check(label: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f": {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Set BRAIN_CLIENT_ID and BRAIN_CLIENT_SECRET env vars.")
        print("  gbrain auth register-client bee-pipeline --scopes read write")
        sys.exit(1)

    print(f"Testing Brain client at {BRAIN_URL}")
    print()

    client = BrainClient(BRAIN_URL, CLIENT_ID, CLIENT_SECRET)

    # ── Test 1: Token acquisition ─────────────────────────────────────────────
    print("1. OAuth token acquisition")
    try:
        client._acquire_token()
        check("Token acquired", bool(client._token.access_token))
        check("Token cached", client._token.is_valid())
    except Exception as exc:
        check("Token acquired", False, str(exc))

    # ── Test 2: list_pages ────────────────────────────────────────────────────
    print("\n2. list_pages")
    try:
        pages = client.list_pages(limit=5)
        check("Returns a list", isinstance(pages, list), f"got {type(pages).__name__}")
        print(f"     {len(pages)} page(s) returned")
    except Exception as exc:
        check("list_pages call", False, str(exc))

    # ── Test 3: get_page (non-existent slug) ──────────────────────────────────
    print("\n3. get_page (non-existent slug)")
    try:
        result = client.get_page(TEST_SLUG)
        check("Returns None for missing page", result is None, str(result))
    except Exception as exc:
        check("get_page missing", False, str(exc))

    # ── Test 4: put_page ──────────────────────────────────────────────────────
    print("\n4. put_page (create test page)")
    test_content = (
        "---\n"
        "title: Smoke Test Page\n"
        "type: note\n"
        "tags: [pending-review]\n"
        "bee_conversation_id: \"test-conv-123\"\n"
        "bee_conversation_date: \"2026-05-18\"\n"
        "---\n"
        "**Proposed person**: people/test-person  \n"
        "**Fact**: Enjoys testing.  \n"
        "**Confidence reason**: test run  \n"
        "**Snippet**: > This is a test transcript snippet.\n"
    )
    try:
        result = client.put_page(TEST_SLUG, test_content)
        check("put_page succeeded", True)
    except Exception as exc:
        check("put_page", False, str(exc))

    # ── Test 5: get_page (should now exist) ───────────────────────────────────
    print("\n5. get_page (page just created)")
    try:
        page = client.get_page(TEST_SLUG)
        check("Page exists", page is not None)
        if page:
            fm = page.get("frontmatter", {})
            check("Frontmatter parsed", isinstance(fm, dict), str(fm))
            check("Title correct", fm.get("title") == "Smoke Test Page", str(fm.get("title")))
            check("Type correct", fm.get("type") == "note", str(fm.get("type")))
    except Exception as exc:
        check("get_page existing", False, str(exc))

    # ── Test 6: add_tag ───────────────────────────────────────────────────────
    print("\n6. add_tag")
    try:
        client.add_tag(TEST_SLUG, "pending-review")
        check("add_tag succeeded", True)
    except Exception as exc:
        check("add_tag", False, str(exc))

    # ── Test 7: list_pages with tag filter ────────────────────────────────────
    print("\n7. list_pages tag=pending-review")
    try:
        pages = client.list_pages(tag="pending-review", limit=50)
        slugs = [p.get("slug") for p in pages]
        check("Test page appears in tag results", TEST_SLUG in slugs, f"slugs: {slugs[:5]}")
    except Exception as exc:
        check("list_pages tag filter", False, str(exc))

    # ── Test 8: Resolver alias loading ────────────────────────────────────────
    print("\n8. Resolver alias loading (discovers person/pet pages from Brain)")
    try:
        from pipeline.resolver import Resolver
        resolver = Resolver(client)
        resolver.load()
        print(f"     Loaded {len(resolver._alias_map)} aliases from Brain")
        print(f"     Page titles: {list(resolver._title_map.values())}")
        check("Resolver loaded without error", True)
    except Exception as exc:
        check("Resolver.load()", False, str(exc))

    # ── Cleanup ───────────────────────────────────────────────────────────────
    print("\n9. Cleanup: delete test page")
    print("   (No delete_page op used in pipeline — manual cleanup needed)")
    print(f"   Test page slug: {TEST_SLUG}")

    print("\n" + "─" * 50)
    print("All checks passed. Brain client is working correctly.")
    print()
    print("Next steps:")
    print("  1. Add aliases to family pages:")
    print("     e.g. get_page people/ashley-dowd, add aliases, put_page")
    print("  2. Set .env values for GCS bucket and deploy to Cloud Function")
    print("  3. Obtain Bee API key and update FIELD_* constants in bee_client.py")


if __name__ == "__main__":
    main()
