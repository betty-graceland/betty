# Betty — Build Log

Append-only execution journal. Most recent entries at the top. Each entry
captures what was built, what worked, what broke, and what was learned —
including operational quirks of this specific machine that won't be in
any documentation elsewhere.

---

## 2026-05-14 — Stage 1 Complete: Substrate

**Goal:** Stand up Postgres + pgvector, write Stage 1 schema, prove
end-to-end vector query works.

**Built:**
- Docker Desktop installed via Homebrew
- Postgres 16.13 + pgvector 0.8.2 running natively on aarch64 in container
- `docker/docker-compose.yml` with healthcheck, named volume, restart policy
- `docker/.env` with generated credentials (gitignored)
- Schema migration `001_init.sql`: schema_migrations, source_documents,
  source_document_chunks
- HNSW index on embedding column (vector_cosine_ops, m=16, ef_construction=64)
- `ops/schema/apply.sh` — idempotent migration runner

**Verified:**
- Inserted synthetic document + chunk with random 768-dim vector
- Cosine similarity query returned self_similarity = 1.0 (mathematically correct)
- Cascade delete on parent removed child chunk cleanly
- Both tables empty after cleanup

**Issues encountered and resolved:**
1. **Python 3.14 incompatibility.** Initial attempt would have used system
   Python 3.14.4, which doesn't have working `sentence-transformers` or
   `psycopg` wheels for Apple Silicon yet. Resolved by installing pyenv
   and pinning Python 3.12.13 for the project.
2. **pyenv init missing from `~/.zshrc`.** First `pyenv local` call failed
   because zsh didn't have pyenv on PATH or shims initialized. Resolved by
   appending the three-line pyenv init block (`PYENV_ROOT`, `PATH`, and
   `eval "$(pyenv init -)"`) to `~/.zshrc`.
3. **Docker required Rosetta on first launch.** Standard Apple Silicon
   first-time gotcha. Accepted via the GUI dialog.
4. **Heredoc for `apply.sh` silently didn't execute on first try.** Caught
   by `ls` verification before proceeding. Re-ran the heredoc and confirmed.
5. **Initial GitHub clone failed.** Repo URL was correct but returned
   "Repository not found" — likely a brief replication delay after repo
   creation. Retry succeeded.

**Machine-specific notes:**
- Container name: `betty-postgres`
- Host port: 5433 → container port 5432
- Postgres data volume: `betty-pgdata` (Docker-managed)
- Apple Silicon aarch64 confirmed via `SELECT version()`

**Commits:**
- `6c83f54` — initial repo
- `6debae2` — pin Python 3.12.13
- `78f7f6a` — Stage 1: Postgres + pgvector substrate
- `f344019` — add .env files to gitignore

**Next:** Stage 2 — Python ETL pipeline (pypdf, sentence-transformers, chunker, DB writer).

---


---

## 2026-05-17 — Stage 3 Read-Path Complete

**Goal:** Wire query-side embedding + cosine retrieval against the
OpenBrain pgvector substrate.

**Built:**
- `embed_query()` added to `embeddings.py` using Nomic's `search_query`
  prefix (registered centrally in `_load_model` so both embed paths
  share state via lru_cache)
- `retrieval.py` with `RetrievalHit` dataclass and `retrieve()` function
- Workspace/project/source_kind filters pushed into SQL WHERE clause so
  HNSW lookup honors tenant boundaries
- Char offsets extracted from chunk `metadata` jsonb (per Stage 2's
  schema decision to use jsonb instead of dedicated columns)

**Verified against "Attention Is All You Need":**
- "self-attention mechanism" → top hit chunk 7 at sim=0.8117
- "multi-head attention" → top hit chunk 15 at sim=0.7619
- "how do transformers handle long sequences" → top hit chunk 38 at sim=0.6742
- Three distinct top hits proves `search_query` prefix is wired correctly
- Char offsets populated and traceable back to source (chunk 7 at chars 5244-6144)

**Performance:** Retrieval is effectively free (~50ms dominated by
single Nomic forward pass for the query embedding). Actor latency will
dominate end-to-end response time once Stage 3 wires Qwen.

**Stage 3 remaining:** Ollama client, minimal Markdown OS, actor loop.

---

## 2026-05-17 — Stage 3 Complete: Actor Wiring

**Goal:** Wire betty-generalist as the routine actor, with retrieval
context delivered through a stable-prefix Markdown OS system prompt.

**Built:**
- `openclaw/` as workspace sibling to `etl/`, single shared .venv
- `betty_os/AGENTS.md`, `USER.md`, `MEMORY.md` — load order locked
  (immutable → stable → volatile) for Ollama KV prefix caching
- `ollama_client.py` — httpx wrapper around /api/chat with thinking
  content captured separately from visible response
- `actor.py` — actor_turn() stitches Markdown OS + retrieval + Ollama

**Model decisions locked:**
- Actor: betty-generalist (Qwen 3 14.8B Q4_K_M) ~34 tok/s sustained
- Reflector (Stage 9): betty-primary (Qwen 3.5 MoE 36B-A3B) parked

**Verified end-to-end on Attention paper query:**
- Retrieval surfaced 5 chunks from sim=0.77 down to 0.67
- Betty produced grounded summary citing source by document title
- Stayed in retrieved evidence (no hallucinated facts)
- Voice matched AGENTS.md constraints
- First-turn latency: 25.00s (1957 tokens in, 659 tokens out)

**Gotchas resolved:**
1. Qwen 3 reasoning consumed entire 100-token budget before producing
   visible content. Fix: raise DEFAULT_NUM_PREDICT to 1024 and capture
   thinking content into a separate field, not merged with response.
2. uv workspace required explicit `dependencies = [members]` at root
   to actually install members into shared venv.
