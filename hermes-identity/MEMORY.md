# Current focus (2026-05-26)

Pivoted to Hermes Agent (Nous Research, MIT-licensed, Feb 2026 release) as the autonomous runtime. Qwen3.5-35B-A3B via Ollama is the cognition layer (custom provider "Betty" → http://localhost:11434/v1). Local terminal backend. 90 bundled skills synced. SOUL + USER + MEMORY just populated from the prior `claw/betty_claw/betty_os/` content.

The 30 days of custom betty_claw architecture (`~/code/betty/`) is the integration substrate, not the runtime: per-tool risk_class registry, Judge layer (Opus-pre-tool-call), envelope minimum contract, EmDash MCP wrappers, Airbnb dossier parser, judge_decisions audit trail. These become Hermes skills (or stay as Python modules called from skills) over the next phase. Custom-built actor.py and smoke_test.py runners are obsolete now that Hermes provides multi-tool chains natively.

## What landed before the pivot

- **Phase 4.4 scoping**: closed with Q1 Decisions A+B (per-tool constant risk_class, adapter-populated). Q7 locked on travelpec.com autonomous deploy as the launch milestone.
- **Phase 4.5**: Envelope contract, `judge_decisions` migration, read_only Judge-skip, forward-compat `authorization_refs`. Self-tests pass.
- **Phase 4.6 substages (a)(b)(c)**: 21-tool registry implemented. Smoke test (T01 create `smoketest` collection + T02 write marker file + T03 publish) ran green on 2026-05-26, $0.0563 Anthropic.
- **Phase 4.6.1**: Airbnb dossier parser validated end-to-end against a real dossier (the Parsonage). Clean Stays dict produced from YAML frontmatter + scraped body.
- **Phase 4.6.2**: Single-dossier chain (parse → create_draft → publish) ran successfully. The Parsonage now exists in EmDash and on travelpec.com.
- **Phase 4.6.3** (Claude-Code-mode, not Betty-autonomous): Fixed broken Stays Astro templates. `pages/stays/[slug].astro` and `pages/stays/index.astro` written using `getEmDashEntry`. Site now renders all 17 prior Stays/Villages/Articles/Itineraries entries that had been invisible since May because templates were never wired correctly.

## Site state — travelpec.com

- 7 EmDash collections: stays (7 entries), villages (6), articles (3), itineraries (2), pages (skeleton sitemap), posts (default, empty), section (default, empty).
- All published content now renders on the live site. The Parsonage entry is visible at `/stays/3-bed-pec-home-loads-of-style-12-hr-to-sandbanks` (the slug is the auto-generated keyword-stuffed Airbnb title — Phase 4.6.4 cleanup target).
- Deploys are MANUAL (`pnpm run deploy` from `~/Projects/emdash/travelpec-site/`). No GitHub auto-deploy on this repo. CLOUDFLARE_API_TOKEN in `~/Projects/emdash/travelpec-site/.env` for non-interactive wrangler.
- Hard rules from the v3 BRIEF still apply: no `is_advertised: true` in public copy, editorial-we only, no push to main (use vic-overnight), each task ≤2 MCP calls OR 1 atomic file edit.

## Path artifacts

- `~/code/betty/` — Phase 4.3–4.6.3 work. Contracts, tools, judge, smoke_test, single_dossier_test. Some pieces become Hermes skills; some get retired.
- `~/Projects/emdash/travelpec-site/` — Astro source for travelpec.com (the deploy target). Local git repo, Cloudflare Workers deploy via wrangler.
- `~/travelpec-com/` — local source-data tree for travelpec.com. The 35 Airbnb dossiers live at `~/travelpec-com/01-source-data/research/airbnb-listings/`. The article research lives at `~/travelpec-com/01-source-data/research/pages/`.
- `~/My Drive/Betty/emdash-sites/travelpec.com-v3/` — Google Drive synced metadata (BRIEF, voice calibration, UI polish notes, architecture decisions). Voice doc at `02-voice/03-voice-calibration.md`.

## Open threads

- **Phase 4.6.4** — Clean slugs. The dossier parser should derive the slug from the property's actual name in the body (e.g., "The Parsonage" → `the-parsonage`) instead of letting EmDash auto-generate from the keyword-stuffed Airbnb title. Pending.
- **Stays needs descriptions.** All existing entries (including the Parsonage) have empty `description` fields. Either Peter writes them in admin UI or a Hermes skill does a pass to add them.
- **No images on Stays detail pages.** The Stays schema has no `image` or `gallery` field. Adding one is a schema-extension job. Peter approved using Airbnb images earlier — that decision is live and needs a follow-on phase to implement.
- **35 Airbnb dossiers still to publish.** Only the Parsonage is live. The other 34 are in `~/travelpec-com/01-source-data/research/airbnb-listings/` waiting for the autonomous chain to process them.
- **Article and itinerary research.** Article dossier parser is a separate workstream (different YAML frontmatter shape — `url_path`, `primary_keyword`, `page_type`).
- **Telegram messaging gateway**: Peter had it set up under the prior OpenClaw Betty. The Hermes setup wizard skipped past it without prompting. Re-setup via `hermes setup gateway` is pending.

## What I don't have yet on Hermes

- The custom betty_claw tools (parse_airbnb_dossier, emdash_*, file/git wrappers) are not yet ported as skills. Until they are, I drive content workflows manually via the `terminal` tool, calling Python scripts in `~/code/betty/claw/`.
- The Opus-as-Judge content-judgment layer is not wired into Hermes's tool execution path. Whether it should be — given Hermes has its own approvals + sandboxing — is a Phase 5.0 architectural question.
- The `~/.hermes/skills/travelpec/` skill directory does not exist yet. First custom skill to author: `publish-airbnb-stay`, modeled on `single_dossier_test.py` but driven by Hermes's native multi-tool chains.
