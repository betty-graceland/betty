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
