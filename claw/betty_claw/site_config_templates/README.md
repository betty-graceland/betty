# ~/.betty/sites/ — site config registry

Every website Betty operates on is defined by a YAML file in this directory.
Files are named `{site_id}.yaml`. The site_id is the short slug Betty uses
to identify the site when calling tools (e.g., `parse_airbnb_dossier(site="travelpec", path=...)`).

Files starting with underscore (like this `_README.md` or a future `_template.yaml`)
are ignored by the site discovery logic.

## Adding a new site

The Pattern B design lock means adding a new site requires **only** editing
files in this directory plus the EmDash + Astro infrastructure for the site
itself. No Python edits, no Hermes config edits, no environment plumbing.

The workflow:

1. Set up the new site's external infrastructure (EmDash project, Cloudflare,
   GitHub repo, Astro project, voice doc). This is the longest step.
2. Copy `travelpec.yaml` to `{newsite_id}.yaml` and edit the values.
3. Add the EmDash token to `~/.hermes/.env` under the `token_env` name you
   declared in the new YAML (e.g., `LINGERIESHOPPE_EMDASH_TOKEN`).
4. Restart Hermes. Betty picks up the new site automatically via `list_sites()`.

## Required fields

A site config must have these top-level keys:

- `id` — short slug, must match the filename without `.yaml`
- `domain` — the public-facing domain (e.g., `travelpec.com`)
- `status` — one of `in_progress`, `live`, `archived`
- `paths` — file-system roots for the site's source/docs/research
- `emdash` — MCP URL + token env var name
- `git` — repo URL + branch policy
- `collections` — schema for each EmDash collection (Betty validates content writes against this)
- `hard_rules` — list of strings from the site BRIEF that constrain content
- `voice_doc_path` — relative path within docs to the voice calibration markdown
- `parsers` — site-specific parser configs (e.g., Airbnb dossier mapping)

See `travelpec.yaml` for a complete example.

## Tools that use site config

Every MCP tool exposed by OpenClaw takes `site` as its first parameter:

- `betty_ping(site)` — health check
- `list_sites()` — returns all available sites (no site param)
- `parse_airbnb_dossier(site, path)` — uses site's research path allow-list + parser fixed_fields
- (Phase 1+) `emdash_create_content_draft(site, collection, data)` — uses site's EmDash URL/token + collection schema
- (Phase 1+) `write_file(site, path, content)` — uses site's astro path as write-root
- (Phase 1+) `git_push(site)` — uses site's working_branch as hardcoded refspec

The site param forces Betty to think about which site she's working on every
tool call. This prevents cross-site bleed: she can't accidentally publish a
travelpec entry to lingerieshoppe's EmDash because the tool resolution
chain uses the site config for URL + token.

## Why YAML, not JSON or TOML

Hard rules from BRIEFs are multi-line prose. Collection field definitions
benefit from inline comments. YAML supports both naturally. Operators
hand-edit these files; readability matters more than parse speed.

## Backups and versioning

The active configs are not git-tracked (they may contain references to
local paths and env var names that vary per operator machine). However,
a template version of each site config IS tracked in the betty repo at
`claw/betty_claw/site_config_templates/{site_id}.yaml` so the canonical
schema for each site is reviewable in code.
