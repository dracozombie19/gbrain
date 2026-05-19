---
name: bee-triage
description: Triage pending-review pages created by the Bee → Brain pipeline. Confirms or corrects low-confidence fact attributions, creates stub pages for newly discovered entities (people, pets, companies, technologies, places, media), and updates alias mappings.
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
  - companies/*
  - tech/*
  - places/*
  - media/*
---

# Bee Triage Skill

Use this skill to work through facts extracted from Bee transcripts that couldn't be automatically attributed with high confidence. Each pending-review page contains a single extracted fact that needs a human decision.

## When to Use This Skill

- After the Bee pipeline has run and you want to review what needs attention
- When `list_pages tag=pending-review` returns results
- When you want to update alias mappings for an entity

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
Entity type: [entity_type]
Proposed entity: [proposed entity slug or "unknown"]
Confidence reason: [attribution_reasoning]
Snippet: [transcript_snippet]
Conversation date: [bee_conversation_date]
```

Ask the user: **Confirm, reassign, discard, or new entity?**

---

## Step 3: Act on the User's Decision

### CONFIRM — fact is correct, proposed entity is right

1. `add_timeline_entry slug=<entity-slug> date=<bee_conversation_date> summary=<fact> source=bee:<bee_conversation_id>`
2. `delete_page slug=<pending-review-slug>`

If the entity page doesn't exist yet, create a stub first (see Step 5).

### REASSIGN — fact is correct, but wrong entity

1. Ask user: which entity does this fact belong to?
2. `add_timeline_entry` on the correct entity page
3. `delete_page slug=<pending-review-slug>`

### DISCARD — fact is wrong, irrelevant, or not extractable

1. `delete_page slug=<pending-review-slug>`
2. No timeline entry written.

### NEW ENTITY — this entity is not yet in Brain

Go to Step 5 (New Entity Discovery), then return here to confirm the timeline entry.

---

## Step 4: Alias Update

After each triage decision, check:
- Did the user recognize this entity by a name/reference not in their alias list?
- Is there a new reference pattern worth adding? (e.g., "my boss" → `people/john-smith`, "the dog" → `pets/buddy`)

If yes, update the alias:
1. `get_page slug=<entity-slug>`
2. Read current frontmatter `aliases` list
3. Add the new alias to the list
4. `put_page slug=<entity-slug>` with updated frontmatter

Example for a person:
```yaml
---
title: Ashley Dowd
type: person
aliases: ["Ashley", "Mama", "my wife", "babe"]
---
```

Example for a place:
```yaml
---
title: Greenfield Park
type: note
tags: [place]
aliases: ["Greenfield Park", "the park with the merry-go-round", "that park near the school"]
---
```

Only add aliases that are stable references, not one-time phrasing.

---

## Step 5: New Entity Discovery

When a pending-review page refers to an entity genuinely not in Brain, create a stub based on `entity_type`:

### person

1. Ask: *"What's this person's full name and relationship to you?"*
2. Slug: `people/firstname-lastname`
3. Create stub:
   ```
   put_page slug=people/firstname-lastname content="""
   ---
   title: Firstname Lastname
   type: person
   aliases: ["Name as spoken"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```
4. Add tags as appropriate (e.g., `add_tag slug=... tag=family`)

### pet

1. Ask: *"What's this pet's name and species?"*
2. Slug: `pets/name`
3. Create stub:
   ```
   put_page slug=pets/name content="""
   ---
   title: Name
   type: note
   tags: [pet]
   aliases: ["Name as spoken", "the dog"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```

### company

1. Ask: *"Is this the right company name?"* (confirm spelling/full name)
2. Slug: `companies/company-name`
3. Create stub:
   ```
   put_page slug=companies/company-name content="""
   ---
   title: Company Name
   type: company
   aliases: ["Company Name", "short name if any"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```

### technology

1. Ask: *"Is this the right tool/platform name?"* (confirm spelling)
2. Slug: `tech/tool-name`
3. Create stub:
   ```
   put_page slug=tech/tool-name content="""
   ---
   title: Tool Name
   type: concept
   tags: [technology]
   aliases: ["Tool Name", "short name if any"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```

### place

1. Ask: *"Does this place have a proper name, or should we use a descriptive phrase?"*
2. Slug: `places/place-name` (or `places/the-coffee-shop-on-5th` for descriptive)
3. Create stub:
   ```
   put_page slug=places/place-name content="""
   ---
   title: Place Name
   type: note
   tags: [place]
   aliases: ["Place Name", "how you refer to it in conversation"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```
   Good aliases for places are the natural phrases you'd use when speaking: "the park with the merry-go-round", "that sushi place", "the coffee shop on Oak". These make future extractions resolve automatically.

### media

1. Ask: *"Is this the right title? What format is it — book, podcast, film, show?"*
2. Slug: `media/title-slug` (e.g. `media/atomic-habits`, `media/the-wire`)
3. Create stub:
   ```
   put_page slug=media/title-slug content="""
   ---
   title: Title
   type: media
   tags: [book]
   aliases: ["Title", "how you'd refer to it in conversation"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```
   Replace `tags: [book]` with the appropriate format tag: `podcast`, `film`, `show`, or `article`.
   Good aliases are natural spoken references: "that Clear book", "the poker one", "the Lex episode about X". These make future extractions resolve automatically.

After creating the stub (any type):

1. `add_timeline_entry slug=<new-slug> date=<date> summary=<fact> source=bee:<conv_id>`
2. `delete_page slug=<pending-review-slug>`
3. Note the new slug — future extractions for this entity will resolve correctly once aliases are set.

---

## Step 6: Completion

After working through all pending items, report:
- N facts confirmed and written to Brain
- N facts discarded
- N new entities created (broken down by type: N people, N companies, N places, etc.)
- N alias updates made

If any items couldn't be resolved (ambiguous even with context), leave them in pending-review for a future session.

---

## Conventions

- Always use `bee:<conversation_id>` as the `source` on timeline entries from this pipeline
- The `bee_conversation_id` frontmatter field on pending-review pages contains the conversation ID
- Pending-review page slugs follow the pattern: `pending-review/YYYY-MM-DD-{conv_suffix}-{n}`
- The `entity_type` field on the pending-review page tells you what kind of entity is being proposed
- Page type conventions: person → `type: person`, pet → `type: note` + `tags: [pet]`, company → `type: company`, technology → `type: concept` + `tags: [technology]`, place → `type: note` + `tags: [place]`, media → `type: media` + `tags: [book|podcast|film|show|article]`
- The dream cycle handles compiling timeline entries into compiled truth — do not edit compiled truth sections directly