3. Stage 2 ETL files had never been committed — entire pipeline
   existed only on local disk until this session.

**Next:** Stage 4 — Judge adapter (Claude 3.5 Sonnet) + draft_email tool.
# Betty Build Status — Stage 3 Complete
*Handback briefing dated 2026-05-17*

## TL;DR

Betty can now talk. The full read-think-respond loop is wired and verified end-to-end. Stages 1-3 are complete, committed, and reproducible from self-tests.

Stage 4 (Judge adapter + first action tool) is next.

## What Works Right Now

A natural-language question from Peter goes through:
1. `pipeline.ingest_file()` for new PDFs → checksum-idempotent storage in pgvector with HNSW indexing
2. `actor.actor_turn(question)` → retrieves top-K relevant chunks, assembles Markdown OS system prompt, calls betty-generalist via Ollama
3. Betty responds in her own voice, citing sources by document title, grounded in retrieved evidence

End-to-end latency: ~25s first turn against a substantive question (1957 tokens in, 659 tokens out, ~34 tok/s sustained).

## Architecture Locked-In (Stages 1-3)

**Substrate:** Postgres 16.13 + pgvector 0.8.2 in Docker on port 5433. HNSW index on `vector_cosine_ops`. Idempotency via UNIQUE on `checksum_sha256`. Chunk char offsets live in `metadata` jsonb, not as columns.

**ETL:** Python 3.12 via pyenv, uv workspace with `etl/` and `openclaw/` as sibling members, single shared `.venv` at workspace root. Hard-pinned deps. Nomic 768d embeddings with `search_document` / `search_query` prefix discipline.

**Actor:** betty-generalist (Qwen 3 14.8B Q4_K_M) via Ollama, accessed through httpx-based client. Markdown OS loaded in strict order — AGENTS → USER → MEMORY — for KV prefix caching. Retrieved context goes into user message, not system, to keep prefix stable across turns.

**Models locked:**
| Role | Model | Source |
|------|-------|--------|
| Actor (Stage 3+) | betty-generalist (Qwen 3 14.8B Q4_K_M) | Ollama, ~34 tok/s, ~25s first turn |
| Embedder | nomic-embed-text v1.5 (768d) | sentence-transformers in-process |
| Judge (Stage 4+) | Claude 3.5 Sonnet | Anthropic API — not yet wired |
| Reflector (Stage 9+) | betty-primary (Qwen 3.5 MoE 36B-A3B Q4) | Ollama, parked warm |

## Critical Gotchas Discovered in Stage 3

1. **Qwen 3 thinking tokens consume the budget.** Reasoning-capable models emit a separate `message.thinking` field. With default `num_predict=100`, the entire budget got consumed by reasoning before any visible content. Fix: raise default to 1024, capture thinking into `ChatResponse.thinking` separately from `.content`. Don't merge them — actor sees only `.content` so Betty's voice stays clean. Stage 9 reflector can use `.thinking` later.

2. **`/no_think` directive doesn't work for Qwen 3.5.** Tested explicitly. The model produces *more* tokens with the directive, not fewer. We can't suppress thinking; we budget for it.

3. **uv workspace requires explicit member dependencies at root.** `dependencies = []` makes the root a "virtual" project that doesn't install workspace members. Fix: declare `dependencies = ["betty-etl", "betty-openclaw"]` at root, plus `[tool.uv.sources]` block telling uv these resolve to the workspace.

4. **Stage 2 ETL files were never committed.** Entire pipeline existed only on local disk until this session. Build log instruction: every session, `git status` before closing.

5. **Schema-source-of-truth drift.** Earlier in Stage 2, work proceeded against a pasted version of `001_init.sql` that didn't match the live database. Real schema lives at `~/code/betty/ops/schema/001_init.sql`. Always `cat` the file rather than trusting in-conversation pastes.

6. **HNSW index uses `vector_cosine_ops`** (live schema), not `vector_ip_ops`. Retrieval uses `<=>` operator.

7. **Nomic empty prompts dict.** `nomic-ai/nomic-embed-text-v1.5` in sentence-transformers 3.3.1 ships with `model.prompts = {}`. Must manually register `search_document` and `search_query` after model load.

## Project Layout
~/code/betty/                           # uv workspace root
├── ARCHITECTURE.md
├── BUILD_LOG.md
├── OPEN_QUESTIONS.md
├── pyproject.toml                      # workspace declaration
├── uv.lock                             # shared lockfile
├── .venv/                              # single shared venv
├── .python-version                     # 3.12.13
├── docker/
│   ├── docker-compose.yml
│   └── .env                            # gitignored
├── ops/schema/
│   ├── 001_init.sql                    # the real schema, source of truth
│   └── apply.sh
├── etl/                                # workspace member: OpenBrain
│   ├── pyproject.toml
│   └── betty_etl/
│       ├── config.py
│       ├── chunking.py
│       ├── embeddings.py               # embed_chunks, embed_query
│       ├── db.py                       # pool, ingest_document
│       ├── pipeline.py                 # ingest_file → IngestResult
│       ├── retrieval.py                # retrieve → RetrievalHit
│       └── extractors/pdf.py
└── openclaw/                           # workspace member: actor
    ├── pyproject.toml
    └── betty_openclaw/
        ├── ollama_client.py            # httpx wrapper, /api/chat
        ├── actor.py                    # actor_turn()
        └── betty_os/
            ├── AGENTS.md               # identity, immutable
            ├── USER.md                 # operator profile, stable
            └── MEMORY.md               # volatile working set

## Verified Self-Tests

