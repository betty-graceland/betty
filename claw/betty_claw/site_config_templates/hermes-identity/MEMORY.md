# Current state (2026-06-15)

I run on Hermes Agent (Nous Research, MIT-licensed, Feb 2026 release) on Peter's Mac Studio. My cognition layer is Qwen3.5-35B-A3B via Ollama, configured as the "Betty" custom provider in Hermes. I consume the OpenClaw MCP server at `~/code/betty/claw/betty_claw/mcp_server.py` for all site-specific operations.

## Architecture (LOCKED — do not propose alternatives without grounding here)

Brain/Executor split via MCP stdio:

- **Hermes is the Brain.** Planning, memory, Kanban orchestration, scheduling (cron), multi-step reasoning, persistent agent identity. Qwen3.5 drives this.
- **OpenClaw is the Executor.** All site-specific tool calls flow through `~/code/betty/claw/betty_claw/` Python code via the betty MCP server registered in `~/.hermes/config.yaml`.

Pattern B multi-site (locked 2026-05-31):

- Every MCP tool takes `site` as its first parameter. Site config YAML at `~/.betty/sites/{site_id}.yaml` is the only file operators edit to add a new site.
- Active sites: travelpec (in_progress). Lingerieshoppe deferred until travelpec ships fully.
- Site config schema documented in `~/.betty/sites/_README.md`. Canonical templates live in `~/code/betty/claw/betty_claw/site_config_templates/`.

Two-layer voice eval architecture (built Phase 1.7–2.0):

- **Layer 1 (mechanical):** `mcp_betty_validate_against_voice` — regex/substring checks for banned words, banned openers, first-person singular, owner attribution, hallucinated numbers. Pure function, deterministic, near-zero cost. Backstops every Stays write via `_enforce_voice_validation` inside `emdash_create_content_draft`.
- **Layer 2 (semantic):** `mcp_betty_score_editorial_quality` — Claude Haiku LLM-as-judge with rubric prompt. Catches what regex can't (atmospheric invention, distance/capacity inference, marketing voice, generic filler). ~$0.005 per call. Advisory, not blocking, at write time.

## Tool surface (21 MCP tools as of 2026-06-15)

Inspection: `list_sites`, `betty_ping`, `read_file`, `list_directory`, `git_status`, `git_diff`, `emdash_list_collections`, `emdash_get_collection_schema`, `emdash_list_content`, `emdash_get_content`, `emdash_list_taxonomies`, `emdash_list_taxonomy_terms`.

Parsing: `parse_airbnb_dossier`.

Eval: `validate_against_voice`, `score_editorial_quality`.

Write (draft-only, no Judge gate yet): `emdash_create_content_draft`, `emdash_update_content_draft`, `emdash_create_taxonomy_term`.

Composed workflow (canonical Stays processing path): `compose_stays_draft_begin`, `compose_stays_draft_publish`. Use these for Airbnb dossier → Stays draft, not the raw emdash_create_content_draft.

Worklist: `list_pending_airbnb_dossiers` (returns pending + published + skipped dossiers for a site).

## Build state — what's done, in progress, next

DONE (production, end-to-end proven):
- Phase 0 Test A: MCP bridge proven on read tools (2026-05-31).
- Phase 1.0: EmDash read tools through MCP with site param (2026-05-31).
- Phase 1.1: filesystem read tools (read_file, list_directory) (2026-05-31).
- Phase 1.2: git read tools (git_status, git_diff) (2026-05-31).
- Phase 1.5: EmDash write tools draft-only, no Judge yet (2026-05-31).
- Phase 1.7: voice calibration validator with deterministic checks (2026-06-14).
- Phase 1.8: atmospheric phrase blocklist extension (2026-06-14).
- Phase 1.9: composed workflow tool with token-based state (2026-06-14).
- Phase 2 Editorial Scorer: Claude Haiku LLM-as-judge layer (2026-06-14).
- First autonomous travelpec.com listing published 2026-06-14: "The Suite Spot" at https://travelpec.com/stays/the-suite-spot. Peter promoted to live. Real milestone.

IN PROGRESS:
- Phase 2.0 Step 1 (current): stop-after-publish directive in SOUL.md, list_pending_airbnb_dossiers worklist tool. Step 1 is the prerequisite for Step 2.
- Phase 2.0 Step 2 (next): Hermes Kanban dispatch for autonomous run-to-completion. Each pending dossier → one Kanban task → fresh sub-conversation. Context resets between dossiers. This is the architectural unlock for "site a day" autonomy.

PENDING:
- Phase 4.7.0 cleanup: archive obsolete iterations (actor.py path, ~/.openclaw/, old proposals/).
- Phase 1.3+: write_file + git_commit_all + git_push tools with Judge gating. Currently the Judge layer is not in place; these writes happen manually.
- Travelpec voice doc tightening — see Phase 1.7 / 1.8 lessons.
- Broader content beyond Stays: village landings, articles, itineraries. Most are stubs.

## Operational restraints (current as of Phase 2.0)

These are HARD constraints. SOUL.md governs my behavior; this section governs which of my tools I should and shouldn't use.

- Single-task mode is the default. When Peter sends a prompt I do exactly what the prompt asks, nothing more. I do not infer authorization for batch work from a casual mention of multiple items.
- Batch mode requires explicit invocation. Peter must literally write "process the worklist," "process all pending dossiers," "run until done," or an equivalent imperative. Listing pending dossiers is NOT batch invocation; it's a query.
- Stop after successful tool completion. `compose_stays_draft_publish` returning a draft_id is task complete. I report and stop. I do not call additional tools to verify, clean up, or move on.
- Use the composed workflow for Stays. `compose_stays_draft_begin` → rewrite both description AND persona → validate both → score both → `compose_stays_draft_publish` with both fields. Skipping persona rewrite causes publish to fail because the parser auto-extracts persona from raw Airbnb marketing prose.
- No file writes outside MCP tools during Phase 1 architecture. Hermes's native write_file is not authorized for the Astro repo until Phase 1.3 lands the Judge-gated write_file.
- No git push during Phase 1 architecture. Peter pushes manually.
- No EmDash publishing — only drafts. `emdash_publish_content` exists in EmDash but is not exposed through OpenClaw MCP. Peter promotes drafts manually via EmDash UI.
- No image sourcing for Airbnb listings. Image rights are unresolved; cannot pull from Airbnb. Images stay as placeholders in markup; Peter swaps for real assets in a separate pass.

## How I answer architectural questions

I read THIS file (`~/.hermes/memories/MEMORY.md`) before answering anything architectural — what's the current state, what's locked, what's in flight, what's deferred. I do not answer from session memory or from training-data assumptions about how agentic AI projects "usually" work. This file is authoritative for the Betty project's current state and supersedes any earlier guidance.
