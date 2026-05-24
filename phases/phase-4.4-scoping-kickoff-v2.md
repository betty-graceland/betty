# Phase 4.4 Scoping Chat Kickoff (v2) — Universal Operator UI + Execution Dispatcher

## Role

You are the Lead AI Implementation Engineer and Pair Programmer. You held this role through Stages 1-3, Phases 4.1, 4.2, 4.3, and the Cleanup Phase.

This chat is scoping, not execution. Your output is an *architecture decision document* that the next chat will use as input for an execution kickoff. You write no production code in this chat. You may write throwaway code sketches to pressure-test design choices, but they live in chat, not in the repo. (See workflow rules below for the one exception: contract sketches that mature into specifications.)

Peter works in audio mode often and prefers dialogue-style exploration to verbose lists. Hard rule: do not suggest stopping work, taking a break, or closing the laptop. Do not offer unsolicited wellness advice or schedule management. Assume Peter is operating precisely when and how he intends to. Your job is to execute the next architectural step — which in this chat means thinking rigorously, not coding.

## Boundary

The Cleanup Phase closed at commit `7845519` on `origin/main`. The original Phase 4.4 scoping kickoff (v1) landed at commit `fb6c3d1` as `phases/phase-4.4-scoping-kickoff.md`. **This v2 document supersedes v1.** v1 is preserved in git history as the original framing; v2 is what the scoping chat works from.

Verify the boundary:

```bash
cd ~/code/betty
git log --oneline -8
git status
```

The four Cleanup Phase commits (`86ca1af`, `17bf70b`, `43ae4a3`, `7845519`) and the Phase 4.3 closure (`705b9cc`) beneath them are immutable history. The commit landing v1 (`fb6c3d1`) is part of that immutable history too. The commit landing this v2 document will be visible above that boundary.

If the boundary is not intact, stop and diagnose before any scoping work.

## Why this v2 reframing exists

v1 framed the proposal-shape question (Q1) as **extensibility**: "does the existing schema generalize as-is, or does it need new fields to decouple proposal identity from execution path?" An external strategic assessment against the full Nate B. Jones / OpenBrain Judge Extender v1 spec surfaced that this framing answers the wrong question.

The current proposal envelope is a 5-field shape: `{schema_version, call_id, tool_name, proposed_at, arguments}`. The OB1 Judge Extender v1 spec defines a ~30-field envelope including `risk_class`, structured `authorization` references with quote/timestamp, `evidence` source refs, `expected_consequence` (recipients, data exposed, systems changed, persistence), `rollback` (reversible / plan / owner), and a `sensitivity` flag block. Phase 4.3's envelope is a strict subset.

**Extensibility says: the `arguments` dict is polymorphic, so generalization is trivial.** That answer is technically correct and architecturally wrong. It commits Phase 4.4 to a dispatcher that consumes a 5-field envelope, which means every executor module is built against the wrong contract, which means Phase 4.6+ (when Google Ads or Shopify or Emdash executors land) will require a second migration that rewrites both the dispatcher and every executor module already written. The Cleanup Phase paid down structural debt from a single types.py rename across six importers. The cost of paying down structural debt from a proposal-envelope migration across N executors is materially higher.

**Spec compliance says: which OB1 v1 fields are load-bearing for the dispatcher's design, and which can land as documented nulls?** That framing forces the architectural decision to happen once, in scoping, before the dispatcher contract locks any field names.

The same reframing applies to the Operator UI. v1 frames the UI as "the gate between Judge-approval and execution." That collapses two separate governance functions into one click:

1. **Approve Action** — this specific proposal executes now.
2. **Promote to Rule** — separately, this verdict becomes recall-grade memory for future Judge calls.

Per the OB1 spec, conflating these is the exact rubber-stamp failure mode the architecture is meant to prevent. A blocked action should be retrievable as evidence by default; only an explicit human promotion turns a verdict into instruction-grade memory that future Judge calls will treat as authoritative. v2 makes the dual-button governance an explicit Phase 4.4 architectural commitment.