All runnable from workspace root via `uv run python -m ...`:
- `betty_etl.chunking` — 52 chunks from Attention paper, avg 878 chars
- `betty_etl.embeddings` — 768d vectors, L2 norm 1.0, Nomic on MPS
- `betty_etl.db` — pool ping, ingest, idempotency check
- `betty_etl.pipeline --self-test` — extract→chunk→embed→ingest, all 4 paths
- `betty_etl.retrieval` — top sim 0.81 on "self-attention mechanism"
- `betty_openclaw.ollama_client` — chat with thinking captured separately
- `betty_openclaw.actor` — full chain, Betty cites Attention paper by title

## Build Stage Sequence

1. Substrate ✓
2. ETL Skeleton ✓
3. Local Actor ✓
4. **Judge Adapter** ← next
5. Review Queue UI
6. REVISE Loop
7. Heartbeat
8. Work Records + Intent Parameters
9. Reflection Loop
# Side-Note for Gemini: Naming Collision Resolved

*Dated 2026-05-17, after Stage 3 closeout*

## What happened

After committing Stage 3 (commit dd7d660), Peter sent a Telegram message
intending to talk to "Betty." Telegram returned a response showing
🛠️ Exec tool calls referencing paths under `openclaw/workspace/travelpec-com`.

This caused brief confusion: the system we built tonight has no Telegram
bridge, no exec capability, no workspace directory, and no tool execution.
Initial concern was that an unauthorized agent was operating on real
project files.

## What it actually was

Peter had previously `npm install -g openclaw` — Mario Zechner's
agent framework. The Node process (PID 1093) had been running quietly
since Tuesday 9pm, configured with Telegram + tool execution + workspace
mappings pointing at his real repos.

When Peter typed "finish building travelpec.com" into Telegram, it went
to npm-openclaw — not to the Python betty_openclaw we built this session.
The two systems share only a name.

## Verified safe

- `git status` on travelpec-site: clean, no modifications
- `git log` shows latest commit from May 11 (Peter's own work)
- All file mtimes pre-date today
- Peter sent "Stop" before any exec actually fired
- The Node process logged "Agent was aborted" cleanly

No files were modified. No commits made. No damage of any kind.

Process has been killed (`kill 1093`).

## The naming problem this surfaces

Two systems on Peter's machine called "openclaw":

1. **npm-openclaw** — Mario Zechner's framework at
   `/opt/homebrew/lib/node_modules/openclaw/` with data in `~/.openclaw/`
2. **python-openclaw** — Our Stage 3 workspace member at
   `~/code/betty/openclaw/` (the betty_openclaw package)

The name "OpenClaw" in ARCHITECTURE.md was always *conceptual* — it
described the actor's role in the broader system, not a specific
product. The collision is accidental.

## Resolution chosen

Rename the Python package before Stage 4 begins, to eliminate the
collision permanently. The npm framework stays available if Peter ever
wants to use it for other purposes; our Python Betty gets a clean,
unambiguous name.

Likely rename: `betty_openclaw` → `betty_claw` or `betty_actor`.
Trivial refactor: rename the package, update imports in actor.py and
the workspace pyproject.toml, re-run self-tests, commit.

## What changes for Stage 4

Nothing functional. Stage 4's blueprint (Anthropic client → stub
tool → Judge wiring) is unchanged. The rename is a 10-minute warm-up
task at the start of the Stage 4 session, before the real work begins.

The architectural intent — Stage 4 introduces the *first external
action with a safety boundary* — remains exactly as drafted in the
Stage 3 handback.

## Meta-lesson for the build log

When standing up agent systems with tool execution, name collisions
between conceptual architecture and installed software are an actual
operational hazard. They look like they're working when they aren't,
or worse, look like they aren't working when they are. Worth a line
in OPEN_QUESTIONS.md: "Before installing any agent framework named
after architecture concepts (claw, brain, judge, etc.), verify no
name collision with planned Python packages."

# Stage 4 Kickoff — Locked Decisions

*Prepared 2026-05-17 after empirical Judge model comparison.*

## Context

Stages 1-3 complete and verified. Stage 4 introduces external action capability with a safety boundary: Betty proposes actions via Qwen 3 (local), and the Judge (Anthropic API) evaluates each proposal before execution. This is the first stage where external API spend is real and where failures have consequences beyond test output.

Repo: ~/code/betty/ — uv workspace, etl/ + claw/ as sibling members, single shared venv.

Final commit before Stage 4: 01c6db9 (rename openclaw to claw).

## Locked decisions

1. **API key storage:** ~/code/betty/.env (root-level, gitignored, mirrors docker/.env pattern). Loaded via python-dotenv.

2. **Tool calling protocol:** Ollama's native tools parameter for Qwen 3. The actor receives tool definitions and returns structured tool calls in the response; basic schema validation in actor.py before passing to Judge.

3. **Judge model:** claude-opus-4-7 — single-stage Judge.

   Rationale: empirically tested against Haiku 4.5 and Sonnet 4.6 on two scenarios (schedule_meeting with embedded date/timezone errors; publish_shopify_product_description with embedded regulatory/SEO/safety issues). Opus 4.7 caught the union of all concerns both smaller models flagged plus 4 additional real concerns neither smaller model surfaced, with zero hallucinations across both tests. Sonnet 4.6 hallucinated a day-of-week in test 1 (claimed May 27 was a Thursday — it is a Wednesday). Opus analysis included cross-context reasoning that connected proposed actions back to original user briefs (e.g., flagging keyword stuffing as inconsistent with Peter stated "design-forward, slightly aspirational" audience requirement). Cost per Judge call: ~$0.025, comparable to a two-stage Haiku+Sonnet pattern but with strictly broader coverage.

   Model string lives in .env as ANTHROPIC_JUDGE_MODEL so it can be swapped without code changes if production evidence later supports a different model.

4. **Verdict space:** approve/reject only. "Revise" is deferred to Stage 6 where the revise loop will actually exist to use it. YAGNI.

