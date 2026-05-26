# Phase 4.5 + 4.6 Execution Kickoff — Autonomous Deploy Milestone

**Date:** 2026-05-24
**Boundary commit:** `17ef5a2` (v2 scoping kickoff) + this kickoff
**Scoping reference:** `phases/phase-4.4-scoping-kickoff-v2.md` + `phase-4.4-scoping-decisions.md`
**Author:** Claude (lead implementation engineer)
**Approval:** Peter (architect), Littlebird (peer reviewer)

---

## What we're shipping

Betty reads the existing travelpec.com content research, finishes the in-progress Emdash/Astro template, writes the Astro source files, commits, and pushes. Cloudflare's existing CI/CD pipeline deploys the site live. The whole sequence runs autonomously overnight. Peter wakes up to a deployed travelpec.com.

That is the milestone. One sentence. Everything in this kickoff is in service of it. If a sub-task does not load-bear for that sentence, it does not belong in Phase 4.5 or 4.6.

This is the "ship the win" pivot from the v2 kickoff's six-phase plan. Phases 4.7 (Operator UI), 4.8 (HEARTBEAT autonomy), 4.9 (second executor), the authorization-freshness gap, and the dual-button governance pattern are all deferred until after this milestone lands.

---

## Why this is the right milestone

**Business:** travelpec.com is real work Peter is paying for in time today. A deployed site is the first concrete output that justifies Betty's existence relative to a plain Claude subscription.

**Architectural:** A single overnight run exercises the entire Q1 contract — read-only Judge-skip (file reads, git reads), reversible_write Judge gate (write_file, git_commit_all), and external_side_effect Judge gate (git_push). The dispatcher proves itself across all three risk classes in the same execution, which the prior Q7 lock (Google Ads read-only report) could not do alone.

**Repeatability:** The same tool surface lets Betty maintain travelpec.com going forward and immediately extends to thelingerieshoppe.ca and the kPixies client sites. Phase 4.6 builds capability, not a one-shot.

---

## Locked decisions in scope

From the v2 kickoff and the decisions log:

- **Q1 Decision A** — `risk_class` is a per-tool constant declared in the tool registry.
- **Q1 Decision B** — Adapter populates `risk_class` from registry metadata; the actor never reasons about risk.
- **Q1 Decision C** (implied by A+B) — Actor's inner loop skips the Judge for `risk_class == "read_only"` tool calls and returns the tool result directly.
- **Q7 (re-locked 2026-05-24)** — travelpec.com autonomous deploy is the first executor.
- **Operational boundary 4** — No media sourcing. Image placeholders only: `<!-- IMAGE: short description -->`.
- **Operational boundary 5** — Autonomous deploy via `git push`. Judge gates the push. No human pre-push review. Safety net is `git revert`.
- **Operational boundary 6** — Tool registry designed for ongoing site maintenance, not one-shot.

---

## Out of scope (deferred to post-win phases)

The following are deferred and tracked in `OPEN_QUESTIONS.md`:

