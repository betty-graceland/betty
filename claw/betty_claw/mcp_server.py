"""
Phase 4.7 — OpenClaw MCP server.

Exposes betty_claw tools as MCP tools for Hermes (the Brain) to consume.
This is the Executor half of the Brain/Executor split locked on 2026-05-27:
Hermes plans + orchestrates + remembers; OpenClaw executes + judges +
audits. Hermes connects to this server via stdio on startup and discovers
the tools.

Phase 0 scope (validate-the-seams gate)
=======================================
Only one tool is exposed: parse_airbnb_dossier. It is read_only, which
means no Judge round-trip is required to validate the MCP transport
itself. If Hermes can call parse_airbnb_dossier via MCP and get back a
valid Stays dict, the bridge contract is proven and Phase 1 unlocks.

Phase 1+ scope (after seams are proven)
=======================================
The full Phase 4.6 tool registry (emdash_*, write_file, git_*, etc.)
gets exposed as MCP tools, each wired through the Opus Judge based on
its declared risk_class. Skill markdown files get auto-generated from
TOOL_META so Hermes has documentation for when to call which tool.

Run via: uv run --directory /Users/betty/code/betty python -m betty_claw.mcp_server
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from betty_claw.tools.airbnb_parser import parse_airbnb_dossier as _parse_airbnb_dossier


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# MCP servers communicate over stdio, so we cannot write logs to stdout
# (that would corrupt the JSON-RPC stream). Send all logs to stderr; Hermes
# captures the subprocess stderr and surfaces it on connection issues.
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="[betty_claw.mcp_server] %(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("betty_claw.mcp_server")


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
# Server name "betty" — Hermes registers tools as mcp_betty_<tool_name>.
# So parse_airbnb_dossier becomes mcp_betty_parse_airbnb_dossier on the
# Hermes side.
mcp = FastMCP("betty")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def parse_airbnb_dossier(path: str) -> dict[str, Any]:
    """Parse an Airbnb research dossier into a Stays-compatible dict.

    Reads the dossier markdown at `path` (must resolve under BETTY_DOCS_DIR
    or BETTY_RESEARCH_DIR — environment variables set by Hermes via the
    mcp_servers env block). Returns the structured data Hermes/Qwen
    needs to drive a subsequent emdash_create_content_draft call.

    Args:
        path: Absolute or ~-prefixed path to the dossier .md file.

    Returns:
        Dict with four keys:
          - data: The Stays-shaped dict, validated against COLLECTION_SCHEMAS.
                  Ready to pass to emdash_create_content_draft.
          - frontmatter: Raw parsed YAML frontmatter for inspection.
          - body_excerpt: Cruft-stripped body text (~2000 chars).
          - summary: Human-readable one-line summary of the parsed result.

    Raises:
        ValueError on missing file, missing required frontmatter fields,
        or path outside the allow-list roots. MCP will surface the error
        message back to Hermes as a tool error.
    """
    logger.info("parse_airbnb_dossier called with path=%r", path)
    result = _parse_airbnb_dossier({"path": path})
    logger.info(
        "parse_airbnb_dossier returning: %s",
        result.payload.get("summary", "(no summary)"),
    )
    return result.payload


@mcp.tool()
def betty_ping() -> dict[str, str]:
    """Health-check tool. Hermes can call this to confirm the OpenClaw
    MCP server is up and the bridge is alive.

    Returns:
        A small dict with server name and status. No side effects.
    """
    logger.info("betty_ping called")
    return {
        "server": "betty",
        "status": "alive",
        "phase": "4.7.0 / Phase 0 prototype",
        "message": "OpenClaw MCP server is reachable from Hermes.",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server over stdio.

    Hermes spawns this as a subprocess (per ~/.hermes/config.yaml
    mcp_servers.betty) and communicates over stdin/stdout. We block here
    until Hermes shuts the subprocess down (typically at agent exit).
    """
    logger.info("OpenClaw MCP server starting (Phase 4.7 Phase 0 prototype)")
    logger.info("Exposing 2 tools: parse_airbnb_dossier, betty_ping")
    mcp.run()


if __name__ == "__main__":
    main()