5. **Proposal storage:** JSON files at ~/code/betty/claw/proposals/<uuid>.json. Filesystem visibility is valuable for debugging the first LLM-to-LLM loop. Migrate to Postgres in Stage 5 when the review UI needs to query them.

6. **Circuit breaker (critical):** Hard cap of 3 Judge rejections per actor turn. Soft cap of $5.00/day cumulative Anthropic API spend. Either cap trips, halt and escalate. Daily spend ledger at ~/code/betty/claw/spend-ledger.json, rolled daily.

7. **Judge prompt:** String constant in judge.py. Code-coupled, not user-configurable. Not a Markdown OS file.

## Implementation order (locked from Gemini review: contracts before transport)

**Phase 4.1 — Foundations:**
- claw/betty_claw/types.py — ToolCall, ToolResult, JudgeVerdict, SpendLedger dataclasses
- claw/betty_claw/anthropic_client.py — httpx wrapper around Messages API, loads key from .env, returns structured response with token counts. Includes _self_test() that pings Claude with "Respond with the single word OK" and asserts content == "OK"

**Phase 4.2 — Stub tool:**
- claw/betty_claw/tools/__init__.py
- claw/betty_claw/tools/draft_email.py — does NOT send email. Returns ToolResult(status="proposed", ...) and writes proposal JSON to claw/proposals/<uuid>.json. Self-test: call with valid args, verify proposal file written.

**Phase 4.3 — Judge wiring:**
- claw/betty_claw/judge.py — Judge prompt as string constant; before_tool_call(tool_call, user_request) -> JudgeVerdict function; circuit breaker logic (3-rejection cap, spend ledger check); spend ledger read/write
- Modify claw/betty_claw/actor.py — detect tool calls in Qwen response (via Ollama tools parameter), route through judge before execution
- Self-test: two scenarios — (a) Betty drafts benign email, Judge approves, proposal file written; (b) Betty asked to do something obviously rejectable, Judge rejects with reasoning

## Models active in .env

ANTHROPIC_API_KEY=sk-ant-xxx
ANTHROPIC_JUDGE_MODEL=claude-opus-4-7
DAILY_SPEND_CAP_USD=5.00
TURN_REJECTION_CAP=3

## Critical safety properties Stage 4 must guarantee

1. No tool executes without Judge approval (the safety boundary is the whole point)
2. Daily API spend cannot exceed cap (the financial safety boundary)
3. Repeated rejections within one turn halt the loop (the runaway-loop boundary)
4. Spend ledger persists across restarts (otherwise cap is per-process, not per-day)
5. Failed Anthropic API calls (network, auth, deprecation) fail safe — tool does NOT execute on Judge failure

## Open questions for after Stage 4

- KV cache verification (deferred from Stage 3) — measure turn-2 latency with Markdown OS prefix unchanged
- Heartbeat latency budget (Stage 7) — 25s first-turn currently too long for 30-min tick
- Whether Opus 4.7 is overkill in practice; reassess after 50-100 real Judge calls

## What to do when starting the implementation chat

Start a fresh Claude chat. Paste this entire kickoff document. First message after: "Begin Phase 4.1. Implement types.py and anthropic_client.py per the locked decisions above."

Do not implement multiple phases in one message. Each phase gets implemented, tested, committed before the next phase begins. This is the rhythm Stages 1-3 used successfully.

## Stage 4.1 — Foundations (complete)

**Commits:** bd903b5, ee11a1f, ff0139c

### What landed
- `claw/pyproject.toml`: added `python-dotenv>=1.0.1`
- `claw/betty_claw/types.py`: four frozen dataclasses — `ToolCall`, `ToolResult`, `JudgeVerdict`, `SpendLedger`. Frozen enforcement verified via `FrozenInstanceError` on attempted mutation.
- `claw/betty_claw/anthropic_client.py`: httpx wrapper, mirrors `ollama_client.py` posture. Loads `.env` from repo root via path anchored to `__file__`. Two custom exceptions (`AnthropicAPIError`, `AnthropicResponseError`) under common `AnthropicClientError` base for fail-safe Judge semantics.

### Design notes worth preserving
- `call_id` (UUID4) is the universal join key across `ToolCall` -> `JudgeVerdict` -> `ToolResult` -> proposal JSON filename. Single identifier traces a proposal through the whole pipeline.
- Per-turn rejection counting was initially proposed inside `SpendLedger` and removed during design review. Transient per-turn state lives in-memory in the Judge instance; the ledger is dollars only.
- Opus 4.7 pricing constants: `INPUT_COST_PER_MTOK = 15.00`, `OUTPUT_COST_PER_MTOK = 75.00`. Verified to six decimals against live API response.

### Verified day-zero baselines
- First live Anthropic API call: `content='OK'`, 31 input tokens, 6 output tokens, **cost $0.000915**, `stop_reason=end_turn`, model resolved to `claude-opus-4-7`.
- Implied headroom: ~200 Judge calls at the kickoff's $0.025-per-call estimate before $5/day cap.

### Incident: model-string drift
Mid-session, a user message proposed swapping the Judge model from the kickoff-locked `claude-opus-4-7` to `claude-3-7-sonnet-20250219`. Assistant rejected the swap because (a) it contradicted the kickoff's empirical justification, (b) the proposed string is an older generation than the Sonnet 4.6 that already failed the day-of-week test that justified Opus 4.7. User confirmed the swap was a temporal continuity error. Kept Opus 4.7.

**Discipline preserved:** locked decisions in the kickoff document are the source of truth, even against in-session counter-suggestions. Future sessions: re-verify kickoff before swapping load-bearing components.

