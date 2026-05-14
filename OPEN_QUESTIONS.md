# Betty — Open Questions

Working backlog of deferred architectural decisions. Reviewed at stage
transitions, not mid-stage. Items here are deliberately not-yet-answered;
when one is resolved, the answer migrates to `ARCHITECTURE.md` or a
migration file and the item moves to the "Resolved" section at the bottom
with a date.

---

## Schema & Data Model

- **Memory tables (Stage 3+):** What columns does `memories` need? At minimum:
  provenance (`observed | inferred | generated | user_confirmed`), confidence,
  scope (workspace/project), supersedes link, use policy. Final shape TBD when
  we actually start writing memories.
- **Work records (Stage 8):** What state machines do we wrap email and docs
  into? Probably one base `work_records` table with `record_kind` (lead,
  client_request, campaign_change, content_draft, invoice_issue) and per-kind
  JSONB metadata. Worth designing once we have a real use case in hand.
- **Intent parameters (Stage 8):** Table-per-workspace or single table with
  workspace_id scoping? Lean toward single table with override hierarchy.
- **Review queue (Stage 5):** Priority field? Estimated time-to-review? Tags
  for batch processing? Start minimal; add fields when the manual review pass
  reveals the gaps.

## Operations & Tooling

- **Backup strategy:** `pg_dump` to local disk via launchd cron? Frequency?
  Retention? The `betty-pgdata` Docker volume is local but not automatically
  backed up — single-disk failure means total loss. Need at minimum a daily
  dump and ideally an offsite copy.
- **Secrets management:** Anthropic API key will need to land somewhere.
  Plain `.env` file is fine for Stage 4 (gitignored, local-only). At some
  point worth considering macOS Keychain for stronger isolation.
- **ETL scheduling (Stage 2+):** `launchd` is the Mac-native answer over
  `cron`. Cleaner integration with macOS power management, runs on wake from
  sleep, proper logging via `~/Library/Logs/`. We'll write the plist files
  when the ETL is functional and we want it triggered automatically.
- **Review queue UI framework (Stage 5):** FastAPI + Jinja2 templates +
  Tailwind via CDN is probably the right minimum. No build step, no Node.js,
  no SPA framework. Localhost-only binding. Revisit if the UI complexity
  grows past 3-4 views.
- **Admin user roles:** Single-user system for now. If Amber or anyone else
  ever needs to use Betty, we revisit. Until then, no role/permission layer.

## Architectural

- **Heartbeat schedule:** 30 minutes per the research docs, but is that the
  right cadence for Peter's workflow? Maybe heartbeat for inbox/calendar
  awareness (15-30 min), separate cron for scheduled reports (specific
  times). Decide at Stage 7.
- **Failure triage docs:** The framework calls for a structured incident
  format. Worth setting up the template (`ops/failure-triage/YYYY-MM-DD-N.md`)
  when we have our first real production-style failure.
- **Multiple workspaces:** Architecture supports it (workspace_id columns
  exist) but Stage 1-9 assume single workspace ("betty"). When/if we add
  client-specific workspaces, what's the projection model — separate DBs,
  separate schemas, or single DB with strict workspace_id filtering? Lean
  toward filtering for simplicity.
- **Tailscale integration:** Mac Studio runs Betty 24/7 — at some point
  remote access from laptop or phone becomes useful. Tailscale to the
  review queue UI specifically (not the gateway) is the right pattern.
  Defer until there's a real need.

## Performance & Cost

- **Anthropic API daily cap:** What's the right hard ceiling? $5/day?
  $10/day? Should be high enough to never block legitimate work, low
  enough to catch a runaway loop within hours not weeks.
- **Postgres tuning:** Default Postgres config is fine for current scale.
  When chunk count exceeds 100k, revisit `shared_buffers`, `work_mem`,
  and `effective_cache_size` for HNSW query performance.
- **Qwen quantization revisit:** Started with 4-bit. If actor latency or
  JSON adherence becomes a bottleneck, evaluate higher-precision quants
  (q5_K_M, q6) within the 32GB budget.

## Resolved (Date / Migrated To)

_None yet — this section will grow as questions get answered._