The dispatcher and the email-vs-other-executor decision from v1 are also reopened in light of these reframings.

## Phase 4.4 in the broader launch plan

v2 sits inside a six-phase launch sequence whose goal is **Betty executing one real-world daily task autonomously by Phase 4.8.** Briefly:

- **Phase 4.4** (this chat): architecture scoping. No code in the repo.
- **Phase 4.5**: pre-work. Two commits — `judge_decisions` table migration, envelope extension with the OB1 v1 minimum fields locked in Q1 below.
- **Phase 4.6**: universal dispatcher + first executor. The dispatcher reads new-shape proposals; the first executor proves the loop.
- **Phase 4.7**: Operator Review UI with dual-button governance.
- **Phase 4.8**: HEARTBEAT.md + launchd plist. **Autonomous execution. Launch.**
- **Phase 4.9**: second executor against the same dispatcher. Proves the contract held.

The OB1 `memories` table, the recall step, SKILL.md routing, REVISE Judge decisions, and .emlx ETL extraction are Stage 5+ work. Not on the critical path to launch.

## Peter's working hypothesis (to pressure-test, not ratify)

Same as v1: same-phase combined approach for the universal dispatcher and first executor, with the Operator UI as the gate. v2 does not change this lean; it sharpens the architecture questions the dispatcher and UI must answer.

The scoping chat's job is to pressure-test this hypothesis with rigorous architectural thinking. Either confirm it with sharper reasoning, or surface concerns that change it. Do not rubber-stamp.

## What changed from v1 to v2

For the scoping chat to be efficient, the deltas are listed explicitly:

- **Q1 reframed**: extensibility → OB1 spec compliance. New table of load-bearing vs deferrable fields.
- **Q5 expanded** (was Q4 in v1): UI is dual-purpose. New required architectural commitment to two separate buttons sharing the same screen but representing distinct intent surfaces.
- **Q6 added** (was implicit in v1): `judge_decisions` table migration promoted from OPEN_QUESTIONS.md to gating dependency. The UI cannot list verdicts that don't exist in queryable form.
- **Q7 reopened** (was assumed in v1): first-executor choice. v1 inherited `draft_email` as the first executor from Phase 4.3's demo tool. That inheritance is demo-driven, not workflow-driven, and email is also the highest-risk executor. Reopened for explicit decision.
- **Goal alignment statement added** (below): explicit framing of Betty's end-state so the scoping chat doesn't drift toward spec-compliance for its own sake.

## Goal alignment (what we are actually building)

Betty is a **local-first AI operator for Peter's actual business work** — Emdash website management for brand directories like `thelingerieshoppe.ca` and `travelpec.com`, Google Ads reporting and (eventually) adjustments for kPixies clients (Jump Sudbury, DecorChic, Cooks on Main, etc.), SEO initiative administration, and incremental replacement of marketing software (HubSpot dashboards, reporting tools) that currently consumes Peter's time or budget.

The Nate B. Jones / OB1 / OpenClaw spec is the **scaffold that lets Betty do this work safely.** It is not the deliverable. The deliverable is Betty doing the Monday-morning Jump Sudbury Google Ads report autonomously without burning the house down.

Phase 4.8 (HEARTBEAT + launchd) is the launch milestone. If the scoping chat finds itself debating fields that exist only to comply with the OB1 spec for its own sake — and those fields don't load-bear for any executor that will exist within Phase 4.6 or 4.9 — the right answer is to defer them with documented nulls, not to land them in Phase 4.5.

Drift signal: if Phase 4.9 closes and we are scoping Phase 4.10 as "add SOUL.md / IDENTITY.md / SKILL.md routing for completeness" while Betty has not yet shipped a useful daily task, we have drifted. The architecture is meant to enable business work, not become the business work.

## Open questions the scoping chat must answer

Work through these in roughly the order listed, but feel free to reorder if dependencies surface.

### Q1. OB1 envelope minimum compliance (reframed from v1)

The OB1 v1 envelope spec defines ~30 fields. The question is not "should we support all 30?" — it is "which fields are load-bearing for the dispatcher's design, and which can we defer with documented nulls?"

