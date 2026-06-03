---
name: bee-triage
description: Triage pages created by the Bee → Brain pipeline. Three passes: (1) entity fact attribution for pending-review pages, (2) memory approval, (3) action item confirmation. Creates entity stubs and updates alias mappings as needed.
triggers:
  - triage bee
  - review pending extractions
  - pending review
  - bee triage
  - review bee facts
  - review bee memories
  - review bee tasks
writes_pages:
  - pending-review/*
  - memories/*
  - tasks/*
  - people/*
  - pets/*
  - companies/*
  - tech/*
  - places/*
  - media/*
  - projects/*
---

# Bee Triage Skill

Three passes over Bee pipeline output, each with a different goal:

| Pass | Queue | Goal |
|---|---|---|
| **1. Entity facts** | `tag=pending-review` | Confirm or correct low-confidence fact attributions |
| **2. Memories** | `tag=bee-unreviewed` + `tag=memory` | Approve or discard episodic memory pages |
| **3. Action items** | `tag=bee-unreviewed` + `tag=task` | Confirm or promote task pages |

Start by surfacing all three queues, then work through them in order (or let the user choose which to tackle).

---

## Step 1: Surface All Queues

```
list_pages tag=pending-review limit=50
list_pages tag=bee-unreviewed limit=50
```

From the `bee-unreviewed` results, separate memories (have `memory` tag) from action items (have `task` tag) by checking each page's `tags` field.

Report counts before proceeding:
*"There are N entity facts pending attribution, M memories awaiting approval, and K action items to confirm."*

If all queues are empty, nothing to triage — done.

---

## Pass 1: Entity Facts (pending-review)

Skip this pass if `list_pages tag=pending-review` is empty.

### Present each item

For each pending-review page, call `get_page slug=<slug>` and present:

```
Fact: [fact]
Entity type: [entity_type]
Proposed entity: [proposed entity slug or "unknown"]
Confidence reason: [attribution_reasoning]
Snippet: [transcript_snippet]
Conversation date: [bee_conversation_date]
```

If more context is needed to make a decision, retrieve the original transcript:

```
get_raw_data slug=archive/bee-conversations/<bee_conversation_id> source=bee
```

The result contains the full `transcript`, per-utterance breakdown, and Bee's auto-generated summary.

Ask the user: **Confirm, reassign, discard, or new entity?**

### Act on the decision

**CONFIRM** — fact is correct, proposed entity is right

1. `add_timeline_entry slug=<entity-slug> date=<bee_conversation_date> summary=<fact> source=bee:<bee_conversation_id>`
2. `delete_page slug=<pending-review-slug>`

If the entity page doesn't exist yet, create a stub first (see Step 5: New Entity Stubs).

**REASSIGN** — fact is correct, but wrong entity

1. Ask user: which entity does this fact belong to?
2. `add_timeline_entry` on the correct entity page
3. `delete_page slug=<pending-review-slug>`

**DISCARD** — fact is wrong, irrelevant, or not extractable

1. `delete_page slug=<pending-review-slug>`

**NEW ENTITY** — this entity is not yet in Brain

Go to Step 5 (New Entity Stubs), then return here to confirm the timeline entry.

### Alias updates (after each decision)

After each triage decision, check: did the user recognize this entity by a name not in the alias list? If yes:

1. `get_page slug=<entity-slug>`
2. Add the new alias to the frontmatter `aliases` list
3. `put_page slug=<entity-slug>` with updated frontmatter

Only add stable references, not one-time phrasing.

---

## Pass 2: Memories (bee-unreviewed + memory tag)

Skip this pass if no `bee-unreviewed` pages have the `memory` tag.

Memory pages capture episodic moments — notable firsts, funny observations, developmental milestones, and anything worth remembering that isn't a durable entity fact. They live at `memories/YYYY-MM-DD-{slug}`.

### Present each memory

For each memory page, call `get_page slug=<slug>` and present:

```
Memory: [fact / title]
Date: [memory_date]
People & places: [linked entities from body, if any]
Snippet: [source snippet]
```

Ask the user: **Approve, edit, add links, or discard?**

### Act on the decision

**APPROVE** — memory is accurate and well-written

1. `remove_tag slug=<memory-slug> tag=bee-unreviewed`

The memory stays in Brain, tagged `memory` and `bee-extracted`.

**EDIT** — memory needs correction (wrong detail, awkward wording, missing context)

1. `get_page slug=<memory-slug>` to read current content
2. Construct the corrected content (keep frontmatter structure, update body)
3. `put_page slug=<memory-slug> content=<corrected content>`
4. `remove_tag slug=<memory-slug> tag=bee-unreviewed`

**ADD LINKS** — the memory involves people or places not automatically linked

1. Ask which entities to link: *"Who or what else should this memory reference?"*
2. Resolve each name to a slug (check existing pages or ask user to confirm)
3. `add_link from=<memory-slug> to=<entity-slug> link_type=mentions` for each
4. Optionally update the body to embed `[Name](slug)` wikilinks
5. Then approve or edit as above

**DISCARD** — memory is incorrect, duplicate, or not worth keeping

1. `delete_page slug=<memory-slug>`

---

## Pass 3: Action Items (bee-unreviewed + task tag)

Skip this pass if no `bee-unreviewed` pages have the `task` tag.

Action item pages capture tasks mentioned in conversation — things to do, follow up on, or remember to handle. They live at `tasks/YYYY-MM-DD-{slug}` with `status: open`.

### Present each action item

For each task page, call `get_page slug=<slug>` and present:

```
Task: [fact / title]
Date extracted: [created_date]
Due date: [due_date, if any]
People involved: [linked entities, if any]
Snippet: [source snippet]
```

Ask the user: **Confirm, promote to task list, edit, or discard?**

### Act on the decision

**CONFIRM** — task is real, keep it as a Brain page

1. `remove_tag slug=<task-slug> tag=bee-unreviewed`

The task stays at `tasks/YYYY-MM-DD-{slug}`, tagged `task` and `bee-extracted`.

**PROMOTE** — add to the consolidated task list at `ops/tasks.md`

1. `get_page slug=ops/tasks.md` (create if it doesn't exist)
2. Append a checkbox entry under the appropriate section:
   ```
   - [ ] [task description] *(from Bee, [date])*
   ```
   If a due date exists:
   ```
   - [ ] [task description] — due [due_date] *(from Bee, [date])*
   ```
3. `put_page slug=ops/tasks.md content=<updated content>`
4. `delete_page slug=<task-slug>` — the ops/tasks.md entry is now the canonical record

**EDIT** — task needs correction (wrong description, missing due date, wrong assignee)

1. `get_page slug=<task-slug>` to read current content
2. Construct corrected content (update frontmatter `due_date` if needed, fix body text)
3. `put_page slug=<task-slug> content=<corrected content>`
4. `remove_tag slug=<task-slug> tag=bee-unreviewed`

**DISCARD** — task is not real, already done, or not worth tracking

1. `delete_page slug=<task-slug>`

---

## Step 5: New Entity Stubs

When a pending-review page (Pass 1) refers to an entity not yet in Brain, create a stub:

### person

1. Ask: *"What's this person's full name and relationship to you?"*
2. Slug: `people/firstname-lastname`
3. ```
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
3. ```
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

1. Ask: *"Is this the right company name?"*
2. Slug: `companies/company-name`
3. ```
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

1. Ask: *"Is this the right tool/platform name?"*
2. Slug: `tech/tool-name`
3. ```
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
3. ```
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
   Good aliases: "the park with the merry-go-round", "that sushi place", "the coffee shop on Oak".

### media

1. Ask: *"Is this the right title? What format — book, podcast, film, show?"*
2. Slug: `media/title-slug` (e.g. `media/atomic-habits`)
3. ```
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

### project

1. Ask: *"Is this the right project name? Work or personal?"*
2. Slug: `projects/project-name`
3. ```
   put_page slug=projects/project-name content="""
   ---
   title: Project Name
   type: project
   aliases: ["Project Name", "how you refer to it in conversation"]
   ---
   Stub page created via Bee triage on YYYY-MM-DD.
   """
   ```

After creating a stub (any type):

1. `add_timeline_entry slug=<new-slug> date=<date> summary=<fact> source=bee:<conv_id>`
2. `delete_page slug=<pending-review-slug>`

---

## Step 6: Completion

After working through all queues, report:

**Entity facts (Pass 1):**
- N facts confirmed to existing entities
- N facts reassigned
- N facts discarded
- N new entity stubs created (N people, N places, etc.)
- N alias updates made

**Memories (Pass 2):**
- N memories approved
- N memories edited then approved
- N memories discarded

**Action items (Pass 3):**
- N action items confirmed as Brain pages
- N action items promoted to ops/tasks.md
- N action items discarded

If any items couldn't be resolved (ambiguous even with context), leave them tagged for a future session.

---

## Conventions

- **Entity fact source**: always `bee:<conversation_id>` on timeline entries
- **Pending-review slugs**: `pending-review/YYYY-MM-DD-{conv_suffix}-{n}`
- **Memory slugs**: `memories/YYYY-MM-DD-{subject-slug}`
- **Task slugs**: `tasks/YYYY-MM-DD-{subject-slug}`
- **Page type conventions**: person → `type: person`, pet → `type: note` + `tags: [pet]`, company → `type: company`, technology → `type: concept` + `tags: [technology]`, place → `type: note` + `tags: [place]`, media → `type: media`, project → `type: project`
- **bee-unreviewed tag**: removed by `remove_tag` on approval; indicates pipeline-generated content that hasn't had human eyes on it yet
- The dream cycle handles compiling timeline entries into compiled truth — do not edit compiled truth sections directly
