# MEMORY.md

## Current focus
Betty Stage 3: OpenClaw actor wiring. Markdown OS just scaffolded.
Read-path against OpenBrain (pgvector + Nomic 768d) verified at
sim=0.81 on test corpus.

## Locked architectural decisions
- Actor: betty-generalist (Qwen 3 14.8B Q4_K_M) via Ollama. ~37 tok/s,
  ~4s per typical turn. Predictable, no hidden thinking blocks.
- Reflector (Stage 9): betty-primary (Qwen 3.5 MoE 36B-A3B Q4_K_M).
  Kept warm but invoked rarely.
- Module layout: uv workspace with etl/ (OpenBrain ingest+retrieve)
  and openclaw/ (actor) as sibling packages.
- Markdown OS load order locked: AGENTS → USER → MEMORY. Reorder breaks
  Ollama KV prefix caching.

## Open threads
- Stage 3 remaining: Ollama client (httpx wrapper), actor loop
  (stitches Markdown OS + retrieval + Ollama).
- Stage 4 Judge adapter (Claude 3.5 Sonnet) not yet wired.
- Heartbeat (Stage 7) not yet wired.
- No email/calendar tools. Conversation is read-only.

## What Betty does not yet have
Calendar access. Email access. File writes. Web. Anything that touches
the outside world. Stage 3 is conversation against the OpenBrain only.
