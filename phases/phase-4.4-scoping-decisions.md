# Phase 4.4 Scoping Chat — Running Decision Log

**Started:** 2026-05-24
**Kickoff boundary:** commit `17ef5a2` (v2 kickoff `phases/phase-4.4-scoping-kickoff-v2.md` landed on `origin/main`)
**Participants:** Peter (architect), Claude (lead implementation engineer), Littlebird (peer reviewer)

This document accumulates locked decisions in real time as the scoping chat works through Q1–Q9. At scoping close, it becomes the basis for the Phase 4.5 + Phase 4.6 execution kickoff(s).

---

## Q1 — OB1 envelope minimum compliance

### Decision A — Risk class is a per-tool constant (LOCKED)

`risk_class` is metadata on the tool itself, declared in the tool registry. It is NOT a per-call dynamic judgment derived from arguments.

If a tool's design would span multiple risk classes (e.g., a monolithic Shopify `update_product` that could update SEO text *or* change the price by 90%), it must be split into multiple atomic tools (`shopify_update_seo_title`, `shopify_update_price`, etc.) — each with its own constant risk class.

**Rationale (per Littlebird's reframing, which is the stronger argument):** Per-tool-constant is not just a safety property — it is a **structural forcing function** that produces atomic, intent-driven tools instead of monolithic REST wrappers. The design of the tool registry IS the access control model. This maps cleanly to the AO Execution Engine's `profile.json` capability-gating pattern Peter already uses.

**Cost accepted:** Logically-related multi-field updates now require multiple tool calls, each generating its own proposal and (if not `read_only`) its own Judge round-trip (~$0.02 each). A holistic Shopify product update across price + SEO + inventory becomes three separate evaluations instead of one. The safety value of evaluating each change independently — and the operator's ability to approve/reject each in the review queue — outweighs the cost.

**Explicit non-exception:** Tools that *appear* to require per-call dynamic classification (e.g., `execute_sql`, `run_python`) shouldn't exist in the registry at all. The architecture's safety guarantee depends on Betty's tool surface being narrow. If analytical capability is needed, build a specific tool with a fixed query template (e.g., `analyze_ads_performance(client_id, date_range)`) — never an arbitrary code execution surface.

### Decision B — Adapter populates risk_class from registry metadata (LOCKED)

`risk_class` is populated by the OpenClaw adapter at envelope-construction time, by reading `TOOLS[tool_name].risk_class` from the registry. The actor (Qwen) never reasons about risk class — never sees it, never emits it.

**Rationale:** Phase 4.3's locked split (actor emits semantic justification, adapter adds mechanical fields) extends naturally to `risk_class` — it is purely mechanical metadata about the tool, not a judgment about this invocation. Having Qwen reason about her own risk class would reproduce the prompt-based-guardrail anti-pattern Jones explicitly warns against (actor and safety system sharing the same blind spots).

### Operational boundaries (captured from Peter, validated by Littlebird)

Three boundaries that constrain Phase 4.6+ tool design. These are not Q1 sub-decisions per se, but they shape which tools land in the registry and at which risk classes:

1. **Google Ads adjustments OUT through Stage 5.** Peter is not yet comfortable with autonomous AI mutation of Google Ads budgets, bid adjustments, or campaign pause/resume. Phase 4.6+ Google Ads work is strictly `read_only` reporting until Stage 5+ revisits.

2. **Email sending IN for client reports, with mandatory adapter-level legal boilerplate.** Betty may send emails to clients (not just draft them), provided every send is appended at the adapter level — never by Qwen — with: (a) AI-disclosure statement, (b) legal-liability limitation language, (c) Peter's contact info for clarification. The `send_client_email` tool concatenates this footer before dispatch; Qwen has no path to bypass or rewrite it. This is consistent with the Stage 5 architectural commitment from the Phase 4.3 BUILD_LOG closure.

3. **Full website building IN, decomposed into atomic file-writing + git_push.** Betty builds full Astro sites by writing files and pushing branches, not via a God-mode CMS tool. Deployment is offloaded to the existing CI/CD pipeline (Vercel/Astro/Cloudflare). The "monolithic" feel of "build a website" is real but architecturally remains a sequence of atomic per-file writes and a final `git_push`. A future capability: real-time site adjustments based on test data (opportunity/issue identification). Flagged for Phase 4.8+ heartbeat-driven action scoping; not Phase 4.4 scope.

### Concrete Phase 4.5 implication

`claw/betty_claw/tools/__init__.py`'s `ToolEntry` dataclass gains one field:

```python
risk_class: Literal["read_only", "reversible_write", "external_side_effect", "high_risk"]
```

`draft_email` (the existing Phase 4.3 demo tool) sets `risk_class="reversible_write"` (writes a proposal file, no external effect). The Stage 4 actor's inner loop in `actor.py` gates the Judge call: if `risk_class == "read_only"`, the Judge is not invoked, and the tool result returns directly to the actor. This is a real contract change vs Phase 4.3 (where every tool call hits the Judge) and must be journaled in the Phase 4.5 BUILD_LOG entry.

---

## Q7 — First executor selection (LOCKED early, by side-channel)

**Phase 4.6 first executor: Google Ads weekly report (`risk_class="read_only"`).**

Locked early because Peter and Littlebird both surfaced it during the Q1 risk_class dialogue. Rationale matches the v2 kickoff's Q7 framing:

- Real Monday motion Peter does today
- Lowest Judge cost (read-only skips the Judge entirely per Decision A+B)
- Proves the dispatcher contract end-to-end without external side effects
- Validates the "Judge-skip for read_only" path, which is a load-bearing contract change in itself

**Implication for Q4/Q5 (UI design):** The Operator UI's behavior for `read_only` outputs is different from its behavior for reversible_write and above. For a read-only report, the UI doesn't gate *execution* (already done) — it presents the result and offers chaining into a follow-on side-effecting tool (e.g., "now send this as an email to the client"). For reversible_write and above, the UI is the gate before execution. Flagged for explicit treatment when Q5 (dual-button governance) is scoped.

**Implication for Q9 (Phase 4.9 second-executor stress test):** With Google Ads `read_only` as Phase 4.6 first executor, the Phase 4.9 second executor should be maximally architecturally different. Strongest candidate: `send_client_email` (external_side_effect, irreversible, requires adapter-level disclaimer concatenation). This validates the dispatcher across the read-only-skip-Judge path AND the high-rigor Judge-gated path with the locked operational boundary #2 above.

---

## Q7 — REOPENED and RE-LOCKED 2026-05-24 (supersedes prior Q7 lock)

**Phase 4.6 first executor: autonomous travelpec.com build and deploy.**

Peter pivoted scoping during the Q1 follow-on dialogue: results-to-date relative to investment (new Mac Studio, weeks of architecture work) are not yet competitive with a plain Claude subscription. Stuck-risk on full architectural buildout is unacceptable. The new criterion for Phase 4.6 is "ship the win" — concrete user-visible business value that justifies the autonomous-agent investment in one milestone.

**Concrete milestone (locked):** Betty reads the existing travelpec.com content research, finishes the in-progress Emdash/Astro template, writes the Astro source files, runs `git commit`, and runs `git push`. Cloudflare's existing CI/CD pipeline deploys the change live. The whole sequence runs autonomously overnight. Peter wakes up to a deployed site.

**Safety model (locked):** The Judge is the deployment gate, not Peter's manual diff review. Every non-`read_only` tool call hits the Judge per Q1 Decisions A+B. The $5/day spend cap, per-turn rejection breaker, and the actor's inner-loop circuit breaker remain in force. If Betty pushes something broken, Peter reverts the commit. Git history is the rollback mechanism. No pre-push human approval step exists in the loop — this is a deliberate departure from the "operator review before execute" pattern from the v2 kickoff Q5, which is now deferred entirely until after the win.

**Why this supersedes the prior Q7 lock:** Google Ads weekly report was the engineering-wisdom choice because it skips the Judge and proves the dispatcher cheaply. Travelpec.com deploy is the *business-wisdom* choice because it delivers a deployed asset Peter would otherwise build manually. The Q1 contract change (per-tool risk_class, adapter-populated, read-only Judge-skip) still gets validated — by the `read_file` / `list_directory` / `git_status` / `git_diff` tools in the new tool surface, which exercise the Judge-skip path naturally during the build. The first executor now also exercises `reversible_write` (write_file, git_commit_all) and `external_side_effect` (git_push) in the same run, which the prior Q7 selection couldn't do alone.

**Stuck-risk discipline:** If any scoping question in the remaining Q2–Q9 list does not load-bear for "Betty writes travelpec.com files, commits, pushes, Cloudflare deploys," it is deferred to a post-win phase. This applies to dispatcher abstraction (Q2), async patterns (Q3), Operator UI modality (Q4), dual-button governance (Q5), `judge_decisions` table beyond the minimum (Q6 — minimum kept because the Judge already writes verdicts and we need an audit trail), phase decomposition beyond 4.5+4.6 (Q8), and second-executor surface (Q9).

**Implication for Q9:** Second-executor stress test is deferred until after the travelpec.com win. The architectural goal — proving the dispatcher across read-only, reversible_write, and external_side_effect — is already satisfied by the travelpec.com tool surface in a single run. A second executor becomes a "scale to thelingerieshoppe.ca and the kPixies sites" follow-on, not a separate architectural phase.

---

## Operational boundaries (added 2026-05-24, locked)

These are not Q1 sub-decisions but constrain Phase 4.6 tool design and runtime behavior. They sit alongside the three boundaries captured under Q1.

4. **No media sourcing.** Betty does not attempt to find, fetch, generate, or embed photos or other media. When the build requires an image, Betty writes an HTML/Astro comment placeholder in the source: `<!-- IMAGE: short description, e.g. "Hero shot of Sandbanks at sunset" -->`. Peter swaps placeholders for real assets in a separate manual pass post-deploy. This boundary is reusable across all future site builds (lingerieshoppe.ca next) and removes an entire class of media-licensing, image-quality, and hallucinated-asset failure modes from Phase 4.6 scope.

5. **Autonomous deploy via `git push`, not human pre-review.** The Judge approves or rejects the push at envelope time; Peter does not gate the push manually. Safety net is `git revert` post-deploy. Implication: `git_push` is a Phase 4.6 tool with `risk_class="external_side_effect"`, fully Judge-gated, no operator-UI dependency. This is the load-bearing decision that lets the milestone run overnight without Peter at the keyboard.

6. **Ongoing autonomous site maintenance is in-scope post-launch.** Once travelpec.com is deployed via this pipeline, the same tool surface lets Betty make subsequent updates (copy edits, new pages, structural adjustments) autonomously. Phase 4.6's tool registry is designed for repeat use, not one-shot. lingerieshoppe.ca and the kPixies client sites become next-up beneficiaries of the same tool surface, no new architectural work required.

---

## In progress

**Scoping closed for Phase 4.5 + 4.6.** Q1 Decisions A+B locked. Q7 re-locked on travelpec.com autonomous deploy. Operational boundaries 1–6 captured. Everything else — Q2 (dispatcher abstraction), Q3 (sync/async), Q4 (UI modality), Q5 (dual-button governance), the parts of Q6 beyond the minimum audit-trail, Q8 (phase decomposition past 4.5+4.6), Q9 (second executor), authorization-freshness handling per Littlebird, and the `authorization` envelope sub-decision — is deferred to a post-win phase and logged in `OPEN_QUESTIONS.md`.

**Next:** Phase 4.5 + 4.6 execution kickoff (separate document, `phases/phase-4.5-4.6-execution-kickoff.md`). No further scoping rounds before implementation starts.
