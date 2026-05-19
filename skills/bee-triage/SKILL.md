---
name: bee-triage
description: Triage pending-review pages created by the Bee → Brain pipeline. Confirms or corrects low-confidence fact attributions, creates person pages for newly discovered people, and updates alias mappings.
triggers:
  - triage bee
  - review pending extractions
  - pending review
  - bee triage
  - review bee facts
writes_pages:
  - pending-review/*
  - people/*
  - pets/*
---

# Bee Triage Skill

Use this skill to work through facts extracted from Bee transcripts that couldn't be automatically attributed with high confidence. Each pending-review page contains a single extracted fact that needs a human decision.

## When to Use This Skill

- After the Bee pipeline has run and you want to review what needs attention
- When `list_pages tag=pending-review` returns results
- When you want to update alias mappings for a person

---

## Step 1: Surface Pending Items

```
list_pages tag=pending-review limit=50
```

If no results: nothing to triage. Done.

Report the count to the user before proceeding: *"There are N items pending review."*

---

## Step 2: Present Each Item

For each pending-review page, call `get_page slug=<slug>` and present:

```
Fact: [fact]
Proposed person: [proposed person slug or "unknown"]
Confidence reason: [attribution_reasoning]
Snippet: [transcript_snippet]
Conversation date: [bee_conversation_date]
```

Ask the user: **Confirm, reassign, discard, or new person?**

---

## Step 3: Act on the User's Decision

### CONFIRM — fact is correct, proposed person is right

1. `add_timeline_entry slug=<person-slug> date=<bee_conversation_date> summary=<fact> source=bee:<bee_conversation_id>`
2. `delete_page slug=<pending-review-slug>`

If the person page doesn't exist yet, create a stub first (see Step 5).

### REASSIGN — fact is correct, but wrong person

1. Ask user: which person does this fact belong to?
2. `add_timeline_entry` on the correct person page
3. `delete_page slug=<pending-review-slug>`

### DISCARD — fact is wrong, irrelevant, or not extractable

1. `delete_page slug=<pending-review-slug>`
2. No timeline entry written.

### NEW PERSON — this is someone not yet in Brain

Go to Step 5 (New Person Discovery), then return here to confirm the timeline entry.

---

## Step 4: Alias Update

After each triage decision, check:
- Did the user recognize this person by a name/reference not in their alias list?
- Is there a new reference pattern worth adding? (e.g., "my boss" → `people/john-smith`)

If yes, update the alias:
1. `get_page slug=<person-slug>`
2. Read current frontmatter `aliases` list
3. Add the new alias to the list
4. `put_page slug=<person-slug>` with updated frontmatter

Example updated frontmatter:
```yaml
---
title: Ashley Dowd
type: person
aliases: ["Ashley", "Mama", "my wife", "babe"]
---
```

Only add aliases that are stable references, not one-time phrasing.

---

## Step 5: New Person Discovery

When a pending-review page refers to someone genuinely not in Brain:

1. Ask the user: *"What's this person's full name and relationship to you?"*
2. Determine the correct slug:
   - People: `people/firstname-lastname`
   - Pets: `pets/name`
3. Create the stub page:
   ```
   put_page slug=people/firstname-lastname content="""
   ---
   title: Firstname Lastname
   type: person
   aliases: ["Name as spoken", "other aliases"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD. Dream cycle will enrich.
   """
   ```
4. Add appropriate tags:
   - `add_tag slug=people/firstname-lastname tag=family` (if family member)
   - Other relevant tags based on relationship
5. Write the timeline entry:
   ```
   add_timeline_entry slug=people/firstname-lastname date=<date> summary=<fact> source=bee:<conv_id>
   ```
6. Delete the pending-review page:
   ```
   delete_page slug=<pending-review-slug>
   ```
7. Note the new slug — future extractions for this person will now resolve correctly once their aliases are set.

---

## Step 6: Completion

After working through all pending items, report:
- N facts confirmed and written to Brain
- N facts discarded
- N new people created
- N alias updates made

If any items couldn't be resolved (ambiguous even with context), leave them in pending-review for a future session.

---

## Conventions

- Always use `bee:<conversation_id>` as the `source` on timeline entries from this pipeline
- The `bee_conversation_id` frontmatter field on pending-review pages contains the conversation ID
- Pending-review page slugs follow the pattern: `pending-review/YYYY-MM-DD-{conv_suffix}-{n}`
- The dream cycle handles compiling timeline entries into compiled truth — do not edit compiled truth sections directly