- Operator Review UI (dual-button Approve Action vs Promote to Rule governance) — Q4, Q5.
- HEARTBEAT.md + launchd autonomous trigger loop — was Phase 4.8.
- Generalized universal dispatcher abstraction — Q2.
- Async / parallel tool execution — Q3.
- `judge_decisions` table fields beyond the minimum audit-trail set — Q6 advanced.
- Authorization envelope sub-decision (actor vs adapter split for `authorization_refs`) — deferred from Q1.
- Authorization-freshness handling (Littlebird's slow-burn concern) — deferred from Q1.
- Second-executor stress test (`send_client_email` or other) — Q9.
- Markdown OS spec completion (SOUL.md, IDENTITY.md, TOOLS.md, SKILL.md, HEARTBEAT.md) beyond what 4.5+4.6 require — Q8.
- `.emlx` ETL, SKILL.md routing, OpenBrain recall — none load-bear for the milestone.

Every deferred item gets a one-line entry in `OPEN_QUESTIONS.md` pointing back to this kickoff and the decisions log for context. The discipline: if a future scoping debate touches any of the above, the default answer is "defer until post-launch, document a null."

---

## Phase 4.5 — Envelope minimum + Judge-skip + audit trail

Phase 4.5 is the contract-level work. It does not ship Peter a deployed site by itself; it makes Phase 4.6's tool surface safe to execute.

### Implementation checklist

**1. Extend the `ToolEntry` dataclass.**

In `claw/betty_claw/tools/__init__.py`:

```python
from typing import Literal

RiskClass = Literal["read_only", "reversible_write", "external_side_effect", "high_risk"]

@dataclass(frozen=True)
class ToolEntry:
    callable: Callable
    schema: dict
    risk_class: RiskClass  # NEW — required, no default
```

`risk_class` is required at registration time. No tool may be registered without one. This is the structural forcing function from Q1.

**2. Update the existing `draft_email` registration.**

Set `risk_class="reversible_write"`. `draft_email` writes a proposal file to the local filesystem with no external side effect — reversible_write is correct. Existing Phase 4.3 tests must continue passing without modification.

**3. Adapter populates `risk_class` at envelope-construction time.**

In the OpenClaw adapter (the layer that turns a tool-call into a JudgeVerdict envelope), look up `TOOLS[tool_name].risk_class` and write it onto the envelope. The actor never sees this field and never emits it. This matches the Phase 4.3 actor-vs-adapter split discipline.

**4. Actor inner loop skips Judge for `read_only` tools.**

In `claw/betty_claw/actor.py`, before invoking the Judge, branch on `risk_class`:

```python
if tool_entry.risk_class == "read_only":
    # Execute directly, no Judge round-trip
    result = tool_entry.callable(**arguments)
    # Still write to journal for audit trail
    journal_tool_call(tool_name, arguments, result, judge_skipped=True)
    return result
else:
    # Existing path: build envelope, call Judge, execute on APPROVE
    verdict = judge.before_tool_call(envelope)
    ...
```

The journal entry distinguishes Judge-skipped calls from Judge-approved calls. This is required so the audit trail captures *every* tool call, not just Judge-gated ones.

**5. Add `judge_decisions` table — minimum schema only.**

New file `ops/schema/002_judge_decisions.sql`:

```sql
CREATE TABLE judge_decisions (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tool_name       TEXT NOT NULL,
    risk_class      TEXT NOT NULL,
    envelope_json   JSONB NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN ('APPROVE', 'REJECT', 'SKIP_READ_ONLY')),
    cost_usd        NUMERIC(10, 6) NOT NULL DEFAULT 0,
    reasoning       TEXT,
    executed_at     TIMESTAMPTZ,
    execution_result JSONB
);

CREATE INDEX idx_judge_decisions_timestamp ON judge_decisions (timestamp DESC);
CREATE INDEX idx_judge_decisions_tool_name ON judge_decisions (tool_name);
CREATE INDEX idx_judge_decisions_verdict ON judge_decisions (verdict);
```

Wire the Judge to write a row after every verdict construction (including SKIP_READ_ONLY for the journal-equivalent path from step 4). This is the audit trail Q6 minimum. Reviewing what Betty did overnight comes down to `SELECT ... FROM judge_decisions WHERE timestamp > 'last night'`.

**6. Document `authorization_refs` as a forward-compatible field.**

In the envelope schema (whatever Phase 4.3 left in place), add `authorization_refs: list[str]` as an optional field with no validation. Phase 4.5 does not enforce population, freshness, or semantic meaning — those are deferred. The field exists so Phase 4.6's tool calls don't require schema migration when authorization handling lands later.

### Phase 4.5 verification gate

- All eight existing self-tests pass with no modifications.
- New test: `test_actor_skips_judge_for_read_only` — registers a synthetic read_only tool, invokes the actor, asserts Judge was not called and result was returned.
- New test: `test_judge_decisions_row_written` — invokes the Judge against a reversible_write tool, asserts a row landed in `judge_decisions` with the correct verdict, cost, and envelope JSON.
- New test: `test_judge_decisions_skip_row_for_read_only` — invokes a read_only tool, asserts a row landed in `judge_decisions` with `verdict='SKIP_READ_ONLY'` and `cost_usd=0`.
- `draft_email`'s existing end-to-end test passes unchanged (the `reversible_write` classification triggers the Judge path it was already exercising).
- BUILD_LOG.md entry committed journaling the contract change, with a clear "the Judge no longer sees read_only tool calls" note for future readers.

### Phase 4.5 Definition of Done

A single commit on `main` that (a) adds the `risk_class` field, (b) sets `draft_email.risk_class="reversible_write"`, (c) wires the read-only skip path, (d) lands the `judge_decisions` migration and Judge write-path, (e) adds the forward-compatible `authorization_refs` field, (f) passes all tests, (g) journals in BUILD_LOG.md.

Estimated work: one focused session. No new external dependencies. No infrastructure changes beyond the migration.

---

## Phase 4.6 — Tool surface for travelpec.com + first autonomous run

Phase 4.6 is the visible milestone. Phase 4.5 makes it safe; Phase 4.6 makes it happen.

### Tool registry

Phase 4.6 adds the following tools, each registered with an explicit `risk_class`:

| Tool name | risk_class | Purpose |
|---|---|---|
| `read_file(path)` | `read_only` | Read repo files and content research. Judge-skipped. |
| `list_directory(path)` | `read_only` | Enumerate repo structure. Judge-skipped. |
| `git_status()` | `read_only` | Inspect working-tree state. Judge-skipped. |
| `git_diff(path=None, staged=False)` | `read_only` | Inspect pending changes. Judge-skipped. |
| `write_file(path, content)` | `reversible_write` | Write Astro source, components, layouts, content collections. Judge-gated. Atomic write pattern (tmpfile + fsync + os.replace) per existing discipline. |
| `git_commit_all(message)` | `reversible_write` | Stage all changes and commit locally. Judge-gated. |
| `git_push(remote="origin", branch=None)` | `external_side_effect` | Push to GitHub; Cloudflare's webhook deploys to live travelpec.com. Judge-gated with highest rigor. |

Seven tools total. Three Judge-skipped (read), three Judge-gated reversible, one Judge-gated with external effect. This is the minimum surface that supports the milestone.

### Implementation-start checkpoint (Emdash MCP scope)

Before Phase 4.6 implementation begins, confirm with Peter: does travelpec.com content live in Emdash CMS (requires `emdash_*` MCP tool wrappers) or in Astro markdown content collections written to the repo (covered by `write_file` alone)?

The kickoff commits to **Astro markdown content collections via `write_file`** as the default — this keeps the tool surface narrow and the Judge cost predictable. If Peter confirms content must go through Emdash, Phase 4.6 gains 3–4 additional Emdash MCP tool wrappers (`emdash_get_content`, `emdash_create_content`, `emdash_update_content`, `emdash_get_schema`), each registered with appropriate `risk_class`. This is an additive change, not an architectural pivot — the per-tool risk_class contract from Phase 4.5 handles it cleanly.

### Constraints on the first autonomous run

- **No `shell` / `exec` / `run_python` tool.** The architecture explicitly forbids arbitrary code execution surfaces (Q1 explicit non-exception). Betty cannot run `astro check`, `npm run build`, or any local validation command. Cloudflare's build step does that at deploy time. If the build breaks, the deploy fails and Peter sees the failure in the Cloudflare dashboard — no broken site goes live.
- **Image handling is placeholder-only** per operational boundary 4. Betty writes `<!-- IMAGE: description -->` comments. No image search, no AI image generation, no fetching from URLs.
- **Content research input lives in the repo** as files Betty reads with `read_file`. To be confirmed at implementation start: a `content/research/` folder or similar, with one file per page/section to be built. If research lives elsewhere (Google Drive, a brief in the Betty hand-off folder), Peter syncs it into the repo before the overnight run.
- **Single working branch.** Betty operates on `main` directly. No feature-branch ceremony. The "revert if bad" safety net works on `main` commits.
- **One overnight run target.** The first run aims to complete the travelpec.com build top-to-bottom. The `$5/day` Judge cap and the actor's circuit breaker are the only auto-stop mechanisms. If they fire, Betty halts and the partial work is committed up to that point.

### The first autonomous run, narrated

Peter kicks off the run before bed with a one-shot prompt to Betty along the lines of: *"Finish the travelpec.com build. Content research is at `content/research/`. Use the Emdash/Astro template already in place. Image placeholders only. Commit and push when each major section is complete. Stop on any rejection breaker trip."*

Betty's actor loop:
1. `list_directory('content/research/')` — read_only, no Judge — enumerates research files.
2. `read_file('content/research/sandbanks-overview.md')` — read_only, no Judge — pulls content for a page.
3. `read_file('src/pages/index.astro')` — read_only — inspects existing template.
4. `read_file('src/layouts/PageLayout.astro')` — read_only — inspects layout.
5. `write_file('src/pages/sandbanks.astro', content)` — reversible_write — Judge round-trip — Judge approves a focused page write, citing the research file as evidence.
6. `git_diff('src/pages/sandbanks.astro')` — read_only — Betty inspects her own work.
7. (More writes across pages, layouts, content collections.)
8. `git_commit_all("Add Sandbanks page from research")` — reversible_write — Judge round-trip — Judge approves a commit that matches the staged diff.
9. `git_push()` — external_side_effect — Judge round-trip with highest rigor — Judge inspects the commit history about to be pushed, evaluates whether each commit message matches its diff, whether sensitive content (API keys, credentials) is present, whether the push would break the deployment. Approves or rejects.
10. Cloudflare webhook fires, Astro builds, site updates live.
11. Loop: next research file, next page, repeat.

If at any step the Judge rejects, the actor's per-turn rejection breaker increments. After N consecutive rejections (existing Phase 4.3 setting), Betty halts the loop and journals the failure. Peter reviews in the morning.

If at any step the `$5/day` cap is hit, the Judge refuses further calls and Betty halts. Peter reviews in the morning.

If everything works: Peter wakes up, opens travelpec.com, sees a deployed site. Reviews `git log` for what Betty did. Reverts any bad commits with `git revert`. Swaps image placeholders for real assets in a manual pass.

### Phase 4.6 verification gate

- All Phase 4.5 verification tests still pass.
- Per-tool smoke tests for the seven new tools, each asserting the correct `risk_class` is on the registry entry and the tool executes its happy path against a temp directory.
- An end-to-end "synthetic dry-run" test: a scripted scenario where the actor is given a tiny content research file, builds a single Astro page, commits, and pushes to a *test repo* (not travelpec.com). Asserts the full chain executes, the Judge gates the three Judge-gated calls, and the test repo receives the push. This is the architectural smoke test before the real overnight run.
- The first real overnight run on travelpec.com itself is the acceptance test. Success = a deployed site Peter approves of in the morning.

### Phase 4.6 Definition of Done

A second commit on `main` that (a) registers the seven new tools with their `risk_class` values, (b) implements each tool with the same discipline as `draft_email` (input validation, atomic writes where applicable, structured tool result), (c) passes the synthetic dry-run test against a test repo, (d) journals in BUILD_LOG.md. The real overnight run on travelpec.com is the post-merge acceptance check; failure of that run does not invalidate the Phase 4.6 closure — it generates a Phase 4.6.1 follow-on focused on whatever broke.

Estimated work: one to two focused sessions for the tool implementations. The overnight run itself is unattended.

---

## Safety properties this milestone depends on

These are inherited from Phase 4.3 and the Cleanup Phase — Phase 4.5+4.6 do not redesign them, only depend on them:

1. **`$5/day` Judge spend cap.** Hard cap; Judge refuses calls once exceeded. Cap resets at midnight local. If Betty's overnight run exceeds the cap, she halts; she does not silently downgrade or skip the Judge.
2. **Per-turn rejection breaker.** N consecutive REJECT verdicts in a single inner-loop turn halts the actor. Prevents Betty from spinning indefinitely on a Judge that keeps saying no.
3. **Atomic write pattern.** `write_file` uses tmpfile + fsync + os.replace. Partial writes never land. Inherited from `draft_email`'s implementation pattern.
4. **`risk_class` is a tool registry constant.** Per Q1 Decision A, no tool can change its risk class at runtime. The Judge knows the risk class of every call from the registry, not from anything Betty emitted.
5. **Adapter populates risk_class.** Per Q1 Decision B, the actor cannot lie about (or omit) its own risk class. Defense against actor and safety system sharing blind spots.
6. **Read-only Judge-skip is documented and audit-trailed.** Phase 4.5 step 5 ensures every read-only call still produces a `judge_decisions` row with `verdict='SKIP_READ_ONLY'`, so "what did Betty read overnight" is queryable.
7. **`git revert` as rollback.** External — not enforced by Betty's code, but the architecture is designed around it being available. If Cloudflare deploys something bad, Peter has a one-command rollback path.

---

## Sequencing

1. Land this kickoff document on `origin/main` (Peter pushes via GitHub web UI or via Betty's Mac, same as v2 kickoff).
2. Phase 4.5 implementation session.
3. Phase 4.5 verification gate passes; commit lands on `main`.
4. Implementation-start checkpoint with Peter: confirm Emdash MCP scope for travelpec.com content.
5. Phase 4.6 implementation session(s).
6. Phase 4.6 verification gate passes (synthetic dry-run against test repo); commit lands on `main`.
7. Peter kicks off the first overnight run on travelpec.com.
8. Acceptance review the next morning.
9. Post-acceptance: BUILD_LOG entry closing Phase 4.6, OPEN_QUESTIONS.md re-prioritization for the deferred items.

---

## What success looks like

The morning after the first overnight run, Peter opens travelpec.com and sees a finished site. He reviews `git log` and the `judge_decisions` table for what Betty did. He swaps `<!-- IMAGE: ... -->` placeholders for real photos in a separate manual pass. He calls his first kPixies client.

That is the win. Everything else in the Betty roadmap — UI, HEARTBEAT autonomy, second executor, OB1 spec completion — gets re-prioritized from that vantage point, not from this one.