### Incident: API key leak (caught and remediated)
First `.env` write produced a malformed key with duplicated `sk-ant-` prefix. The malformed string was pasted into chat during verification. Even though malformed, the recoverable substring was treated as compromised. Key revoked on console.anthropic.com, fresh key provisioned, never echoed again. Length-check helper (`awk` over `~/.env`) added to verify key shape without revealing contents.

### Next session opens with Phase 4.2
- `claw/betty_claw/tools/__init__.py`
- `claw/betty_claw/tools/draft_email.py` — does NOT send. Returns `ToolResult(status="proposed", ...)`, writes proposal JSON to `~/code/betty/claw/proposals/<uuid>.json`.
- First end-to-end exercise of `ToolCall` -> `ToolResult` shapes with zero real-world side effect.

## Stage 5 Architectural Commitments

### AI-disclosure footer on outbound emails (logged Phase 4.2)

When Stage 5 or 6 wires real email execution (replacing the Phase 4.2 stub),
outbound emails sent by Betty MUST carry a footer that explicitly identifies
the message as AI-generated and disclaims promises/claims requiring human
verification.

Working footer text (refine in Stage 5):

  This message was sent by Betty, an AI agent acting on behalf of Peter
  Benes. Content may contain errors. Any commitments, promises, or factual
  claims should be verified directly with Peter before being relied upon.

Architectural properties:

1. The footer is appended at execution time in the send tool, NOT in the
   proposal. The proposal JSON captures the user-facing body; the footer
   is a property of the send operation. This keeps proposals reviewable
   as the message-the-actor-intended-to-send.

2. The footer is not optional and not bypassable by the actor. It is
   hardcoded into the send tool, not passed as an argument. Qwen cannot
   suppress it via prompt manipulation, tool-call argument injection, or
   any other channel reachable from the actor surface.

3. The Judge prompt (Phase 4.3) must be aware the footer will be
   auto-appended at execution time, so it does NOT reject proposals for
   "missing AI disclosure" when the disclosure is added downstream. The
   Judge evaluates the body as-proposed; the footer is a guaranteed
   downstream invariant outside the Judge's verdict scope.

No code changes in Phase 4.2. This is a logged decision to prevent
re-litigation in future sessions.

## Phase 4.2 closed — first stub tool with proposal contract

Commits: `17844e3` (draft_email), `61d58b0` (registry), `39e225f` (proposals/ infra)

### What landed
- `claw/betty_claw/tools/__init__.py` — tool registry. Exposes `TOOLS` dict
  and `get_tool(name)` accessor with diagnostic KeyError.
- `claw/betty_claw/tools/draft_email.py` — stub tool. Validates arguments
  strictly, writes proposal JSON atomically, returns
  `ToolResult(status="proposed")`. Does NOT send.
- `claw/proposals/` — runtime storage directory. Tracked via `.gitkeep`
  and `README.md`; contents gitignored.

### Verified live
- Self-test PASS from cold invocation. Happy path + 4 validation failures
  (missing key, extra key, wrong type, empty string) all behave per
  contract.
- Snapshot-diff assertion confirms "validate before UUID, validate before
  disk" property: exactly 1 new file in proposals/ after 1 happy path + 4
  validation failures.
- `git check-ignore` confirms ignore-pattern correctness: wildcard catches
  `.json` and `.tmp`; negations un-ignore `.gitkeep` and `README.md`.
- Registry smoke test: `TOOLS.keys() == ['draft_email']`, `get_tool` returns
  callable, `KeyError` diagnostic produces designed message.
- Phase 4.2 incurred zero Anthropic API spend — first Judge call lands in
  Phase 4.3. Day-zero baseline from Phase 4.1 stands at $0.000915.

### Locked decisions (carry forward)
- Proposal JSON shape includes `schema_version` (starts at 1). Bump when
  Phase 4.3 adds verdict fields; do not silently change shape.
- `ToolResult.payload = {"proposal_path": "<absolute>"}`. Absolute path,
  not relative — the Judge may run from a different CWD than the actor.
- Atomic write pattern: tmpfile + fsync + os.replace. Required (not
  optional) for the actor-writes-then-Judge-reads seam in Phase 4.3.
- Strict validation: reject unknown keys, reject non-string values, reject
  empty strings. No silent coercion. Forward-compat for new fields like
  `cc` is handled by bumping `schema_version` in the same commit that adds
  the field.
- Validation occurs BEFORE call_id generation and BEFORE any disk write.
  The proposals directory answers "did this tool attempt to run?" from
  filesystem state alone. Enforced by snapshot-diff in self-test.
- Tool registry lives in `tools/__init__.py`, not `actor.py`. Eager imports.
  Lazy loading deferred until measured evidence of slow imports.

### Operator notes
- Self-test command: `uv run python -m betty_claw.tools.draft_email`.
- Python may emit `RuntimeWarning: 'betty_claw.tools.draft_email' found in
  sys.modules after import of package 'betty_claw.tools'` — expected
  because `tools/__init__.py` eagerly imports the module and `-m`
  re-imports on invocation. Harmless. The PASS message is the source of
  truth.
- Stray `.tmp` files in `claw/proposals/` indicate a crashed tool process
  mid-atomic-write. Safe to delete; the rename never happened so no
  partial-state was visible to a Judge.

### Incidents
- None this phase.

### Phase 4.3 opens with
- `claw/betty_claw/judge.py` — Judge module. Reads proposal JSON, calls
  Opus 4.7 via `anthropic_client`, returns `JudgeVerdict(approve|reject)`.
- Spend-ledger persistence: where on disk, atomic writes, restart-safety.
- Circuit-breaker logic: 3 rejections/turn (in-memory in Judge instance),
  $5.00/day Anthropic spend cap (persisted in ledger).
- `actor.py` modifications: dispatch through registry, route proposals
  through Judge, handle approve/reject, write ToolResult.status.
