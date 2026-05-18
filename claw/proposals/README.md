# claw/proposals/

Runtime directory for tool proposal JSON files.

## What lives here

When Betty's actor decides to invoke a tool (e.g. `draft_email`), the tool
does NOT execute its real-world side effect. Instead, it writes a proposal
JSON file to this directory and returns `ToolResult(status="proposed")`.
The Judge (Phase 4.3+) then reads the proposal file and decides whether to
approve execution.

Proposal files are named `<call_id>.json` where `<call_id>` is a UUID4
generated at proposal time.

## Proposal JSON shape

```json
{
  "schema_version": 1,
  "call_id": "<uuid4>",
  "tool_name": "<tool-name>",
  "proposed_at": "<iso8601-utc>",
  "arguments": { ... tool-specific ... }
}
```

`schema_version` will bump when the shape changes (e.g. when Phase 4.3
adds a verdict block).

## Atomic writes

Tools write proposals atomically: `<call_id>.json.tmp` -> `fsync` ->
`os.replace` to `<call_id>.json`. The Judge cannot observe a partial-write
state. If you ever see a stray `.tmp` file in this directory, a tool
process crashed mid-write and the rename never happened. Safe to delete.

## Why this directory is tracked but its contents are ignored

The directory needs to exist in fresh clones so tools can write to it
without `mkdir` race conditions. The `.gitkeep` file accomplishes that.

The proposal files themselves are transient runtime state, not source.
They're gitignored via the rule in the repo-root `.gitignore`:
claw/proposals/*
!claw/proposals/.gitkeep
!claw/proposals/README.md

## Migration to Postgres

Stage 5 may migrate proposal storage from JSON-on-disk to a Postgres
table. When that happens, the `ToolResult.payload["proposal_path"]`
contract becomes `ToolResult.payload["proposal_id"]` (or similar) and
this directory becomes obsolete. See `ARCHITECTURE.md` for the long-form
plan.
