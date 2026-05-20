# Phase 4.4 Scoping Chat Kickoff — Universal Operator UI + Execution Dispatcher

## Role

You are the Lead AI Implementation Engineer and Pair Programmer. You held this role through Stages 1-3, Phases 4.1, 4.2, 4.3, and the Cleanup Phase.

This chat is scoping, not execution. Your output is an *architecture decision document* that the next chat will use as input for an execution kickoff. You write no production code in this chat. You may write throwaway code sketches to pressure-test design choices, but they live in chat, not in the repo. (See workflow rules below for the one exception: contract sketches that mature into specifications.)

Peter works in audio mode often and prefers dialogue-style exploration to verbose lists. Hard rule: do not suggest stopping work, taking a break, or closing the laptop. Do not offer unsolicited wellness advice or schedule management. Assume Peter is operating precisely when and how he intends to. Your job is to execute the next architectural step — which in this chat means thinking rigorously, not coding.

## Boundary

The Cleanup Phase closed at commit `7845519` on `origin/main`. Verify with:

```bash
cd ~/code/betty
git log --oneline -6
git status
```

The Cleanup Phase boundary at `7845519` must be intact — the four Cleanup Phase commits (`86ca1af`, `17bf70b`, `43ae4a3`, `7845519`) and the Phase 4.3 closure (`705b9cc`) beneath them are immutable history. The commit landing this kickoff document at `phases/phase-4.4-scoping-kickoff.md` will be visible somewhere above that boundary. Other commits between are fine and don't break the verification. What matters is that `7845519` exists in your history and that nothing has rewritten the commits below it.

If the boundary is not intact, stop and diagnose before any scoping work.

## Why this phase exists

Phase 4.3 wired the Judge as a financial and behavioral gate between the actor (Qwen) and tool execution. The actor can now propose a `draft_email` call, the Judge approves or rejects it, and on approval a proposal JSON file lands in `claw/proposals/<uuid>.json`. Phase 4.3's closure named two candidate surfaces for Phase 4.4: a send tool with AI-disclosure footer enforcement, and an operator review UI for `claw/proposals/`.

The Cleanup Phase paid down Phase 4.3's structural debt. With contracts cleaned up and `atomic_io` consolidated, the Phase 4.4 surface is now the right size of question to scope rigorously.

But Phase 4.3's closure framed both surfaces around email. **Email is a tracer bullet, not the artifact.** Peter's actual business use cases for Betty are:

- Website creation and management (Emdash)
- Google Ads reporting and campaign adjustments
- SEO initiative administration

The send tool / review UI scoping question is not "how does email work end to end." It is: **how does *any* real-world action get from Judge-approved proposal to executed effect, with operator review as the universal gate?**

This reframes Phase 4.4. The deliverable is not "email send tool + email review UI." It is a generalized architecture: a tool-agnostic operator UI that reads proposal JSON files, surfaces them for approval regardless of what kind of action they propose, and on approval dispatches the payload to the appropriate executor module (initially `email_send.py`, eventually `shopify_update.py`, `google_ads_adjust.py`, and others). Email is the first executor we build against this architecture, chosen because Phase 4.3 already shaped the proposal contract around it. Subsequent executors plug into the same dispatcher.

## Peter's working hypothesis (to pressure-test, not ratify)

Peter leans toward the **same-phase combined approach**: build the universal Operator UI and the email executor in the same phase, with the UI as the gate between Judge-approval and execution. The UI reads pending JSON proposals, displays them to Peter, and on Approve dispatches the payload to the corresponding execution tool. Email is first, but the dispatcher is generalized from day one so that adding Shopify or Google Ads later is an executor module plus a UI renderer, not a re-architecture.

The scoping chat's job is to pressure-test this hypothesis with rigorous architectural thinking. Either confirm it with sharper reasoning, or surface concerns that change it. Do not rubber-stamp.

## Open questions the scoping chat must answer

These are the architectural decisions that need to be made before any execution kickoff can be written. Work through them in roughly this order, but feel free to reorder if dependencies surface.

### 1. Proposal contract generalization

Currently, `draft_email` writes proposals with this shape:

```json
{
  "schema_version": 1,
  "call_id": "<uuid4>",
  "tool_name": "draft_email",
  "proposed_at": "<iso8601-utc>",
  "arguments": {"to": "...", "subject": "...", "body": "..."}
}
```

For a universal dispatcher, the proposal contract must accommodate any tool's payload. Questions to resolve:

- Does the existing schema generalize as-is (the `arguments` dict is already polymorphic, and `tool_name` is the dispatch key)? Or does the contract need a new field (e.g., `executor_module`) to decouple the proposal's tool identity from the dispatcher's execution path?
- How does `schema_version` evolve as new tools land? One global version, or per-tool versions inside `arguments`?
- Does the UI need a separate `display_hints` field for tool-specific rendering metadata (subject + body for email, before/after diff for Shopify, budget delta for Google Ads), or does the UI inspect `tool_name` and route to per-tool renderers?