- Judge prompt: string constant in `judge.py`. Aware of the Stage 5
  footer-auto-append commitment — do NOT reject proposals for missing
  AI disclosure.

## Phase 4.3 closed — Judge wired, actor inner loop, full safety stack

Commits: `916f600` (atomic_io), `5c461f3` (var/ infra), `0b805d8` (spend_ledger), `f88c074` (Judge), `b160c6d` (OllamaClient tool-calling extension), `1f70bc0` (tool registry schema export), `3d09cf6` (actor.py inner loop)

### What landed

- `claw/betty_claw/atomic_io.py` — shared atomic JSON write utility. Factored
  at the second use site (rule of three played correctly). tmpfile + fsync +
  os.replace, with `except BaseException` cleanup to defend against
  KeyboardInterrupt mid-write.
- `var/` — new repo-root directory for runtime mutable state. README.md and
  .gitkeep tracked; contents gitignored. Matches `claw/proposals/` pattern.
- `claw/betty_claw/spend_ledger.py` — daily Anthropic spend ledger.
  Persistent at `var/spend_ledger.json`. `load()` returns
  `LedgerResult(status: ok|fresh|corrupt, ledger, corruption_reason)`.
  `check()` is a pure function; `record()` is read-modify-write with no
  caching across calls. Fail-loud on corruption.
- `claw/betty_claw/judge.py` — the Judge. `Judge.before_tool_call()` consults
  the ledger, estimates cost, calls Opus 4.7 via the Phase 4.1 client,
  parses verdict, returns `JudgeVerdict`. `reset_turn()` zeros the in-memory
  per-turn rejection counter. Lenient JSON parsing (strict first, regex
  fallback for the first `{...}` block).
- `claw/betty_claw/ollama_client.py` — extended for native tool-calling.
  `ChatMessage` supports `tool_calls` and `role='tool'`. `ChatResponse`
  surfaces parsed `tool_calls`. `chat()` accepts optional `tools=` schemas
  passthrough. Stage 3 callers that omit the new parameters get identical
  behavior.
- `claw/betty_claw/tools/__init__.py` — extended with `ToolEntry` dataclass
  pairing callable and schema. New `get_ollama_tools_schema()` returns
  schemas in sorted order for KV cache stability. Phase 4.2's `TOOLS`
  shape changed; verified no consumers existed before refactoring.
- `claw/betty_claw/tools/__main__.py` — new file. Required for
  `python -m betty_claw.tools` to execute (packages need `__main__.py`
  for `-m`, unlike modules).
- `claw/betty_claw/tools/draft_email.py` — `DRAFT_EMAIL_SCHEMA` module
  constant added. Schema's required-fields and types mirror the existing
  strict validator. Tool body unchanged.
- `claw/betty_claw/actor.py` — Stage 3 actor extended with the inner loop.
  `ActorTurn` gains `outcome`, `proposal_path`, `judge_verdicts`,
  `iterations` fields with defaults. `actor_turn()` accepts optional
  `judge: Judge | None`. When None, Stage 3 single-call behavior preserved
  exactly. When supplied, the bounded inner loop activates.

### Verified live

All seven modules have self-tests. The substantive ones:

- **judge.py**: 6 scenarios PASSED. Real-API verdicts: faithful email
  approved ($0.0203); phishing email rejected with concrete reasoning
  ($0.0275); 2-rejection breaker trips and 3rd call short-circuits at $0;
  `reset_turn()` clears the counter; near-cap ledger refuses without API
  hit; corrupt ledger refuses without API hit. Total Judge self-test cost:
  $0.1157 across 5 Anthropic calls.
- **ollama_client.py**: 3 scenarios PASSED against local Qwen 3 14B.
  Stage 3 baseline preserved (text response, zero tool_calls). Tool-calling
  produces parseable `tool_calls` with correct name and arguments.
  `role='tool'` rejection feedback accepted by Ollama; Qwen retried with
  corrected tool call.
- **actor.py**: 3 scenarios PASSED end-to-end. Scenario A — Stage 3 text
  path (no Judge, retrieval ran, outcome='text'). Scenario B — approve
  path (real Judge + real Qwen, draft_email proposal written to disk,
  outcome='tool_approved' at $0.0195). Scenario C — reject-loop + breaker
  trip with MockJudge (3 rejections, third has cost_usd == 0.0,
  outcome='breaker_tripped', iterations=3, structural cost-zero detection
  worked under adversarial Judge).

Total Phase 4.3 self-test Anthropic spend: roughly $0.14.

### Day-zero cost baselines (carry forward)

- Phase 4.1: bare ping = **$0.000915** (31 input + 6 output tokens).
- Phase 4.3: full Judge call = **~$0.023** average over the Judge
  self-test's 5 paid calls ($0.0203, $0.0275, $0.0243, $0.0231, $0.0205).
  Implied headroom at $5.00/day cap: roughly 215 Judge calls.

### Safety properties — all five Stage 4 properties satisfied

- **#1 No tool executes without Judge approval** (satisfied Phase 4.2; tools
  return `status='proposed'` only). Phase 4.3 confirms: actor only returns
  `outcome='tool_approved'` after `verdict.decision == 'approve'`.
- **#2 Daily spend cap enforced before API call.** Pre-call conservative
  estimate ($0.0375 = max_tokens 500 * output rate $75/Mtok) gated against
  `spend_ledger.check()`. Verified live in Scenario 5 of judge self-test:
  near-cap ledger refused without API hit, `cost=$0.0`.
- **#3 Repeated rejections halt the loop.** In-memory counter on Judge
  instance; `reset_turn()` at turn boundary; counter increments on
  substantive rejects AND on API/parse failures. Verified in Scenario 3
  of judge self-test (real Opus rejections) and Scenario C of actor
  self-test (MockJudge adversarial rejections, structural `cost_usd == 0.0`
  detection).
