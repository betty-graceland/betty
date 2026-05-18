# var/

Runtime mutable state for Betty. Not source code, not artifacts —
process state that survives across runs.

## What lives here

- `spend_ledger.json` — Daily Anthropic API spend tracking (Phase 4.3).
  Tracks cumulative USD spent on Judge calls within the current
  local-Toronto day. Rolls over at local midnight. Read/write logic
  in `claw/betty_claw/spend_ledger.py`.

Future Phase 4.3+ additions may include other runtime state files
(circuit-breaker state, session counters, etc.) — they land here.

## What does NOT live here

- Source code (lives in `claw/betty_claw/` or `etl/`).
- Tool proposals (live in `claw/proposals/`).
- Logs (no logging infrastructure yet; Stage 7).
- Markdown OS files (live in repo root: AGENTS.md, USER.md, MEMORY.md).

## Git tracking

This directory is tracked but its mutable contents are gitignored.
Only `README.md` and `.gitkeep` are committed. The directory exists
in fresh clones so write paths don't have to mkdir-if-not-exists
every time (though the spend_ledger code does anyway, defensively).

## Manual operations

To reset the spend ledger (e.g., the ledger file is corrupt and
blocking Judge calls):

    rm ~/code/betty/var/spend_ledger.json

Next Judge call will treat the missing file as "fresh" and start
a new zero-cost ledger for the current day.
