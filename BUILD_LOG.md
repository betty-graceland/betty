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