- **#4 Spend ledger persists across restarts.** Atomic writes via
  atomic_io.atomic_write_json; corruption fail-loud via `LedgerResult.status`;
  internal-consistency b2 validator (`cumulative_cost_usd == sum(entries)`
  within 1e-9 tolerance) catches drift at load. Verified across 10
  scenarios in spend_ledger self-test.
- **#5 Failed Anthropic calls fail safe.** `AnthropicAPIError`,
  `AnthropicResponseError`, and malformed verdict JSON all route through
  Judge's `_reject()` with diagnostic reasoning. Counter increments. No
  tool executes on Judge failure. Verified by composition (Judge calls the
  Phase 4.1 client which raises these exceptions; Judge's `_reject()` path
  is exercised by the cap-refused and corrupt-ledger scenarios).

### Locked decisions (carry forward)

- **Day boundary for spend cap**: local Toronto midnight via
  `ZoneInfo("America/Toronto")`. DST transitions produce one 23-hour and
  one 25-hour day per year. Accepted as known property, not incidents.
- **Spend ledger location**: `var/spend_ledger.json`, NOT
  `claw/spend-ledger.json`. The types.py docstring's path reference is
  stale; left unmodified to preserve Phase 4.1 closure seal (see Deferred
  cleanup below).
- **Internal-consistency validator pattern**: option (b2) from design
  discussion — validate in `spend_ledger.load()`, not in types.py. Same
  discipline rationale as #2 above. Drift between `cumulative_cost_usd`
  and `sum(entries)` routes to `status='corrupt'`.
- **JudgeVerdict.call_id = ToolCall.call_id** (the tool's UUID4), not the
  Anthropic msg_id. The Phase 4.1 `AnthropicResponse` does not surface
  msg_id; reaching back to add it would violate the Phase 4.1 seal.
  Anthropic-bill cross-reference is available via timestamps on ledger
  entries.
- **Cost estimation**: conservative worst-case `max_tokens * output_rate
  = $0.0375/call`. Input token cost is recorded post-call from actual
  usage, never pre-estimated. Pre-check is a gating estimate, not an
  accounting figure.
- **API failures count toward the rejection breaker.** A Judge that's
  failing repeatedly is itself halt-worthy.
- **Verdict parsing is lenient** (strict `json.loads` first, regex fallback
  for first `{...}` block). The Judge system prompt instructs Opus to
  emit pure JSON, but enforcement is belt-and-braces.
- **Tool dispatch translates wire-shape ToolCall to types.ToolCall** in
  the actor. Two `ToolCall` classes exist by design: one in
  `ollama_client.py` (what Ollama emits) and one in `types.py` (what the
  Judge sees, carrying UUID4 call_id). The actor is the translation
  boundary.
- **Terminal-vs-substantive rejection is STRUCTURAL**: `verdict.cost_usd
  == 0.0` distinguishes no-API short-circuit (breaker/cap/corrupt, halt)
  from substantive reject (cost > 0, feed back and loop). String matching
  on `verdict.reasoning` is used only for outcome label classification,
  never for control flow.
- **Inner loop bounded by `max_iterations = rejection_limit + 1`** as
  defense in depth. In practice the Judge's breaker trips first.

### Incidents

**Discipline preserved: types.py stdlib shadow surfaced under direct-script execution.**
While developing atomic_io.py, the run plan deviated from the documented
`uv run python -m betty_claw.<module>` invocation and used direct-script
execution (`uv run python claw/betty_claw/atomic_io.py`). This put
`claw/betty_claw/` first on `sys.path`, causing stdlib `json` -> `re` ->
`enum`'s transitive `import types` to resolve to our Phase 4.1
`claw/betty_claw/types.py` instead of stdlib `types`. ImportError fired
on circular import.

Three fix options surfaced: (1) rename `types.py` to `contracts.py`
(structural fix, touches Phase 4.1 closed code), (2) standardize on `-m`
invocation (workaround, preserves Phase 4.1 seal), (3) move self-tests to
a separate tests/ directory (architectural change out of scope).

Initial assistant lean was Option 1, framed as a "Phase 4.1 bug fix."
User pushed back: framing as a bug fix is plausible but weakens the
phase-closure discipline; once "reach back if framed as bug fix" is
accepted, the seal on closed phases becomes negotiable. Option 2 was
not actually a change — it was already the documented invocation
pattern. The deviation was the run plan, not the codebase.

**Resolution**: Option 2. Rename deferred. Cost of leaving it: zero
under documented usage. Cost of renaming: reopen Phase 4.1, update every
importer (anthropic_client, draft_email, tools/__init__.py), full
re-verification pass. Cost-benefit favored deferring.

**Meta-lesson**: locked phase boundaries are the source of truth for
what may and may not be reopened. Framing a Phase 4.3 surface concern
as a Phase 4.1 bug fix is rhetorical drift, not a discipline.

**Known latent footgun: stdlib name shadow.**
`claw/betty_claw/types.py` shadows the Python stdlib `types` module
under direct-script execution. Documented invocation pattern
(`uv run python -m betty_claw.<module>` from workspace root) dodges it.
Anyone running scripts directly will hit ImportError. Fix: use `-m` from
workspace root. Structural fix (rename to `contracts.py`) deferred to a
future cleanup phase that would also handle the related deferred items
below.

**.gitignore var/ collision.**
The Phase 4.3 var/ runtime directory commit landed broken on first
attempt because the existing `.gitignore` had `var/` in the standard
Python `Distribution / packaging` block (template artifact, irrelevant
to this project). The new `var/* + !var/README.md + !var/.gitkeep`
allow-list at the bottom of `.gitignore` was silently overridden by the
upper-block `var/`. Git showed "The following paths are ignored" and
the commit landed with only 5 insertions (the new gitignore lines) and
NO `var/README.md` or `var/.gitkeep`.

**Resolution**: `git reset --soft HEAD~1` to undo the bad commit while
keeping changes staged; targeted sed surgery to remove the upper-block
`var/` line; re-commit with both `.gitignore` edits and the var/ files.
Local-only history rewrite, no force-push needed (commit hadn't been
pushed).

**Meta-lesson**: read `git commit` output carefully. The
"file changed, 5 insertions" line was the tell; 5 insertions for what
should have been a multi-file infrastructure change was an obvious red
flag that I caught only on the verification step.

**ToolResult.call_id contract mistake.**
In the first draft of actor.py, the code read
`call_id=tool_result.payload["call_id"]`. Self-test Scenario B failed
with `KeyError: 'call_id'`. Root cause: `ToolResult.call_id` is a
top-level field (Phase 4.1 contract), not inside `payload`. `payload`
contains only `{"proposal_path": ...}`. I'd conflated where each piece
of information lives.

**Resolution**: one-line sed fix. Same class of bug as the
`cumulative_cost_usd` field assumption that the b2 discussion caught
earlier in the phase.

**Meta-lesson**: the discipline of `grep -B 2 -A 15 "class X"` before
writing code that touches a contract is non-negotiable. It catches
this exact class of bug. I did it for `SpendLedger`, `JudgeVerdict`,
`ToolCall`, and `AnthropicClient` — but not for `ToolResult`, and it
bit me. Always grep.

**External review caught coverage gap (Scenario C).**
Initial actor.py self-test design proposed two scenarios: approve path
and text path, with the reject-loop's correctness verified "by
composition" (Judge self-test scenario 3 exercises the breaker;
OllamaClient self-test scenario 3 exercises role='tool' feedback).
Gemini reviewed and pushed back: the inner loop's `while`-bound on
rejections is the load-bearing financial safety mechanism, and a bug
there could infinite-loop and burn the API budget. Composition coverage
isn't sufficient when the gap is the integration glue itself.