The architectural assessment behind this v2 names a candidate minimum and a candidate deferrable set. Scoping chat must lock these (or revise them with stated reasons).

**Candidate minimum (lands in Phase 4.5 pre-work):**

| Field | Why load-bearing |
|---|---|
| `risk_class` | Gates whether Judge is invoked at all. Without it, every tool call burns Opus. With it, read-only retrievals skip the Judge entirely and the daily cap is reclaimed for the calls that actually need governance. |
| `authorization.user_authorization_refs[]` (at least one, with `kind` + `quote_or_summary` + `timestamp`) | Preserves the chain of accountability that justifies the action. The Judge reads it; the UI displays it; future recall will index on it. |
| `evidence.source_refs[]` (zero or more, with `kind` + `uri` + `summary`) | The Judge's audit trail. Hallucinated references become a deterministic block before the Judge is called (per the ARCHITECTURE.md "adapter validates evidence" decision). |
| `rollback.is_reversible` (boolean) | Determines whether the dispatcher needs to track post-execution undo info or treat execution as terminal. Different executors have different answers; the dispatcher needs to know. |
| `expected_consequence.external_recipients[]` (list, may be empty) | Gates sensitivity checks for non-email executors. A Google Ads budget change has external_recipients=[] but is still consequential; the field surfaces the right distinction. |

**Candidate deferrable (nullable or absent in Phase 4.5, documented in OPEN_QUESTIONS):**

| Field | Why deferrable |
|---|---|
| `sensitivity.contains_*` flags | Auto-computable by the adapter from content. Doesn't need actor input. |
| `arguments_digest` + `full_arguments_ref` | Use raw arguments dict for now. Hash/retention concerns come with multi-user or compliance scope. |
| `workspace_id` / `project_id` / `task_id` / `flow_id` | Single-workspace Betty. Adapter can hard-code `workspace_id="betty"` until multi-workspace becomes real. |
| `runtime` + `actor` metadata blocks | Adapter-computable. Actor doesn't need to populate. |
| `expected_consequence.data_exposed[]` / `systems_changed[]` | Per-executor metadata. Better lived in executor manifests than asked of Qwen. |

Scoping chat must lock:

1. The exact field list for Phase 4.5 (this candidate or a sharper version with stated reasons).
2. The exact field list for explicit deferral (this candidate or a sharper version).
3. Where the deferred set is documented — `OPEN_QUESTIONS.md`, a new `claw/proposals/ENVELOPE_SPEC.md`, or as comments in the `contracts.py` definitions.
4. Whether the actor (Qwen) populates the load-bearing fields or the adapter does. Per Phase 4.3's locked decision: actor emits the semantic justification (intent, authorization basis, evidence IDs, risk self-assessment); adapter adds the mechanical fields. Q1's minimum likely splits: actor populates `authorization`, `evidence`, `expected_consequence.external_recipients`; adapter populates `risk_class` (via executor classification) and `rollback.is_reversible` (via executor metadata).

### Q2. Dispatcher architecture

The dispatcher is the module that takes "operator approved proposal X" as input and produces "real-world effect Y" as output.

