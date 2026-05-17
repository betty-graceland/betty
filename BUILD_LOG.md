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
