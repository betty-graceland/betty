"""
Phase 4.7 — OpenClaw MCP server (Pattern B multi-site, locked 2026-05-31).

Exposes betty_claw tools as MCP tools for Hermes (the Brain) to consume.
This is the Executor half of the Brain/Executor split locked on 2026-05-27:
Hermes plans + orchestrates + remembers; OpenClaw executes + judges +
audits. Hermes connects to this server via stdio on startup and discovers
the tools.

PATTERN B — MULTI-SITE TOOL CONTRACT
====================================
Every tool exposed by this server takes `site` (a string slug) as its
first parameter. The server uses site_config.load_site_config(site) to
resolve site-specific state — read/write allow-list roots, EmDash MCP
URL+token, git branch policy, collection schemas, parser fixed_fields,
voice doc path, hard rules — and passes the relevant pieces to the
underlying betty_claw tool function.

This means adding a new website Betty operates on requires only:
  1. Setting up the site's external infrastructure (EmDash, Cloudflare,
     GitHub repo, Astro project, voice doc).
  2. Dropping a new YAML config in ~/.betty/sites/{site_id}.yaml.
  3. Adding the EmDash token to the MCP server env.
  4. Restarting Hermes.

No Python edits required. See ~/.betty/sites/_README.md for the schema.

PHASE 0 SCOPE
=============
Two tools exposed (plus the no-site list_sites helper):
  - parse_airbnb_dossier(site, path) — read_only, no Judge needed
  - betty_ping(site)                 — read_only health check
  - list_sites()                     — no site param; reports registry

If Hermes (Qwen) can call parse_airbnb_dossier via MCP and get back a
valid Stays dict for the named site, Phase 0 passes and Phase 1 unlocks.

PHASE 1+ SCOPE
==============
The full Phase 4.6 tool registry (emdash_*, write_file, git_*, etc.) gets
exposed as MCP tools, each wired through the Opus Judge based on its
declared risk_class. All take `site` first; each resolves the relevant
slice of site config. Skill markdown files get auto-generated from
TOOL_META so Hermes has per-site documentation for when to call which
tool.

Run via:
  uv run --directory /Users/betty/code/betty python -m betty_claw.mcp_server
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP

from betty_claw.emdash_client import EmdashClient
from betty_claw.site_config import (
    SiteConfig,
    list_available_sites,
    load_site_config,
    site_summary,
)
from betty_claw.tools.airbnb_parser import (
    parse_airbnb_dossier as _parse_airbnb_dossier,
)
from betty_claw.tools.emdash_reads import (
    emdash_get_collection_schema as _emdash_get_collection_schema,
    emdash_get_content as _emdash_get_content,
    emdash_list_collections as _emdash_list_collections,
    emdash_list_content as _emdash_list_content,
    emdash_list_taxonomies as _emdash_list_taxonomies,
    emdash_list_taxonomy_terms as _emdash_list_taxonomy_terms,
)
from betty_claw.tools.filesystem import (
    list_directory as _list_directory,
    read_file as _read_file,
)


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
# Hermes side; list_sites becomes mcp_betty_list_sites.
mcp = FastMCP("betty")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_sites() -> dict[str, Any]:
    """List every site this OpenClaw server knows about.

    Hermes should call this when she's deciding which site to work on, or
    when she needs to know what sites she can choose from for a multi-site
    task. The response includes status (in_progress / live / archived),
    collections available, parsers available, and the count of hard rules.

    Paths, EmDash URLs, and tokens are NOT surfaced — those are server-side
    plumbing. If Hermes needs domain or collection details, she has them
    here; if she needs more, she calls a per-site read tool.

    Returns:
        Dict with a `sites` key containing a list of site summaries, plus
        a `count` for convenience. No side effects, always safe to call.
    """
    logger.info("list_sites called")
    site_ids = list_available_sites()
    summaries = []
    for site_id in site_ids:
        try:
            config = load_site_config(site_id)
            summaries.append(site_summary(config))
        except ValueError as e:
            # Surface the bad config rather than silently skipping it —
            # otherwise operators can't tell why a site doesn't show up.
            summaries.append({
                "id": site_id,
                "status": "config_error",
                "error": str(e),
            })
    logger.info("list_sites returning %d sites", len(summaries))
    return {"sites": summaries, "count": len(summaries)}


@mcp.tool()
def betty_ping(site: str) -> dict[str, str]:
    """Health-check tool. Hermes can call this to confirm the OpenClaw MCP
    server is up, that the bridge is alive, AND that the named site's
    config loads cleanly.

    Calling betty_ping('travelpec') is the canonical pre-flight before any
    multi-tool task on travelpec: a green ping means the server is reachable
    and the site config validates. A red ping points the operator at either
    the MCP subprocess or the site YAML.

    Args:
        site: site_id slug (e.g., "travelpec"). Must match a YAML in
            ~/.betty/sites/. Use list_sites() to discover what's available.

    Returns:
        Small dict with server name, site status, and a human-readable
        message. No side effects.
    """
    logger.info("betty_ping called with site=%r", site)
    try:
        config = load_site_config(site)
    except ValueError as e:
        # Don't crash — return a structured error so Hermes can act on it.
        # Pattern B contract: malformed site config is a user-fixable
        # problem, not an MCP transport problem.
        return {
            "server": "betty",
            "status": "site_config_error",
            "site": site,
            "phase": "4.7.0 / Phase 0 prototype",
            "error": str(e),
        }
    return {
        "server": "betty",
        "status": "alive",
        "site": config.id,
        "site_status": config.status,
        "domain": config.domain,
        "phase": "4.7.0 / Phase 0 prototype",
        "message": (
            f"OpenClaw MCP server reachable; site {config.id!r} "
            f"({config.domain}) config loaded cleanly."
        ),
    }


@mcp.tool()
def parse_airbnb_dossier(site: str, path: str) -> dict[str, Any]:
    """Parse an Airbnb research dossier into a Stays-compatible dict for
    the named site.

    Reads the dossier markdown at `path`, which must resolve under one of
    the site's read-allowed roots (paths.astro / paths.docs / paths.research
    in the site's YAML). Returns the structured data Hermes/Qwen needs to
    drive a subsequent emdash_create_content_draft call.

    Site-level fixed_fields (e.g., provider='airbnb', is_advertised=0)
    come from the site config's parsers.airbnb_dossier.fixed_fields block
    and overwrite anything the dossier might claim — site config wins on
    invariants.

    Args:
        site: site_id slug (e.g., "travelpec"). Must have an enabled
            parsers.airbnb_dossier block in its site YAML.
        path: Absolute or ~-prefixed path to the dossier .md file. Must
            resolve under one of the site's read roots.

    Returns:
        Dict with four keys:
          - data: The Stays-shaped dict, validated against the Stays
                  collection schema. Ready to pass to
                  emdash_create_content_draft.
          - frontmatter: Raw parsed YAML frontmatter for inspection.
          - body_excerpt: Cruft-stripped body text (~2000 chars).
          - summary: Human-readable one-line summary of the parsed result.

    Raises:
        ValueError on: site config not found, parser not enabled for site,
        missing file, missing required frontmatter fields, or path outside
        the allow-list roots. MCP surfaces the error message back to Hermes
        as a tool error.
    """
    logger.info(
        "parse_airbnb_dossier called with site=%r, path=%r", site, path
    )
    config = load_site_config(site)
    parser_cfg = config.parser("airbnb_dossier")
    result = _parse_airbnb_dossier(
        {"path": path},
        allowed_roots=config.read_roots,
        fixed_fields=parser_cfg.fixed_fields,
    )
    logger.info(
        "parse_airbnb_dossier returning: %s",
        result.payload.get("summary", "(no summary)"),
    )
    return result.payload


# ---------------------------------------------------------------------------
# Filesystem read tools (risk_class=read_only)
# ---------------------------------------------------------------------------
# read_file and list_directory take `site` first. The server resolves the
# site's read_roots (paths.astro + paths.docs + paths.research) from site
# config and passes them as allowed_roots to the underlying function.
# write_file is deferred to Phase 1.3 when the Judge layer comes online.

@mcp.tool()
def read_file(site: str, path: str) -> dict[str, Any]:
    """Read a UTF-8 text file from one of the named site's read roots.

    The path must resolve under paths.astro, paths.docs, or paths.research
    in the site's YAML; otherwise rejected. Files >5MB are rejected to
    keep them out of the context window — use list_directory then targeted
    reads for large trees.

    Args:
        site: site_id slug (e.g., "travelpec").
        path: Absolute or ~-prefixed path to a regular file.

    Returns:
        Dict with `path`, `content`, `size_bytes`, and `summary`. No side
        effects.
    """
    logger.info("read_file called with site=%r, path=%r", site, path)
    config = load_site_config(site)
    result = _read_file({"path": path}, allowed_roots=config.read_roots)
    return result.payload


@mcp.tool()
def list_directory(site: str, path: str) -> dict[str, Any]:
    """List immediate children of a directory under the named site's read
    roots. Does NOT recurse — call repeatedly with subdirectory paths to
    walk a tree.

    Returns entries sorted alphabetically, each with `kind` ('file' or
    'dir') and `size_bytes` for files. Symlinks and other non-regular
    entries are skipped.

    Args:
        site: site_id slug.
        path: Absolute or ~-prefixed directory path. Must resolve under
            one of the site's read roots.

    Returns:
        Dict with `path`, `entries`, and `summary`. No side effects.
    """
    logger.info("list_directory called with site=%r, path=%r", site, path)
    config = load_site_config(site)
    result = _list_directory({"path": path}, allowed_roots=config.read_roots)
    return result.payload


# ---------------------------------------------------------------------------
# Per-site EmDash client cache
# ---------------------------------------------------------------------------

@lru_cache(maxsize=16)
def _emdash_client_for_site(site_id: str) -> EmdashClient:
    """Return an EmdashClient configured for the named site.

    Cached by site_id since clients are stateless except for token+URL
    (both immutable for the MCP subprocess lifetime). On first use for a
    site, the call reads site config, validates the token env var is set,
    and constructs the httpx-wrapped client.

    Cache invalidation is intentional: site config + tokens are fixed
    until Hermes restart. Editing a site YAML or rotating a token
    requires a Hermes restart, which respawns this server and clears
    the cache.

    Raises ValueError if the site config is missing or the token env var
    isn't set (surfaces as a clean MCP tool error rather than a transport
    failure deep in httpx).
    """
    config = load_site_config(site_id)
    # SiteEmdash.token raises a clear ValueError if the env var isn't set;
    # we let that propagate. The MCP layer wraps it for Hermes.
    return EmdashClient(token=config.emdash.token, url=config.emdash.mcp_url)


# ---------------------------------------------------------------------------
# EmDash read tools (risk_class=read_only; Judge-skip)
# ---------------------------------------------------------------------------
# All six tools take `site` as their first parameter. The server resolves
# the per-site EmdashClient (cached), then calls the underlying read
# function with the standard args dict + client kwarg. Return value is
# the tool payload (data + summary) — Hermes/Qwen gets a structured dict.

@mcp.tool()
def emdash_list_collections(site: str) -> dict[str, Any]:
    """List every content collection in the named site's EmDash CMS.

    Returns slug, label, and supported features for each collection. Use
    to discover what content types exist before reading or writing.

    Args:
        site: site_id slug (e.g., "travelpec").
    """
    logger.info("emdash_list_collections called with site=%r", site)
    client = _emdash_client_for_site(site)
    result = _emdash_list_collections({}, client=client)
    return result.payload


@mcp.tool()
def emdash_get_collection_schema(site: str, slug: str) -> dict[str, Any]:
    """Get the full schema of one EmDash collection in the named site.

    Returns every field with its type, required flag, and constraints.
    Required before any content_create/update so Betty knows the field
    shape the server expects.

    Args:
        site: site_id slug.
        slug: Collection slug (e.g., "stays", "villages", "articles").
    """
    logger.info(
        "emdash_get_collection_schema called with site=%r, slug=%r", site, slug
    )
    client = _emdash_client_for_site(site)
    result = _emdash_get_collection_schema({"slug": slug}, client=client)
    return result.payload


@mcp.tool()
def emdash_list_content(
    site: str,
    collection: str,
    status: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    """List content items in a collection on the named site.

    Returns items sorted by the server's default order. Pass
    status='published' to filter to live items only.

    Args:
        site: site_id slug.
        collection: Collection slug.
        status: Optional filter — 'draft' | 'published' | 'scheduled'.
        limit: Optional max items (default 50, max 100).
        cursor: Optional pagination cursor from a previous response.
    """
    logger.info(
        "emdash_list_content called with site=%r, collection=%r, status=%r, "
        "limit=%r, cursor=%r",
        site, collection, status, limit, cursor,
    )
    args: dict[str, Any] = {"collection": collection}
    if status is not None:
        args["status"] = status
    if limit is not None:
        args["limit"] = limit
    if cursor is not None:
        args["cursor"] = cursor
    client = _emdash_client_for_site(site)
    result = _emdash_list_content(args, client=client)
    return result.payload


@mcp.tool()
def emdash_get_content(site: str, collection: str, id: str) -> dict[str, Any]:
    """Get a single content item by ID or slug from the named site.

    Returns the full data plus a `_rev` token for optimistic concurrency
    on the next update. Always call this before emdash_update_content_draft
    to obtain a fresh _rev.

    Args:
        site: site_id slug.
        collection: Collection slug.
        id: Content item ID (ULID) or slug.
    """
    logger.info(
        "emdash_get_content called with site=%r, collection=%r, id=%r",
        site, collection, id,
    )
    client = _emdash_client_for_site(site)
    result = _emdash_get_content(
        {"collection": collection, "id": id},
        client=client,
    )
    return result.payload


@mcp.tool()
def emdash_list_taxonomies(site: str) -> dict[str, Any]:
    """List all taxonomy definitions in the named site's CMS.

    Returns name, label, and hierarchical flag for each taxonomy
    (categories, tags, region, best_for, etc.).

    Args:
        site: site_id slug.
    """
    logger.info("emdash_list_taxonomies called with site=%r", site)
    client = _emdash_client_for_site(site)
    result = _emdash_list_taxonomies({}, client=client)
    return result.payload


@mcp.tool()
def emdash_list_taxonomy_terms(
    site: str,
    taxonomy: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """List terms within one taxonomy on the named site.

    Returns slug, label, and parent linkage for hierarchical taxonomies.

    Args:
        site: site_id slug.
        taxonomy: Taxonomy name (e.g., "region", "best_for").
        limit: Optional max terms (default 50, max 100).
    """
    logger.info(
        "emdash_list_taxonomy_terms called with site=%r, taxonomy=%r, limit=%r",
        site, taxonomy, limit,
    )
    args: dict[str, Any] = {"taxonomy": taxonomy}
    if limit is not None:
        args["limit"] = limit
    client = _emdash_client_for_site(site)
    result = _emdash_list_taxonomy_terms(args, client=client)
    return result.payload


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _preflight_sites() -> None:
    """Validate every site config loads cleanly at server startup.

    Catches malformed YAML or missing required fields at boot time so
    Hermes sees the failure immediately, rather than at first tool call.
    Logs (but doesn't raise) per-site failures — the server still starts
    so good sites work and operators can fix bad sites without a full
    relaunch cycle for each.
    """
    site_ids = list_available_sites()
    if not site_ids:
        logger.warning(
            "No site configs found in BETTY_SITES_DIR (default "
            "~/.betty/sites/). Tools will fail until at least one "
            "site YAML is added."
        )
        return
    logger.info("Preflight: validating %d site config(s)", len(site_ids))
    ok = 0
    for site_id in site_ids:
        try:
            config = load_site_config(site_id)
            logger.info(
                "  [ok] %s (%s, status=%s, %d collection(s), %d parser(s))",
                config.id, config.domain, config.status,
                len(config.collections), len(config.parsers),
            )
            ok += 1
        except ValueError as e:
            logger.error("  [FAIL] %s: %s", site_id, e)
    logger.info("Preflight complete: %d/%d sites OK", ok, len(site_ids))


def main() -> None:
    """Run the MCP server over stdio.

    Hermes spawns this as a subprocess (per ~/.hermes/config.yaml
    mcp_servers.betty) and communicates over stdin/stdout. We block here
    until Hermes shuts the subprocess down (typically at agent exit).
    """
    logger.info(
        "OpenClaw MCP server starting (Phase 4.7 Phase 0 prototype, "
        "Pattern B multi-site)"
    )
    logger.info(
        "Exposing 11 tools: list_sites, betty_ping, parse_airbnb_dossier, "
        "read_file, list_directory, "
        "emdash_list_collections, emdash_get_collection_schema, "
        "emdash_list_content, emdash_get_content, "
        "emdash_list_taxonomies, emdash_list_taxonomy_terms"
    )
    _preflight_sites()
    mcp.run()


if __name__ == "__main__":
    main()
