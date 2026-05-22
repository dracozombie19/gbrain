"""Update alias frontmatter on existing family pages in Brain.

Run this once after the Brain server is running and the bee-pipeline OAuth
client is registered. It adds the aliases the Bee pipeline's resolver needs.

Usage:
    BEE_GBRAIN_CLIENT_ID=... BEE_GBRAIN_CLIENT_SECRET=... python update_aliases.py

    # Dry run (print what would change, don't write):
    BEE_GBRAIN_CLIENT_ID=... BEE_GBRAIN_CLIENT_SECRET=... python update_aliases.py --dry-run
"""

import os
import sys
import re

sys.path.insert(0, os.path.dirname(__file__))

from pipeline.brain_client import BrainClient, BrainError

BRAIN_URL = os.getenv("GBRAIN_URL", "http://localhost:9090")
CLIENT_ID = os.getenv("BEE_GBRAIN_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("BEE_GBRAIN_CLIENT_SECRET", "")

DRY_RUN = "--dry-run" in sys.argv

# Aliases to add for each family page.
# Edit this map to match your actual aliases / pet names.
ALIAS_UPDATES: dict[str, list[str]] = {
    "people/ashley-dowd": ["Ashley", "Mama", "my wife", "Ash"],
    "people/james-dowd": ["James", "James Dowd", "Jamesers", "my son"],
    "people/elowen-dowd": ["Elowen", "Ella", "my daughter"],
    "pets/milo": ["Milo", "my cat", "Milo cat"],
    "pets/mocha": ["Mocha", "my dog", "Mocha dog"],
}


def _set_frontmatter_aliases(content: str, aliases: list[str]) -> str:
    """Replace or add the aliases: line in YAML frontmatter."""
    alias_yaml = "aliases: [" + ", ".join(f'"{a}"' for a in aliases) + "]"

    if "aliases:" in content:
        # Replace existing aliases line
        content = re.sub(r"^aliases:.*$", alias_yaml, content, flags=re.MULTILINE)
    else:
        # Insert before closing --- of frontmatter
        content = re.sub(
            r"^(---\n(?:.*\n)*?)(---\n)",
            lambda m: m.group(1) + alias_yaml + "\n" + m.group(2),
            content,
            flags=re.MULTILINE,
            count=1,
        )
    return content


def main() -> None:
    if not CLIENT_ID or not CLIENT_SECRET:
        print("ERROR: Set BEE_GBRAIN_CLIENT_ID and BEE_GBRAIN_CLIENT_SECRET env vars.")
        sys.exit(1)

    if DRY_RUN:
        print("DRY RUN — no writes will be made.\n")

    client = BrainClient(BRAIN_URL, CLIENT_ID, CLIENT_SECRET)

    for slug, aliases in ALIAS_UPDATES.items():
        print(f"Processing {slug}")
        page = client.get_page(slug)

        if page is None:
            print(f"  SKIP: page does not exist yet (will be created by pipeline when facts are found)")
            continue

        current_aliases = page.get("frontmatter", {}).get("aliases", [])
        if set(current_aliases) == set(aliases):
            print(f"  OK: aliases already up to date: {current_aliases}")
            continue

        print(f"  Current aliases: {current_aliases}")
        print(f"  New aliases:     {aliases}")

        if DRY_RUN:
            print(f"  [dry-run] Would update aliases")
            continue

        # Rebuild the page content with updated aliases
        # get_page should return 'content' or 'compiled_truth'; use whichever is available
        raw_content = page.get("content") or page.get("compiled_truth") or ""
        if not raw_content:
            # Minimal fallback: reconstruct from title + type
            fm = page.get("frontmatter", {})
            title = fm.get("title", slug.split("/")[-1].replace("-", " ").title())
            ptype = fm.get("type", "person")
            raw_content = f"---\ntitle: {title}\ntype: {ptype}\n---\n"

        updated = _set_frontmatter_aliases(raw_content, aliases)

        try:
            client.put_page(slug, updated)
            print(f"  UPDATED: aliases written to Brain")
        except BrainError as exc:
            print(f"  ERROR: {exc}")

    print("\nDone.")
    if not DRY_RUN:
        print("Resolver will pick up new aliases on next pipeline run.")


if __name__ == "__main__":
    main()
