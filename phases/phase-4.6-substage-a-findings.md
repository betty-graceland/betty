# Phase 4.6 Substage (a) — Findings Report

**Date:** 2026-05-26
**Author:** Claude (lead implementation engineer)
**Inputs:** Canonical BRIEF (Phase 0 smoke-test brief), Littlebird's path lock to `travelpec.com-v3`, live MCP probes against `https://travelpec.com/_emdash/api/mcp`
**Closes:** Substage (a) of Phase 4.6 per `phase-4.5-4.6-execution-kickoff.md`. Substage (b) implementation starts after Peter's branch-policy call locks.

---

## Confirmed paths

- **Codebase** (Astro + EmDash on Betty's Mac): `~/Projects/emdash/travelpec-site/`. Astro project, pnpm, Cloudflare Workers, `.agents/` + `.claude/` from prior Ralph Loop, EmDash OAuth patch in `patches/`. Root has its own `AGENTS.md`.
- **Docs / source-data / dossiers** (on Betty's Mac via Google Drive sync as `betty@`): `/Users/betty/My Drive/Betty/emdash-sites/travelpec.com-v3/`. Subfolders: `00-kickoff/`, `01-source-data/`, `02-voice/`, `02-ui-polish/`, `03-runtime/`, `04-post-run/`.
- **Canonical BRIEF**: `…/travelpec.com-v3/00-kickoff/BRIEF.md`. Phase 0 smoke-test brief; full project scope deferred until T01 (create `smoketest` collection) and T02 (append one line to `src/pages/index.astro`) pass.
- **Old iterations**: `…/Betty/emdash-sites/• old-versions/`.
- **Live MCP endpoint**: `https://travelpec.com/_emdash/api/mcp`. MCP Streamable HTTP, JSON-RPC 2.0, SSE response format. Requires `Accept: application/json, text/event-stream` (rejects requests with only one). Bearer token from `~/Desktop/vic-token.txt` on Betty's Mac (50 chars).

---

## Hard rules (from BRIEF, non-negotiable — must encode in Betty's tools and prompts)

1. **Never reveal `is_advertised: true` in any public copy or markup.** Field exists on the Stays collection; currently all 6 existing entries have `is_advertised: 0` including Peter's three Airbnbs. Either the mapping wasn't done yet or it was deliberately neutralized during Ralph Loop testing. Flagging for Peter (see Open items).
2. **Editorial-we only.** No owner-attribution language. Voice doc at `02-voice/03-voice-calibration.md` is the calibration source.
3. **Never push to `main`. Work on `vic-overnight` only.** Peter merges to main manually after review. Implication: `git_push` tool hard-codes branch to `vic-overnight`, not Qwen-controllable.
4. **Each task = ≤2 MCP calls OR 1 atomic file edit.** Aligns naturally with Q1 Decision A — each betty_claw tool call IS one atomic operation, and a higher-order "publish article" task decomposes into `emdash_create_content` + `emdash_publish_content` = 2 atomic tools. The rule is structurally enforced by atomic tool design.

---

## Live MCP surface (45 tools, verified 2026-05-26)

The 2026-05-06 auto-memory inventory ("~38 tools") was directionally right but quantitatively off by 7. Real categories and exact names below. Every tool reports `taskSupport: "forbidden"` — they can't be batched in MCP's native task system (not relevant for Betty's use; we batch via the actor loop).

### content_* (16 tools)

| Tool | Annotation | Notes |
|---|---|---|
| `content_list` | readOnly | required: `collection`; optional: status, limit, cursor, orderBy, order, locale |
| `content_get` | readOnly | required: collection, id (or slug); returns `_rev` token for optimistic concurrency |
| `content_create` | — | required: collection, data; status defaults to 'draft' |
| `content_update` | — | required: collection, id; data is partial; supports `_rev`, `seo`, `bylines`, `publishedAt` |
| `content_delete` | destructive | soft-delete to trash |
| `content_restore` | — | undelete from trash |
| `content_permanent_delete` | destructive | hard delete from trash |
| `content_publish` | — | move to live; optional `publishedAt` for backdating |
| `content_unpublish` | — | revert to draft |
| `content_schedule` | — | future publication |
| `content_unschedule` | — | cancel scheduling |
| `content_compare` | readOnly | live vs draft |
| `content_discard_draft` | destructive | discard draft changes |
| `content_list_trashed` | readOnly | trash listing |
| `content_duplicate` | — | create copy as draft |
| `content_translations` | readOnly | locale variants |

### schema_* (6 tools)

| Tool | Annotation | Notes |
|---|---|---|
| `schema_list_collections` | readOnly | **NOT `schema_list`** — memory was wrong on this name |
| `schema_get_collection` | readOnly | required: slug; returns full field definitions |
| `schema_create_collection` | — | new collection (DDL) |
| `schema_delete_collection` | destructive | drops collection (DDL) |
| `schema_create_field` | — | adds field (column) to collection |
| `schema_delete_field` | destructive | drops field (column) |

### media_* (5 tools)

Media binaries upload out-of-band via signed URL; `media_create` registers metadata.

| Tool | Annotation |
|---|---|
| `media_list` | readOnly |
| `media_create` | — |
| `media_get` | readOnly |
| `media_update` | — |
| `media_delete` | destructive |

### taxonomy_* (6 tools)

| Tool | Annotation |
|---|---|
| `taxonomy_list` | readOnly |
| `taxonomy_list_terms` | readOnly |
| `taxonomy_create_term` | — |
| `taxonomy_update_term` | — |
| `taxonomy_delete_term` | destructive |
| `taxonomy_term_translations` | readOnly |

### menu_* (7 tools)

| Tool | Annotation |
|---|---|
| `menu_list` | readOnly |
| `menu_get` | readOnly |
| `menu_translations` | readOnly |
| `menu_create` | — |
| `menu_update` | — |
| `menu_delete` | destructive |
| `menu_set_items` | — (atomic replace of items) |

### revision_* (2 tools)

| Tool | Annotation |
|---|---|
| `revision_list` | readOnly |
| `revision_restore` | — |

### settings_* (2 tools)

| Tool | Annotation |
|---|---|
| `settings_get` | readOnly |
| `settings_update` | — |

### search (1 tool)

| Tool | Annotation |
|---|---|
| `search` | readOnly — full-text across indexed collections |

---

## Existing EmDash state (verified 2026-05-26)

17 published entries across 4 collections. Memory said "~13"; the actual count is 17. The site is further along than the rebuild assumption suggested.

### Stays (6 entries — collection slug: `stays`)

Field shape observed (formal definition via `schema_get_collection('stays')` pending):
`title, village, persona, bedrooms, capacity, seasonal (year_round | may_to_oct), outbound_url, provider (direct | airbnb), is_advertised (0 | 1), featured_eligible (0 | 1), schema_subtype`

| Title | Village | Provider | Featured |
|---|---|---|---|
| The Bakery Flat | Bloomfield | direct | no |
| The Cider Barn | Waupoos | direct | no |
| Lakeside Loft | Wellington | direct | yes |
| The Huxley | Picton | airbnb (Peter's) | yes |
| Rosé All Day | Picton | airbnb (Peter's) | yes |
| The Blue Roof | Wellington | airbnb (Peter's) | yes |

**All six have `is_advertised: 0`** — including Peter's three Airbnbs, which per the project memory ARE the advertised properties. Either the mapping wasn't completed during Ralph Loop work, or the field is currently inert. Surfacing as Open item #1.

### Villages (6 entries — collection slug: `villages`)

Field shape: `title, tagline, region (west | central | east), description, why_here, getting_around`

| Slug | Region |
|---|---|
| picton | central |
| wellington | west |
| bloomfield | central |
| waupoos | east |
| cherry-valley | central |
| sandbanks-corridor | west |

All six PEC destination + adjacent areas are covered. No remaining village gaps.

### Articles (3 entries — collection slug: `articles`)

Field shape: `title, kind (logistics | monthly_update), excerpt, body, publish_date`

| Title | Kind |
|---|---|
| Sandbanks parking, decoded | logistics |
| Glenora Ferry intel | logistics |
| What's open this month | monthly_update |

These are the three "logistics anchors" from the v1 scope. Topical article expansion (per Littlebird's 2026-05-26 sync, "majority of remaining research becomes topical articles") is the largest remaining content gap.

### Itineraries (2 entries — collection slug: `itineraries`)

Field shape: `title, duration_nights, persona, summary, body`

| Title | Nights |
|---|---|
| Three nights in Picton, no car needed | 3 |
| Off-season weekend in the County | 2 |

Both v1-scope itineraries are present. Remaining research dossiers that map to sequential itineraries are the second content gap (smaller than Articles).

---

## Phase 4.6 betty_claw tool registry — proposed

The narrowest tool surface that supports the Phase 4.6 milestone (autonomous content population + Astro template edits + git push to `vic-overnight`). 20 tools total. Each declares its `risk_class` per Q1 Decision A.

### Filesystem + Git (7 tools)

Astro source side. Operates on `~/Projects/emdash/travelpec-site/` on Betty's Mac.

| betty_claw tool | Wraps | risk_class |
|---|---|---|
| `read_file(path)` | filesystem read | read_only |
| `list_directory(path)` | filesystem readdir | read_only |
| `git_status()` | `git status --porcelain` | read_only |
| `git_diff(path?, staged?)` | `git diff` | read_only |
| `write_file(path, content)` | atomic write (tmpfile + fsync + os.replace) | reversible_write |
| `git_commit_all(message)` | `git add -A && git commit -m …` | reversible_write |
| `git_push()` | `git push origin vic-overnight` (branch hard-coded per Hard Rule 3, not Qwen-controllable) | external_side_effect |

### EmDash MCP reads (6 tools, Judge-skip per Phase 4.5)

| betty_claw tool | Wraps MCP | risk_class |
|---|---|---|
| `emdash_list_collections()` | schema_list_collections | read_only |
| `emdash_get_collection_schema(slug)` | schema_get_collection | read_only |
| `emdash_list_content(collection, status?, limit?, cursor?)` | content_list | read_only |
| `emdash_get_content(collection, id_or_slug)` | content_get | read_only |
| `emdash_list_taxonomies()` | taxonomy_list | read_only |
| `emdash_list_taxonomy_terms(taxonomy)` | taxonomy_list_terms | read_only |

### EmDash MCP writes (4 tools, Judge-gated)

| betty_claw tool | Wraps MCP | risk_class |
|---|---|---|
| `emdash_create_content(collection, data, slug?, status?)` | content_create | reversible_write |
| `emdash_update_content(collection, id, data, rev?)` | content_update | reversible_write |
| `emdash_unpublish_content(collection, id)` | content_unpublish | reversible_write (demotes to draft only) |
| `emdash_create_taxonomy_term(taxonomy, slug, label, parentId?)` | taxonomy_create_term | reversible_write |

### EmDash MCP external side effects (3 tools, Judge-gated with highest rigor)

| betty_claw tool | Wraps MCP | risk_class |
|---|---|---|
| `emdash_publish_content(collection, id)` | content_publish | external_side_effect |
| `emdash_update_content_published(collection, id, data, rev?)` | content_update with status='published' | external_side_effect |
| `emdash_create_content_published(collection, data, slug?)` | content_create with status='published' | external_side_effect |

The last two exist as separate tool registrations because Q1 Decision A says a tool that spans multiple risk classes must split. `content_update` and `content_create` both have an optional `status='published'` parameter that elevates them from reversible to external-side-effect. Splitting into draft-only vs published-status tools forces the risk_class to be constant per tool.

---

## Explicitly NOT in the Phase 4.6 registry

Tools Betty does not need for the travelpec.com milestone — keeping the surface narrow per the BRIEF's atomic-tool discipline.

- **All `*_delete` / `*_permanent_delete` / `*_discard_draft`**: destructive, no Phase 4.6 use case. Betty creates and updates; she doesn't delete.
- **`schema_create_collection`, `schema_delete_collection`, `schema_create_field`, `schema_delete_field`**: collections + schema are already defined. Phase 4.6 populates content; it doesn't reshape the data model.
- **`media_*`**: image placeholders only per operational boundary 4. No media uploads in Phase 4.6.
- **`menu_*`**: navigation already configured.
- **`settings_*`**: site settings already configured.
- **`content_schedule`, `content_unschedule`, `content_duplicate`, `content_translations`, `content_compare`, `content_list_trashed`, `content_restore`**: not needed for the population workflow.
- **`taxonomy_update_term`, `taxonomy_delete_term`, `taxonomy_term_translations`**: existing taxonomies (Region, Best For) are valid for the current scope; Betty creates new terms only if needed.
- **`search`**: Betty has substrate retrieval already via `betty_etl.retrieval`.
- **`revision_list`, `revision_restore`**: human operator concern, not Betty's.

If Phase 4.6 implementation reveals one of these IS needed, we add it under the same Q1 discipline (atomic, declared risk_class, justified by use case). The list above is the Phase 4.6 commitment.

---

## Open items moving into substage (b)

1. **`is_advertised` situation.** All six existing Stays have `is_advertised: 0` including Peter's three Airbnbs. Either the mapping was deferred during Ralph Loop work or the field is currently inert. Needs Peter's call before Betty writes any Stays content — specifically whether new Stays Betty creates for the 35 Airbnb research dossiers should inherit `is_advertised: 0` (default safe) or whether Peter wants a separate manual pass to flip the right entries to 1 post-build.

2. **Branch policy reconciliation.** The execution kickoff says "Betty operates on main directly." The BRIEF says "Never push to main, use vic-overnight." Recommended honoring the BRIEF: `git_push` hard-codes `vic-overnight`, Peter merges to main manually. Needs Peter's explicit call before substage (b) implementation locks the `git_push` tool signature.

3. **Collection schema fetch.** Substage (b) should call `schema_get_collection` for each of the four collections (stays, villages, articles, itineraries) to lock the exact field types and constraints before writing the betty_claw wrappers' validation logic. Cheap probes (4 read_only calls), best done as the first step of substage (b) so the implementation is grounded in real schema rather than the field shapes I inferred from observed data.

4. **Smoke-test framing for first overnight run.** The BRIEF's Phase 0 says T01 (create `smoketest` collection) + T02 (append one line to `src/pages/index.astro`). Strictly following the BRIEF, Betty's first overnight should do exactly those two tasks — proving the full chain (MCP write + filesystem write + commit + push to vic-overnight) on a deliberately tiny slice before scaling to content population. Recommended: first overnight = T01 + T02 only; second overnight expands to one Article + one Stays from research dossiers; third overnight opens to broader population. Sub-decision for Peter before substage (b) closes.

5. **Voice doc integration.** Betty needs to read `02-voice/03-voice-calibration.md` and apply it when writing content body fields. Cleanest integration: load voice calibration into the actor's system prompt context for any turn that involves Stays/Villages/Articles/Itineraries population. Phase 4.6 implementation question.

6. **Airbnb dossier → Stays mapping schema.** Per Littlebird: 35 Airbnb scraped dossiers in `01-source-data/research/airbnb-listings/` become Stays entries. Betty needs a routing convention: which fields in the dossier map to which Stays field, what the canonical `outbound_url` is (the actual Airbnb listing URL is in the dossier, not `https://example.com/...` as the existing placeholder Stays use), and the operational rule that images stay as `<!-- IMAGE: ... -->` placeholders. Phase 4.6 implementation will need a small "dossier parser" before the wrapper tools fire.

---

## What substage (b) does next, after Peter's open-item answers

1. Confirm branch policy (Open item 2).
2. Confirm is_advertised handling for new Stays (Open item 1).
3. Confirm first-overnight scope: smoke tests only or smoke + first content slice (Open item 4).
4. Probe collection schemas with 4 read_only MCP calls (Open item 3).
5. Implement the 20-tool registry in `betty_claw/tools/` with per-tool risk_class + self-tests. Each tool follows the `draft_email` discipline: validate args before generating call_ids, atomic disk writes where applicable, strict schema match between Ollama-facing schema and validator.
6. Add `~$0.10` Phase 4.6 self-test sweep covering all 20 tools, plus an end-to-end synthetic-dry-run test against a sacrificial collection (e.g., a temporary `smoketest` collection per the BRIEF) and a `vic-overnight-test` branch (not the live `vic-overnight`).
7. Journal in BUILD_LOG.md.
8. Hand off to substage (c) — the first overnight run.