**Resolution**: added MockJudge (duck-typed, not a Judge subclass) that
mirrors the real Judge's two rejection modes (substantive: cost > 0,
short-circuit: cost == 0). Scenario C asserts the actor halts at
`outcome='breaker_tripped'` after exactly 3 iterations with structural
cost_usd == 0.0 detection on the third verdict.

**Meta-lesson**: composition coverage of a load-bearing safety mechanism
is a rationalization to leave it untested, not a reason. When external
review catches this, accept the pushback.

### Deferred cleanup (carry forward to a future cleanup phase)

These are known imperfections that we explicitly chose not to fix in
Phase 4.3 to preserve the Phase 4.1 and Phase 4.2 closure seals. A
future cleanup phase should address them together since they're all
the same category of debt.

- Rename `claw/betty_claw/types.py` to `contracts.py` (or similar) to
  eliminate the stdlib shadow.
- Update the stale docstring path reference in `types.py`'s
  `SpendLedger` (`~/code/betty/claw/spend-ledger.json` -> actual
  `~/code/betty/var/spend_ledger.json`).
- Migrate `claw/betty_claw/tools/draft_email.py`'s inline atomic write
  to use `claw/betty_claw/atomic_io.py`'s shared utility. Phase 4.3
  introduced atomic_io.py as the second use site; the cleanup phase
  becomes the third-site migration consolidating both prior sites.
- Consider whether the `tools/` package's executable `__main__.py`
  pattern should be applied retroactively to other packages, or whether
  it stays unique to the registry.

### Operator notes

- Run all self-tests via `uv run python -m betty_claw.<module>` from
  workspace root. Direct-script execution hits the types.py shadow.
- Self-test order if running the full suite in sequence: atomic_io,
  spend_ledger, anthropic_client (Phase 4.1), tools, ollama_client,
  draft_email, judge, actor. Each is independent and writes to
  `/tmp/betty_*_selftest` directories that are cleaned up on success.
- `var/spend_ledger.json` accumulates real spend data across sessions.
  To reset: `rm ~/code/betty/var/spend_ledger.json`. Next Judge call
  will treat the missing file as `fresh` and start a new zero-cost
  ledger for the current day.
- A corrupt ledger blocks Judge operation by design (Property #5
  fail-safe). To recover: delete the file and the Judge resumes on
  the next call.
- `claw/proposals/*.json` accumulates from approved tool calls.
  Phase 4.3 doesn't yet have proposal cleanup; future operator-facing
  review UI (Stage 5+) will handle this. Manual `rm` is safe.

### Phase 4.4 opens with

Phase 4.4 has not been scoped at kickoff time. Candidate work surfaces
include:

- **Send tool with AI-disclosure footer enforcement** (the Stage 5
  Architectural Commitment). Replaces `draft_email` proposal-only
  behavior with actual SMTP send, footer auto-appended at send time,
  not bypassable from the actor surface.
- **Operator review UI**: surface pending proposals at
  `claw/proposals/` for human approval before execution. The Phase 4.3
  approve verdict from Opus is necessary but not sufficient — Peter is
  the final reviewer for any real-world action.
- **Cleanup phase**: address the deferred items listed above.
  Recommended before Phase 4.4 work that would add new tools or expand
  the registry, because new tools would inherit the types.py shadow
  and the second inline-atomic-write site is now the only barrier to
  three-site migration becoming four-site.

The kickoff for Phase 4.4 (or Phase 5.0, depending on scoping) should
include a fresh architectural review of where the Stage 5 send tool
slots in relative to the operator review UI — they may be the same
work or sequential phases.