- Where does it live? `claw/betty_claw/dispatcher.py` (single module) vs `claw/betty_claw/executors/` (subpackage)?
- Executor discovery: static registry (mirrors `tools/__init__.py`'s `TOOLS` dict), dynamic filesystem scan, or explicit registration?
- Executor contract: function `execute(proposal: dict) -> ExecutionResult`, or class with `validate()` / `execute()` / `rollback()` methods? The shape determines how much safety machinery is per-executor vs shared.
- Does the dispatcher consume the new envelope fields directly (switching on `risk_class` for verbose logging differences, etc.), or pass them straight through to the executor?
- What happens to the proposal file after execution? Delete-on-success, move to `claw/proposals/executed/`, append `execution_result` and leave in place, or write a separate audit log?
- Reversibility/idempotency handling: per-executor concern or shared infrastructure?

### Q3. Synchronous vs asynchronous execution

This determines whether Betty's actor loop is fundamentally changed by Phase 4.4 or not. Upstream of UI modality (Q4) and downstream of dispatcher architecture (Q2).

Lean from v2 assessment: **asynchronous.** Actor returns "proposed, awaiting review" immediately; executor result is a file the actor reads on next turn; no blocking on Peter's approval latency.

Pressure-test the lean:

- When does async break? Multi-step workflow where the actor needs to know whether step 1 executed before planning step 2. Does any Phase 4.6 first-executor candidate hit this?
- If async: how does the actor learn an approved proposal executed? Executor writes a result file the actor reads on next turn? Actor never knows, and execution is purely Peter's domain after proposal?
- Does the answer differ by tool? Email could be fire-and-forget async; a Google Ads budget change might need synchronous confirmation before the actor reports outcome.

### Q4. Operator UI modality

Lean from `OPEN_QUESTIONS.md`: FastAPI + Jinja2 templates + Tailwind CDN, localhost-only binding, no build step.

Pressure-test against:

- TUI (terminal-based, runs in iTerm, lower overhead)
- Slack DM with Approve/Reject buttons
- Mac-native notification with deep-link
- Just CLI prompt: `betty review` opens the next pending proposal in `$EDITOR`

How does Peter learn that a new proposal exists? Filesystem polling, file-watcher events, notification, or pull-only (Peter checks when he wants to)?

**Implementation details deferred to execution chat:** what the UI shows per proposal (raw JSON vs rendered preview vs both), exact rejection-feedback mechanism. Decided as the execution chat builds.

### Q5. UI dual-purpose governance (new framing — required architectural commitment)

The UI is two gates, not one. Per the OB1 spec, conflating them is the rubber-stamp failure mode.

**Surface 1 — Approve Action:**
- This specific proposal executes now.
- Verdict written to `judge_decisions` table (see Q6).
- Proposal lifecycle proceeds (executor runs, result captured, proposal moved/annotated per Q2).
- Default behavior, single-click.

**Surface 2 — Promote to Rule:**
- Separately, this verdict (whether the proposal was approved or rejected) becomes recall-grade memory for future Judge calls.
- Provenance changes from `generated` to `user_confirmed`.
- Use policy changes from `requires_confirmation` to `can_use_as_instruction`.
- Future Judge calls retrieve this as authoritative context, biasing them toward consistent decisions on similar proposals.
- **Anti-rubber-stamp safeguard (per OB1 spec):** the UI must display "examples of future actions this memory could influence" before the Promote button enables. Reviewer must see the downstream consequences of confirmation before they confirm.

Scoping chat must lock:

1. Whether the Promote surface ships in Phase 4.4/4.6 or is deferred to a later phase. The Phase 4.5 pre-work landing `judge_decisions` does NOT include the `memories` table — that is Phase 5+. So Promote may be a Phase 4.7 UI element with deferred backing storage, or it may be deferred entirely until the `memories` table lands.
2. If deferred: how the UI signals the future capability without enabling it (greyed-out button with tooltip? not displayed at all?).
3. If in-scope for Phase 4.7: minimum viable `memories` table shape (probably `id`, `provenance`, `use_policy`, `text`, `created_from_decision_id`, `created_at`, `scope_workspace`), and where it lands (`ops/schema/003_memories.sql`).
4. The architectural commitment is non-negotiable even if Promote is deferred: the UI design must make the two surfaces separable so that adding Promote later doesn't require re-architecting the Approve flow.

### Q6. judge_decisions table (gating dependency — promoted from OPEN_QUESTIONS)

Currently Judge verdicts vanish into the spend ledger (cost only) and the in-memory rejection counter (transient per turn). The Operator UI cannot list verdicts that don't exist in queryable form.

Scoping chat must lock:

1. Table shape. Candidate:

   ```sql
   CREATE TABLE judge_decisions (
       call_id          UUID PRIMARY KEY,
       proposal_path    TEXT NOT NULL,
       tool_name        TEXT NOT NULL,
       risk_class       TEXT,  -- nullable until Q1 lands; required after
       decision         TEXT NOT NULL,  -- 'approve' | 'reject'
       reasoning        TEXT NOT NULL,
       input_tokens     INTEGER NOT NULL,
       output_tokens    INTEGER NOT NULL,
       cost_usd         NUMERIC(10, 6) NOT NULL,
       decided_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
       user_request     TEXT NOT NULL,
       arguments        JSONB NOT NULL
   );
   ```

   Revise as needed. The decision shape should accommodate REVISE/ESCALATE addition in Stage 6 without re-migration — likely by allowing `decision` to be a free string with a CHECK constraint that can grow.

2. When the migration lands: Phase 4.5 pre-work (alongside envelope extension), or Phase 4.6 alongside dispatcher. Recommended: Phase 4.5, because the UI design in Phase 4.7 needs to reason against the real table shape and the dispatcher in Phase 4.6 may want to query it.
3. Where Judge writes to it: inside `Judge.before_tool_call()` after verdict construction, before return. Write-back is part of verdict lifecycle, not optional.
4. Whether the spend ledger is now redundant. Lean: no — the ledger's per-day rollup is structurally different from the per-decision log. They co-exist; ledger gates the cap, decisions table backs the audit trail.

### Q7. First-executor selection (reopened from v1's email default)

v1 inherited email as the first executor because Phase 4.3 chose `draft_email` as the Phase 4.2 demo tool. That choice was demo-driven, not workflow-driven. The actual high-value first executors for Peter's daily work are:

| Candidate | Risk class | First-executor pros | First-executor cons |
|---|---|---|---|
| **Google Ads weekly report** | `read_only` | Real Monday motion Peter does today; lowest Judge cost (read-only doesn't need Judge); proves dispatcher + UI without external side effects; Jump Sudbury report is the canonical use case. | Requires Google Ads API integration (OAuth, account ID resolution) — non-trivial integration work, though no SMTP. |
| **Emdash content publish** | `reversible_write` | Real website-management workflow; reversibility means lower stakes if dispatcher has bugs; per `reference_emdash_oauth_patch.md` Peter already has Emdash OAuth working. | Multi-step (draft → publish → verify); proposal envelope per publish is more complex than a report fetch. |
| **Email send** | `external_side_effect` | Phase 4.3 already shaped the proposal contract around it; least integration work. | Highest-risk (irreversible, external, deliverability, AI-disclosure footer); proves the *hardest* case first rather than the simplest; not a daily Peter workflow in the way Google Ads reporting is. |

Engineering-wisdom argument: **start with the simplest case, prove the architecture, then add complexity.** That argues for Google Ads weekly report as the first executor. The dispatcher's contract gets validated against a `read_only` proposal where the Judge is skipped entirely (per Q1's risk_class gating) — which is itself a meaningful contract test, because it proves the dispatcher handles the case where no verdict exists, not just the case where one does.

Scoping chat must lock:

1. Which executor lands in Phase 4.6 — Google Ads weekly report, Emdash content publish, or email send.
2. If Google Ads: what does the proposal envelope look like? Likely `risk_class="read_only"`, `evidence_refs=[{kind: "api", uri: "google-ads://reports/jumpsudbury/week-YYYY-MM-DD"}]`, `expected_consequence.external_recipients=[]`, `rollback.is_reversible=true` (a read action has nothing to roll back; setting `true` keeps the schema honest).
3. If Emdash: same envelope sketch.
4. If email is retained: explicit rationale, plus deferred-list for what slips from Phase 4.6 to Phase 4.7 (SMTP credentials, disclosure footer, provider choice).
5. What happens to `draft_email` if it's not the first executor? Probably stays as the Phase 4.3 demo tool, still works, but doesn't get extended in Phase 4.6.

### Q8. Phase decomposition

Once Q1–Q7 are decided, the Phase 4.4 architectural deliverable is decomposed into landable commits:

- Phase 4.5 pre-work: how many commits, in what order? Candidate: (1) `002_judge_decisions.sql` migration + `Judge` write-back wiring; (2) `contracts.py` extension with new envelope fields; (3) `draft_email` updates to populate the new fields (or whichever first-executor is chosen, if `draft_email` isn't the chosen executor).
- Phase 4.6 dispatcher + first executor: one phase or two? Peter's lean is one. Pressure-test against scope.
- Minimum viable Phase 4.6 architecture vs deferred: could the UI be a CLI prompt loop initially and richer in Phase 4.7? Could the dispatcher start hardcoded and generalize later, or is that exactly the same anti-pattern the Cleanup Phase paid down?
- Verification gate for each phase: the Cleanup Phase used eight self-tests with ~$0.39 of Anthropic spend. What does Phase 4.5's gate look like? Phase 4.6's? Phase 4.7's (UI testing is harder — what's the minimum useful test)?

### Q9. Surface area for future executors

The dispatcher is being designed with one executor as its first consumer and others coming later. Questions to clarify *now*, even though they're not built yet:

- What are the rough proposal shapes for the future executors not chosen in Q7? Knowing the shape pressure-tests whether the dispatcher contract generalizes.
- What are the real-world-effect properties (reversibility, idempotency, cost-to-execute, time-to-execute, observability of effect) for each? These inform whether the dispatcher needs per-executor safety machinery or shared infrastructure.
- Phase 4.9 (the second-executor contract test) — which executor lands then? The one most architecturally different from the Phase 4.6 first executor, to maximize the contract stress test. If Phase 4.6 is Google Ads (read-only), Phase 4.9 should probably be email (external side effect) or Emdash (reversible write).

## Workflow rules for the scoping chat

- One question at a time. Resist the temptation to scope everything in one response. Each open question deserves dialogue, pressure-testing, and an explicit "here is the decision and here is why."
- Peter's lean is a hypothesis. If the scoping work surfaces a reason to change it, surface that reason explicitly and propose the alternative. Do not preserve Peter's lean for its own sake.
- v2's reframings (Q1 as spec compliance, Q5 as dual-button governance, Q7 as reopened first-executor choice) are themselves hypotheses to pressure-test. They were proposed in a strategic assessment, not handed down. If the scoping work surfaces a reason to revert any reframing, surface that reason explicitly.
- Decisions get logged as they're made. The scoping chat's output is a markdown document that accumulates decisions in real time, not a single response at the end.
- No code in the repo. Sketches in chat are fine. If a sketch matures into a contract worth committing — a Protocol, a dataclass, a JSON schema, a SQL DDL — it lives in the kickoff document the scoping chat produces, as a fenced code block. It does not land in `claw/` or `ops/schema/` until the execution chat opens.
- The scoping chat ends when the open questions above are all answered with explicit decisions, *and* when those decisions are organized into a kickoff document for the next chat to execute against.

## What success looks like

At scoping chat close, the next chat opens with:

1. A complete Phase 4.5 / Phase 4.6 execution kickoff document modeled on the Cleanup Phase kickoff: role, boundary, locked decisions, implementation checklist, verification gate, what success looks like.
2. Locked decisions for every Q1–Q9 above (or explicit notes for any intentionally deferred).
3. **Explicit envelope field list for Phase 4.5 pre-work, with deferred fields named and their deferral rationale documented.**
4. **Explicit dual-button governance commitment for Phase 4.7 UI design**, with the Promote-Surface scoping decision (in-Phase-4.7 vs deferred) made.
5. **Explicit first-executor decision** with the dispatcher-contract implications worked through.
6. A first-commit-is description for each phase, anchoring the execution chats in concrete starting work.
7. A verification gate plan for each phase (what self-tests pass before each commit, what Anthropic API spend is expected, what the cost ceiling is).

The scoping chat is itself a phase, with the same discipline as a code phase: a clear boundary, a clear deliverable, an explicit close.

## Notes for the execution kickoffs this scoping chat produces

The scoping chat's output is *two* execution kickoffs (Phase 4.5 pre-work and Phase 4.6 dispatcher+first-executor), or one combined kickoff if Q8 decides they fold together. When writing those documents, carry forward the following discipline from the Cleanup Phase BUILD_LOG closure:

- **Pyc-clear-before-gate discipline.** Phase 4.5 and Phase 4.6 will add new modules (`dispatcher.py`, possibly `executors/`, the `judge_decisions` write-back wiring, the envelope extensions) and may rename or refactor existing files. The Cleanup Phase incident log named stale `__pycache__` as a recurring source of transient test failures after file-structure changes. Each execution kickoff must include the pyc-clear recipe as a precondition for every verification gate run:

  ```bash
  find claw -name __pycache__ -type d -exec rm -rf {} +
  find claw -name "*.pyc" -delete
  ```

  Not optional, not "if tests fail try this" — a precondition before the gate runs.

- **Schema migration discipline.** Phase 4.5's `002_judge_decisions.sql` is the first new migration since `001_init.sql` in Stage 1. Verify `ops/schema/apply.sh` is still idempotent against the existing database before adding the migration. The migration runner should report 1 applied / 1 skipped on second run.

- **Envelope contract test.** Phase 4.5's envelope extension touches `contracts.py` (frozen dataclasses), `draft_email.py` (if retained as a tool), the actor's proposal-building path, and the Judge's user-message builder. Each of those is a self-test that must pass. Add a new self-test specifically exercising the envelope's load-bearing fields (risk_class round-trips correctly; authorization_refs survive the proposal JSON write and Judge prompt construction; etc.).

- **judge_decisions write-back is a contract.** When Phase 4.5 lands the write-back call inside `Judge.before_tool_call()`, the Judge's self-test (currently exercises 6 scenarios for $0.1157) needs to verify that every scenario that reaches verdict construction also writes a row. The breaker-tripped and cap-exceeded and corrupt-ledger scenarios write rows too (with `cost_usd = 0.0`, distinguishable by `decision = 'reject'` and `reasoning` content).

- **Dispatcher and executor self-tests should run independently of the actor.** Phase 4.6's dispatcher self-test feeds a synthetic proposal JSON to the dispatcher and asserts the executor was called with the right shape. The first-executor's self-test runs against a real API call (Google Ads sandbox, Emdash test workspace, or a Peter-controlled test SMTP — whichever Q7 decides). The two are separate verification gates.

Other discipline patterns from the Cleanup Phase (atomic commit boundaries, self-test verification gates, explicit cost ceilings, BUILD_LOG entries at phase close) carry forward by default.

## What to do when starting the scoping chat

1. Paste this v2 kickoff document as the opening message.
2. Verify the boundary commit (`7845519`) is intact and v1 kickoff (`fb6c3d1`) is preserved in history.
3. Discover the current repo structure. Run:

   ```bash
   cd ~/code/betty
   find . -maxdepth 3 -type d -not -path '*/\.*' -not -path '*/__pycache__*' | sort
   find claw/betty_claw -maxdepth 2 -name "*.py" | sort
   ls -la pyproject.toml ARCHITECTURE.md OPEN_QUESTIONS.md
   cat pyproject.toml
   ```

   Do not assume what's in the repo from BUILD_LOG, from this kickoff document, or from prior Claude context. Run the discovery, trust the discovery.

4. Read the Cleanup Phase BUILD_LOG entry (lines 833 onward in `~/code/betty/BUILD_LOG.md`) for context on the discipline patterns established by the prior phase.
5. Read Phase 4.3's closure (lines 531-832) for the architectural context this phase builds on.
6. Read the v1 kickoff (`phases/phase-4.4-scoping-kickoff.md`) for the original framing — this v2 supersedes it but the v1 questions are still substantively relevant and v2 builds on them.
7. Begin with Q1 (OB1 envelope minimum compliance). The other questions depend on it — Q5 (UI dual-purpose) needs to know whether `risk_class` exists to gate the Judge; Q6 (judge_decisions table) needs to know the envelope shape to log; Q7 (first executor) needs to know the envelope shape to validate a candidate executor's proposal sketch.
