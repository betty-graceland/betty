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


## Stage 3 follow-ups (deferred)

- Retrieval self-test preview length: bump from 100 to 200 chars in
  betty_etl.retrieval._self_test and actor._self_test trace output.
  100 chars hid section-header context that mattered for grounding
  verification.
- Measure warm-turn KV cache effect: run actor self-test twice with
  identical Markdown OS, compare turn-1 vs turn-2 latency. If turn 2
  is materially faster, prefix caching is working. If not, investigate
  before Stage 4 adds Judge round-trips.


## Stage 4 first-task case study (deferred)

- Clean up `~/.openclaw/` — the data directory from the previous
  npm-openclaw install. Contains symlinks pointing to real project
  files (discovered 2026-05-17 during naming collision resolution).
  Cannot safely `rm -rf` without verifying each symlink target.
  Save as Stage 4 demonstration: Betty proposes the cleanup,
  Judge approves only the non-symlink portions, symlinks get
  individual review. Real test of the proposal-then-approve loop
  against a non-trivial filesystem action.


## Phase 4.4 scoping deferrals (locked 2026-05-24, paid down post-win)

Each item below was scoped, considered, and deliberately deferred during
the Phase 4.4 v2 scoping chat to keep Phase 4.5 + 4.6 narrow enough to
ship the travelpec.com autonomous-deploy milestone. References:
`phases/phase-4.4-scoping-kickoff-v2.md`, `phases/phase-4.4-scoping-decisions.md`,
`phases/phase-4.5-4.6-execution-kickoff.md`.

- **Operator Review UI (Q4, Q5).** Dual-button "Approve Action" vs
  "Promote to Rule" governance pattern. Was Phase 4.7. Deferred until
  after travelpec.com deploys — Phase 4.6 uses git revert as the
  rollback mechanism instead of pre-execute human review. Pick back up
  when a second executor or a class of tools genuinely benefits from
  human-in-the-loop.

- **HEARTBEAT.md + launchd autonomous trigger loop (was Phase 4.8).**
  The scheduled-task autonomy layer. Travelpec.com deploy is triggered
  by Peter starting an overnight run, not by Betty's own heartbeat.
  Heartbeat-driven action scoping was specifically flagged in the
  Q1 operational-boundary discussion as the right place for "real-time
  site adjustments based on test data."

- **Generalized universal dispatcher abstraction (Q2).** Pulling the
  "construct envelope, branch on risk_class" logic out of `actor.py`
  into a dedicated dispatcher module. Phase 4.5 keeps it inline; clean
  enough at one executor. Revisit when the same logic needs to live in
  more than one call site.

- **Async / parallel tool execution (Q3).** Phase 4.5 inner loop is
  strictly serial — one tool call per iteration. The travelpec.com
  build naturally serializes (one file write at a time, one commit, one
  push). Async-multiple-tool-call-per-response is a Stage 5+ concern
  per actor.py docstring.

- **`judge_decisions` table fields beyond the minimum (Q6 advanced).**
  Phase 4.5 minimum: call_id, tool_name, risk_class, envelope_json,
  verdict, cost_usd, reasoning, executed_at, execution_result. Future
  fields likely needed: linked_memory_refs, escalation_status,
  operator_review_outcome, derived_rules. Add when the operator UI
  starts consuming the table.

- **Authorization envelope sub-decision (Q1 deferred).** Who populates
  `authorization_refs` — the actor (semantic, from prior conversation)
  or the adapter (mechanical, from a separate authorization store)?
  Genuinely contested. Phase 4.5 ships `authorization_refs: list[str]`
  as a forward-compatible empty-list field with no validation. Semantic
  enforcement is post-win work.

- **Authorization-freshness handling (Littlebird's slow-burn concern).**
  How does an `authorization_refs` value know it's still current? Email
  context from a week ago might no longer be valid authorization for a
  payment action today. Real concern. Not addressable until the
  authorization sub-decision lands.

- **Second-executor stress test (Q9).** Phase 4.9 was originally
  `send_client_email` as the high-rigor Judge-gated executor that
  validates the dispatcher across maximally different risk classes.
  Travelpec.com's tool surface (read_only + reversible_write +
  external_side_effect via `git_push`) exercises all three risk classes
  in one run, so a second executor is no longer architecturally
  necessary — it becomes a "scale to the next site" follow-on.

- **Markdown OS spec completion (Q8).** SOUL.md, IDENTITY.md, TOOLS.md,
  SKILL.md, HEARTBEAT.md. Phase 4.5+4.6 use only AGENTS, USER, MEMORY.
  Add the rest when an executor needs them (e.g., SKILL.md routing
  when the tool surface grows past one-domain-per-tool).

- **`.emlx` ETL, SKILL.md routing, OpenBrain recall in betty_claw.**
  None of these load-bear for travelpec.com deploy. The substrate
  retrieval used by `actor.py` already pulls relevant context via the
  Stage 3 pipeline; the kickoff explicitly notes that spec-completeness
  work in these areas should defer to documented nulls.