### 2. Dispatcher architecture

The dispatcher is the module that takes "operator approved proposal X" as input and produces "real-world effect Y" as output. Questions:

- Where does it live in the codebase? A new `claw/betty_claw/dispatcher.py`? A subpackage `claw/betty_claw/executors/`?
- How does it discover available executors? Static registry (like `tools/__init__.py`'s `TOOLS` dict, mirrored), dynamic discovery (filesystem scan), or explicit registration in the executor module's `__init__`?
- What is the executor contract? A function `execute(proposal: dict) -> ExecutionResult`? A class with `validate()` + `execute()` + `rollback()` methods? The shape determines how much safety machinery is per-executor vs shared.
- What happens to the proposal file after execution? Delete-on-success, move to `claw/proposals/executed/`, append `execution_result` and leave in place, or write a separate audit log? The Stage 5 governance story — "AI took an action in the real world" — depends on this being unambiguous after the fact, especially for irreversible actions like email send.
- Are executions reversible, idempotent, both, or neither? Each real-world action has a different answer (email send is irreversible; Google Ads budget change is reversible-but-with-cost; Shopify product update is reversible). Does the dispatcher need different code paths for reversible vs irreversible, or does the executor module own that complexity?

### 3. Synchronous vs asynchronous execution

This is the question that determines whether Betty's actor loop is fundamentally changed by Phase 4.4 or not. It is upstream of modality choice (Q4) and downstream of dispatcher architecture (Q2).

- Does the UI block actor execution, or is it asynchronous? If the actor proposes a tool call, does the actor loop wait for operator approval before continuing, or does the actor return "proposed, awaiting review" immediately and Peter approves out-of-band?
- If synchronous: how does the actor surface the wait state? A polling loop on the proposal file? A blocking call into the UI module? What happens if Peter takes hours to approve — does the actor process sit idle, time out, or persist state and resume?
- If asynchronous: how does the actor know an approved proposal eventually executed? Does the executor write a result file the actor reads on next turn? Does the actor never know, and execution is purely Peter's domain after proposal?
- Does the answer differ by tool? Email might be fire-and-forget asynchronous; a Google Ads budget change might need synchronous confirmation before the actor reports outcome to its conversation partner.

### 4. Operator UI modality and notification

Once sync/async is decided, the modality of the UI is the next question.

- Web app served locally on the Mac Studio? CLI prompt loop? TUI? Mac-native app? Slack DM that surfaces proposals as messages with Approve/Reject buttons?
- How does Peter learn that a new proposal exists? Filesystem polling? File-watcher events? A notification mechanism? Pull-only (Peter checks when he wants to)?

**Implementation details deferred to execution chat:** what the UI shows for each proposal (raw JSON vs rendered preview vs both), and the exact rejection feedback mechanism (recorded on file, fed back to actor, terminal state with no learning loop). These are real questions, but they're downstream of the modality choice and don't constrain the architecture. The execution chat decides them as it builds.

### 5. Email executor specifics (the tracer bullet)

Once the dispatcher architecture is decided, the first executor — email — has its own scope:

- SMTP credentials: where do they live? `var/`? An environment variable? A new `~/.betty/secrets.json`?
- AI-disclosure footer: the Phase 4.3 closure named this as a Stage 5 architectural commitment. Footer text, where it lives, how it's appended (post-validation but pre-send, not bypassable from the actor surface), whether it varies by recipient domain.
- Email provider: a raw SMTP library, an API-based provider (SendGrid, Postmark, Resend), or Peter's own Gmail via OAuth? Each has different credential, deliverability, and trust implications.
- What gets sent: the email body verbatim, the email body + footer, the email body + footer + a "this was drafted by an AI" header? The send-time enforcement is the architectural commitment; the exact disclosure language is a sub-question.

### 6. Phase decomposition

Once the architecture is decided, how does it land as commits? Questions:

- Is "universal dispatcher + UI + email executor" one phase or two? Peter's lean is one phase. Pressure-test that against scope.
- What is the minimum viable architecture for Phase 4.4 vs what gets deferred? For example: could the UI be a CLI prompt loop initially and a richer interface in Phase 4.5? Could the dispatcher start as a hardcoded `if tool_name == "draft_email"` and generalize later, or is that exactly the same anti-pattern the Cleanup Phase paid down?
- What's the verification gate for Phase 4.4? The Cleanup Phase used eight self-tests with ~$0.39 of Anthropic spend. The next phase's gate will need to verify the UI, the dispatcher, the executor, and the actor's interaction with the now-async-pending state. What does that look like?

### 7. Surface area for future executors

The dispatcher is being designed with email as the first executor and Shopify, Google Ads, Emdash, SEO admin coming later. Questions to clarify *now*, even though they're not built yet:

- What are the rough proposal shapes for the future executors? (A Shopify product update proposal looks like *what*? A Google Ads budget change proposal looks like *what*?) Knowing the shape pressure-tests whether the dispatcher contract generalizes.
- What are the real-world-effect properties? (Reversibility, idempotency, cost-to-execute, time-to-execute, observability of effect.) These inform whether the dispatcher needs per-executor safety machinery or shared infrastructure.
- Are any of these executors close enough that "Phase 4.5 adds Shopify against the same dispatcher" is a credible next step, or are they all far enough out that the dispatcher only has email as a real consumer for a long time?

## Workflow rules for the scoping chat

- One question at a time. Resist the temptation to scope everything in one response. Each open question deserves dialogue, pressure-testing, and an explicit "here is the decision and here is why."
- Peter's lean is a hypothesis. If the scoping work surfaces a reason to change it, surface that reason explicitly and propose the alternative. Do not preserve Peter's lean for its own sake.
- Decisions get logged as they're made. The scoping chat's output is a markdown document that accumulates decisions in real time, not a single response at the end.
- No code in the repo. Sketches in chat are fine. If a sketch matures into a contract worth committing — a Protocol, a dataclass, a JSON schema — it lives in the kickoff document the scoping chat produces, as a fenced code block. It does not land in `claw/` until the execution chat opens.
- The scoping chat ends when the open questions above are all answered with explicit decisions, *and* when those decisions are organized into a kickoff document for the next chat to execute against.

## What success looks like

At scoping chat close, the next chat opens with:

1. A complete kickoff document modeled on the Cleanup Phase kickoff: role, boundary, locked decisions, implementation checklist, verification gate, what success looks like.
2. Locked decisions for every open question above (or explicit notes for any that are intentionally deferred to a later phase).
3. A first-commit-is description (what does Commit 1 of Phase 4.4 do?), to anchor the execution chat in concrete starting work.
4. A verification gate plan (what self-tests need to pass before each commit, what Anthropic API spend is expected, what the cost ceiling is).

The scoping chat is itself a phase, with the same discipline as a code phase: a clear boundary, a clear deliverable, an explicit close.

## Notes for the execution kickoff this scoping chat produces

The scoping chat's output is the Phase 4.4 *execution* kickoff. When writing that document, carry forward the following discipline from the Cleanup Phase BUILD_LOG closure:

- **Pyc-clear-before-gate discipline.** Phase 4.4 will add new modules (`dispatcher.py`, and possibly an `executors/` subpackage with `email_send.py`) and may rename or refactor existing files depending on how Q2's architecture lands. The Cleanup Phase incident log named stale `__pycache__` as a recurring source of transient test failures after file-structure changes. The Phase 4.4 execution kickoff must include `find . -name __pycache__ -type d -exec rm -rf {} +` (or equivalent) as a workflow rule before each verification gate run. Not optional, not "if tests fail try this" — a precondition.

Other discipline patterns from the Cleanup Phase (atomic commit boundaries, self-test verification gates, explicit cost ceilings, BUILD_LOG entries at phase close) carry forward by default. The pyc-clear rule gets named explicitly because it's the one that's specifically triggered by the kind of file-structure work Phase 4.4 will do.

## What to do when starting the scoping chat

1. Paste this kickoff document as the opening message.
2. Verify the boundary commit (`7845519`) is intact.
3. Discover the current repo structure. Run:

```bash
   cd ~/code/betty
   find . -maxdepth 3 -type d -not -path '*/\.*' -not -path '*/__pycache__*' | sort
   find claw/betty_claw -maxdepth 2 -name "*.py" | sort
   ls -la pyproject.toml ARCHITECTURE.md OPEN_QUESTIONS.md
   cat pyproject.toml
```

   The first find shows the directory tree to depth 3, excluding hidden directories and pycache. The second lists Python modules in betty_claw and immediate subpackages. The third confirms the foundational documents exist where the BUILD_LOG says they are. Reading pyproject.toml gives you the package's declared dependencies, entry points, and any tooling config relevant to where new modules can land.

   Do not assume what's in the repo from the BUILD_LOG, from this kickoff document, or from prior Claude context. The Cleanup Phase audit revealed three packages where the kickoff predicted one. Same discipline applies here: run the discovery, trust the discovery.

4. Read the Cleanup Phase BUILD_LOG entry (lines 833 onward in `~/code/betty/BUILD_LOG.md`) for context on the discipline patterns established by the prior phase.
5. Read Phase 4.3's closure (lines 531-832) for the architectural context this phase builds on, especially Phase 4.3's "Phase 4.4 opens with" section which scoped the candidate surfaces.
6. Begin with open question #1 (proposal contract generalization). The other questions depend on it, so resolving it first unblocks the rest.
