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
import re
import sys
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
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
from betty_claw.tools.emdash_writes import (
    emdash_create_content_draft as _emdash_create_content_draft,
    emdash_create_taxonomy_term as _emdash_create_taxonomy_term,
    emdash_update_content_draft as _emdash_update_content_draft,
)
from betty_claw.tools.filesystem import (
    list_directory as _list_directory,
    read_file as _read_file,
)
from betty_claw.tools.git_ops import (
    git_diff as _git_diff,
    git_status as _git_status,
)
from betty_claw.tools.voice_validation import (
    validate_text as _validate_text,
    violation_to_dict as _violation_to_dict,
)
from betty_claw.tools.editorial_scorer import (
    editorial_score_to_dict as _editorial_score_to_dict,
    score_editorial_quality as _score_editorial_quality,
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
    if not parser_cfg.target_collection:
        raise ValueError(
            f"Site {site!r}: parsers.airbnb_dossier.target_collection is "
            f"not set in the site YAML. The parser needs a target collection "
            f"so its output can be validated against the right schema."
        )
    if parser_cfg.target_collection not in config.collections:
        raise ValueError(
            f"Site {site!r}: parsers.airbnb_dossier.target_collection="
            f"{parser_cfg.target_collection!r} is not declared in "
            f"collections. Add the collection to the site YAML."
        )
    result = _parse_airbnb_dossier(
        {"path": path},
        allowed_roots=config.read_roots,
        fixed_fields=parser_cfg.fixed_fields,
        target_collection_schema=config.collections[parser_cfg.target_collection],
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
# Git read tools (risk_class=read_only)
# ---------------------------------------------------------------------------
# git_status and git_diff operate against the named site's Astro working
# tree (paths.astro). The MCP server resolves that path from site config
# and passes it as cwd to the underlying functions. git_commit_all and
# git_push are deferred to Phase 1.4 — they need the Judge layer first.

@mcp.tool()
def git_status(site: str) -> dict[str, Any]:
    """Show working-tree status of the named site's Astro repo.

    Uses `git status --porcelain -b` for machine-readable output. Returns
    the current branch, a list of changed entries (each with porcelain code
    + path), and a `clean` boolean.

    Args:
        site: site_id slug (e.g., "travelpec").

    Returns:
        Dict with `branch`, `branch_line`, `entries`, `clean`, `summary`.
        No side effects.
    """
    logger.info("git_status called with site=%r", site)
    config = load_site_config(site)
    result = _git_status({}, cwd=config.paths.astro)
    return result.payload


@mcp.tool()
def git_diff(
    site: str,
    path: str | None = None,
    staged: bool = False,
) -> dict[str, Any]:
    """Show pending changes in the named site's Astro repo.

    By default shows unstaged changes against the working tree. Pass
    staged=True for staged changes against HEAD. Optionally scope to a
    specific path (relative to the repo root).

    Output is capped at 64KB to keep large diffs out of context — if
    truncated, the `truncated` field is True and Betty should commit
    smaller batches or scope to specific paths.

    Args:
        site: site_id slug.
        path: Optional repo-relative path to scope the diff.
        staged: If True, show staged changes (git diff --staged).

    Returns:
        Dict with `path`, `staged`, `diff`, `truncated`, `summary`.
        No side effects.
    """
    logger.info(
        "git_diff called with site=%r, path=%r, staged=%r", site, path, staged
    )
    args: dict[str, Any] = {"staged": staged}
    if path is not None:
        args["path"] = path
    config = load_site_config(site)
    result = _git_diff(args, cwd=config.paths.astro)
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
# Voice validation tool (Phase 1.7 — deterministic post-process check)
# ---------------------------------------------------------------------------
# Betty calls this AFTER rewriting text and BEFORE create_content_draft.
# The same checks also run automatically inside emdash_create_content_draft
# as a structural gate — so even if Betty skips the explicit call, a
# violating draft cannot reach EmDash.

@mcp.tool()
def validate_against_voice(
    site: str,
    text: str,
    source_text: str = "",
) -> dict[str, Any]:
    """Check `text` against the named site's voice validation rules.

    Returns a structured violation list. If empty, the text is compliant
    and ready to write. If not empty, fix each violation (each has a
    rule, the offending substring, a position, and an explanation) and
    re-validate before writing.

    The check is mechanical — banned words, banned openers, first-person
    singular, owner attribution, and numbers in `text` that don't appear
    in `source_text`. Source-grounded judgment (tone, editorial framing,
    multi-night recommendations) is still Betty's responsibility per the
    voice doc.

    Args:
        site: site_id slug.
        text: The rewritten text to validate (typically a description
            or persona field from a content draft).
        source_text: The original source the rewrite is grounded in
            (typically the parsed dossier's body_excerpt + frontmatter
            stringified). Used for the number-grounding check. Pass
            empty string if not relevant; the number check will then
            flag every number as ungrounded.

    Returns:
        Dict with:
          - compliant: bool — true if zero violations.
          - violations: list of {rule, match, position, explanation}.
          - summary: human-readable one-liner.
    """
    logger.info(
        "validate_against_voice called with site=%r, text len=%d, source len=%d",
        site, len(text), len(source_text),
    )
    config = load_site_config(site)
    if not config.voice_validation or not config.voice_validation.enabled:
        return {
            "compliant": True,
            "violations": [],
            "summary": (
                f"Site {site!r} has no voice_validation block enabled. "
                f"Skipping mechanical check; rely on voice doc judgment."
            ),
        }
    violations = _validate_text(text, source_text, config.voice_validation)
    return {
        "compliant": len(violations) == 0,
        "violations": [_violation_to_dict(v) for v in violations],
        "summary": (
            f"{len(violations)} violation(s) found in text "
            f"({len(text)} chars)."
            if violations
            else f"Compliant: text passes all {site!r} voice rules."
        ),
    }


@mcp.tool()
def score_editorial_quality(
    site: str,
    text: str,
    source_text: str,
) -> dict[str, Any]:
    """Semantic editorial-quality scorer for travelpec.com voice (Phase 2).

    Calls Claude (Haiku by default) to evaluate `text` against the voice
    calibration's qualitative rules — the ones the deterministic
    validate_against_voice tool can't see: atmospheric editorial
    invention, distance inference, capacity inference, marketing voice,
    generic filler, inverted emphasis.

    Use this AFTER validate_against_voice returns compliant. The two
    layers are complementary: mechanical first (cheap, catches the
    bulk of violations), semantic second (catches what the regex
    misses). Treat score >= 8 as ready-to-publish, 5-7 as needs-revision,
    < 5 as restart-from-source.

    Args:
        site: site_id slug. Used for logging context; the rubric is
            travelpec-specific in the current implementation.
        text: The rewritten description to score.
        source_text: The raw source the rewrite is derived from. Pass
            the parsed dossier's frontmatter + body_excerpt for Airbnb
            content; the model uses this to detect inference vs.
            grounding.

    Returns:
        Dict with:
          - score: int 0-10
          - violations: list of {category, passage, explanation}
          - summary: one-sentence verdict
          - cost_usd: what this call cost
          - model: which Anthropic model produced the score
          - input_tokens / output_tokens: accounting for visibility
    """
    logger.info(
        "score_editorial_quality called with site=%r, text len=%d, "
        "source len=%d",
        site, len(text), len(source_text),
    )
    try:
        score = _score_editorial_quality(text=text, source_text=source_text)
    except Exception as e:
        # Editorial scoring failures should not block the workflow —
        # surface as a structured error result instead of crashing the
        # MCP tool. Betty can then choose to proceed with the
        # deterministic validation result only.
        logger.error("score_editorial_quality failed: %r", e)
        return {
            "score": None,
            "violations": [],
            "summary": (
                f"Editorial scoring unavailable: {type(e).__name__}: {e}. "
                f"Proceed with mechanical validation result only, or "
                f"retry."
            ),
            "cost_usd": 0.0,
            "model": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": True,
        }
    result = _editorial_score_to_dict(score)
    logger.info(
        "score_editorial_quality returning score=%d, %d violation(s), "
        "cost=$%.6f",
        score.score, len(score.violations), score.cost_usd,
    )
    return result


def _enforce_voice_validation(
    config: Any,
    collection: str,
    data: dict[str, Any],
    source_text: str = "",
) -> None:
    """Raise ValueError if any check_field in `data` violates voice rules.

    Called from emdash_create_content_draft and emdash_update_content_draft
    as a structural gate: drafts that violate voice rules cannot reach
    EmDash even if Betty skipped the explicit validate_against_voice call.
    """
    vv = config.voice_validation
    if vv is None or not vv.enabled:
        return
    all_violations: list[dict[str, Any]] = []
    for field_name in vv.check_fields:
        if field_name not in data:
            continue
        text = data[field_name]
        if not isinstance(text, str):
            continue
        violations = _validate_text(text, source_text, vv)
        for v in violations:
            all_violations.append({
                "field": field_name,
                **_violation_to_dict(v),
            })
    if all_violations:
        # Surface as a structured ValueError that FastMCP wraps as a
        # tool error back to Betty. The message lists EVERY violation
        # with its field name, rule, and offending substring up-front
        # so the failure mode is unambiguous even when Hermes truncates
        # long tool errors. Field name appears in the FIRST 100 chars
        # so it survives truncation.
        fields = sorted({v["field"] for v in all_violations})
        head = (
            f"VOICE VALIDATION BLOCKED WRITE on fields: {fields}. "
            f"{len(all_violations)} violation(s) total."
        )
        per_violation_lines = [
            f"  field={v['field']} rule={v['rule']} "
            f"match={v['match']!r} — {v['explanation']}"
            for v in all_violations
        ]
        raise ValueError(head + "\n" + "\n".join(per_violation_lines))


# ---------------------------------------------------------------------------
# EmDash write tools (risk_class=reversible_write; draft-only for Phase 1.5)
# ---------------------------------------------------------------------------
# Three write tools, all draft-only. The Judge layer (Phase 2) is not in
# place yet, so we deliberately exclude the high-risk writes:
#   - emdash_publish_content       (external_side_effect — visible online)
#   - emdash_unpublish_content     (external_side_effect — pulls live content)
#   - emdash_create_collection     (schema change — Peter's design work)
#   - emdash_create_field          (schema change — Peter's design work)
#
# Safety net: drafts sit in EmDash's draft state until Peter publishes
# manually via the EmDash UI. SiteCollection schema validation runs
# client-side BEFORE the write hits EmDash, so malformed data is caught
# at the MCP boundary rather than after a round-trip.

def _collection_schema_for(site: str, collection: str) -> tuple[Any, Any]:
    """Resolve (config, collection_schema) for a write tool call.

    Centralizes the validation that the requested collection is declared
    in the site YAML. Returns the full SiteConfig (callers need it for
    the EmdashClient) and the specific SiteCollection. Raises ValueError
    with a clear message if the collection isn't configured.
    """
    config = load_site_config(site)
    if collection not in config.collections:
        available = sorted(config.collections.keys())
        raise ValueError(
            f"Site {site!r} has no collection {collection!r} declared "
            f"in its YAML. Available: {available}. If the collection "
            f"exists in EmDash but not in the YAML, add it to "
            f"~/.betty/sites/{site}.yaml under `collections:`."
        )
    return config, config.collections[collection]


@mcp.tool()
def emdash_create_content_draft(
    site: str,
    collection: str,
    data: dict[str, Any],
    slug: str | None = None,
    source_text: str = "",
) -> dict[str, Any]:
    """Create a new content item as a DRAFT in the named site's EmDash.

    The item is NOT visible on the live site after this call — it sits in
    EmDash's draft state. Peter reviews and publishes manually via the
    EmDash UI. Phase 1.5 deliberately does not expose a publish tool.

    Field values in `data` are validated against the site's declared
    collection schema before the write hits EmDash. If the site has a
    voice_validation block enabled (Phase 1.7), each `check_fields` entry
    in `data` is ALSO checked against the voice rules — drafts that
    violate banned-word, opener, or hallucinated-number rules cannot
    reach EmDash. Betty should call validate_against_voice first and
    self-correct; this is a structural backstop.

    Args:
        site: site_id slug (e.g., "travelpec").
        collection: Collection slug; must be declared in the site YAML.
        data: Field values matching the collection schema.
        slug: Optional URL slug. If omitted, EmDash auto-generates one
            from the title.
        source_text: Optional source text for the voice validation
            number-grounding check. For Airbnb dossier flows, pass the
            concatenation of parsed `frontmatter` and `body_excerpt`. If
            omitted, the number-grounding check will flag every number.

    Returns:
        Dict containing `data` (the EmDash response with new item ID,
        revision, etc.) and a human-readable `summary`. The item ID is
        what subsequent emdash_update_content_draft calls use to target
        this draft.
    """
    logger.info(
        "emdash_create_content_draft called with site=%r, collection=%r, "
        "slug=%r, data keys=%r, source_text len=%d",
        site, collection, slug,
        sorted(data.keys()) if isinstance(data, dict) else "?",
        len(source_text),
    )
    config, collection_schema = _collection_schema_for(site, collection)
    _enforce_voice_validation(config, collection, data, source_text)
    args: dict[str, Any] = {"collection": collection, "data": data}
    if slug is not None:
        args["slug"] = slug
    client = _emdash_client_for_site(site)
    result = _emdash_create_content_draft(
        args, client=client, collection_schema=collection_schema,
    )
    return result.payload


@mcp.tool()
def emdash_update_content_draft(
    site: str,
    collection: str,
    id: str,
    data: dict[str, Any],
    rev: str | None = None,
    source_text: str = "",
) -> dict[str, Any]:
    """Update fields on an existing content item in the named site's EmDash.

    Partial update — only the fields in `data` are changed; other fields
    are left as-is. Does NOT change publication status: drafts stay drafts.

    The optional `rev` token enables optimistic concurrency. Pass the
    `_rev` from a recent emdash_get_content call to detect concurrent
    modifications and avoid silent overwrites.

    If the site has voice_validation enabled and `data` includes any
    check_fields (e.g., description, persona), the new values are
    voice-checked before EmDash is touched. Pass `source_text` so the
    number-grounding rule can verify any numbers in your update.

    Args:
        site: site_id slug.
        collection: Collection slug; must be declared in the site YAML.
        id: Content item ID or slug to update.
        data: Fields to change. Must match the collection schema; unknown
            keys rejected.
        rev: Optional optimistic-concurrency token.
        source_text: Optional source text for voice validation
            number-grounding check.
    """
    logger.info(
        "emdash_update_content_draft called with site=%r, collection=%r, "
        "id=%r, rev=%r, data keys=%r, source_text len=%d",
        site, collection, id, rev,
        sorted(data.keys()) if isinstance(data, dict) else "?",
        len(source_text),
    )
    config, collection_schema = _collection_schema_for(site, collection)
    _enforce_voice_validation(config, collection, data, source_text)
    args: dict[str, Any] = {
        "collection": collection,
        "id": id,
        "data": data,
    }
    if rev is not None:
        args["_rev"] = rev
    client = _emdash_client_for_site(site)
    result = _emdash_update_content_draft(
        args, client=client, collection_schema=collection_schema,
    )
    return result.payload


@mcp.tool()
def emdash_create_taxonomy_term(
    site: str,
    taxonomy: str,
    slug: str,
    label: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    """Create a new term in a taxonomy on the named site's EmDash.

    Use when an in-progress content draft references a taxonomy value
    (e.g., a Region term) that doesn't yet exist in the system. For
    hierarchical taxonomies, pass parent_id to nest under an existing
    term.

    Args:
        site: site_id slug.
        taxonomy: Taxonomy name (e.g., "region", "best_for").
        slug: URL-safe identifier for the new term.
        label: Human-readable display name.
        parent_id: Optional parent term ID for hierarchical taxonomies.
    """
    logger.info(
        "emdash_create_taxonomy_term called with site=%r, taxonomy=%r, "
        "slug=%r, label=%r, parent_id=%r",
        site, taxonomy, slug, label, parent_id,
    )
    args: dict[str, Any] = {
        "taxonomy": taxonomy,
        "slug": slug,
        "label": label,
    }
    if parent_id is not None:
        args["parentId"] = parent_id
    client = _emdash_client_for_site(site)
    result = _emdash_create_taxonomy_term(args, client=client)
    return result.payload


# ---------------------------------------------------------------------------
# Worklist discovery (Phase 2.0 Step 1)
# ---------------------------------------------------------------------------
# Prerequisite for Kanban autonomous dispatch: a tool that returns the
# set of Airbnb dossiers not yet present in EmDash as Stays drafts.
# Cross-references files on disk against EmDash content_list results
# by outbound_url match — the dossier's `url:` frontmatter field is the
# stable identity for an Airbnb listing across both sides.

_DOSSIER_URL_PATTERN = re.compile(r"^url:\s*(.+?)\s*$", re.MULTILINE)


def _extract_url_from_dossier(path: Path) -> str | None:
    """Read just the frontmatter of a dossier and return its url: value.

    Cheap operation — reads at most 40 lines (frontmatter is bounded by
    the second `---` line and is always near the top). Used by the
    worklist tool to fingerprint dossiers without paying the full
    parse_airbnb_dossier cost just to identify them.
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                lines.append(line)
                if i > 50:
                    break
            head = "".join(lines)
    except OSError:
        return None
    m = _DOSSIER_URL_PATTERN.search(head)
    if not m:
        return None
    return m.group(1).strip()


@mcp.tool()
def list_pending_airbnb_dossiers(site: str) -> dict[str, Any]:
    """Return the set of Airbnb dossiers not yet drafted into EmDash.

    Walks {site.paths.research}/01-source-data/research/airbnb-listings/
    and lists every .md file. Calls emdash_list_content for the Stays
    collection. Matches each dossier's `url:` frontmatter field against
    Stays entries' outbound_url field. Dossiers whose URL doesn't appear
    in EmDash are returned as `pending` — these are what a Kanban
    autonomous run would dispatch.

    Note: the match is strict equality on the URL string. If Airbnb
    canonicalizes URLs (e.g., adding tracking params), the dossier will
    appear pending even though there's a draft. If that becomes a
    problem, normalize URLs on both sides before matching.

    Args:
        site: site_id slug.

    Returns:
        Dict with:
          - pending: list of {dossier_path, dossier_filename, url}
            for dossiers without a corresponding Stays entry.
          - published: list of {dossier_path, dossier_filename, url,
            stays_id} for dossiers that have one.
          - skipped: list of {dossier_path, reason} for files we could
            not parse a URL from.
          - counts: summary {pending, published, skipped, total}.
    """
    logger.info("list_pending_airbnb_dossiers called with site=%r", site)
    config = load_site_config(site)
    dossier_dir = config.paths.research / "01-source-data" / "research" / "airbnb-listings"
    if not dossier_dir.exists():
        raise ValueError(
            f"Dossier directory not found: {dossier_dir}. Check "
            f"site.paths.research and that 01-source-data/research/"
            f"airbnb-listings/ exists under it."
        )

    # Build set of URLs already present as Stays in EmDash. Paginate to
    # be safe — Stays collection can grow past one page eventually.
    client = _emdash_client_for_site(site)
    published_by_url: dict[str, str] = {}  # url -> stays_id
    cursor: str | None = None
    while True:
        args: dict[str, Any] = {"collection": "stays", "limit": 100}
        if cursor:
            args["cursor"] = cursor
        result = _emdash_list_content(args, client=client)
        data = result.payload.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            item_data = item.get("data") or item
            if isinstance(item_data, dict):
                url = item_data.get("outbound_url")
                if isinstance(url, str) and url.strip():
                    published_by_url[url.strip()] = str(item.get("id", "(unknown)"))
        next_cursor = data.get("nextCursor") if isinstance(data, dict) else None
        if not next_cursor:
            break
        cursor = next_cursor

    # Enumerate dossiers on disk.
    pending: list[dict[str, str]] = []
    published: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for dossier_path in sorted(dossier_dir.glob("*.md")):
        url = _extract_url_from_dossier(dossier_path)
        if not url:
            skipped.append({
                "dossier_path": str(dossier_path),
                "dossier_filename": dossier_path.name,
                "reason": "Could not extract `url:` from frontmatter.",
            })
            continue
        if url in published_by_url:
            published.append({
                "dossier_path": str(dossier_path),
                "dossier_filename": dossier_path.name,
                "url": url,
                "stays_id": published_by_url[url],
            })
        else:
            pending.append({
                "dossier_path": str(dossier_path),
                "dossier_filename": dossier_path.name,
                "url": url,
            })

    total = len(pending) + len(published) + len(skipped)
    logger.info(
        "list_pending_airbnb_dossiers: %d pending, %d published, "
        "%d skipped (total %d) for site=%r",
        len(pending), len(published), len(skipped), total, site,
    )
    return {
        "pending": pending,
        "published": published,
        "skipped": skipped,
        "counts": {
            "pending": len(pending),
            "published": len(published),
            "skipped": len(skipped),
            "total": total,
        },
    }


# ---------------------------------------------------------------------------
# Kanban worker prompt generation (Phase 2.0 Step 2a)
# ---------------------------------------------------------------------------
# Each Kanban task that dispatches Betty to process one pending dossier
# carries a fully-specified worker prompt as its task description. The
# prompt is tight enough that a fresh Hermes sub-conversation (with no
# accumulated context) can execute the full composed workflow without
# wandering into discovery, batch-pattern-matching, or scope creep.
#
# Tunable here so the template can iterate as we observe Qwen's edge
# cases. The {dossier_path} placeholder gets substituted per task.

_WORKER_PROMPT_TEMPLATE = """\
SINGLE-DOSSIER TASK — execute exactly this workflow, nothing more.

The dossier_path for this task is:
  {dossier_path}

You will reference this same path twice: once in step 1, once in step 8. \
There is no token to remember — just keep this path string in mind.

1. Call mcp_betty_compose_stays_draft_preview with:
     site="travelpec"
     dossier_path="{dossier_path}"
   Use the path verbatim. Do not call mcp_betty_list_pending_airbnb_dossiers \
or any discovery tool. Do not list directories. Do not search for files. \
The path is correct as given.

2. From the response, capture: source_text, parsed_description, \
parsed_persona, body_excerpt, parsed_data_summary. No token; the path \
above is your only state.

3. Rewrite parsed_description following the voice rules in §1-§9 of \
~/.hermes/memories/MEMORY.md and the voice doc at \
/Users/betty/My Drive/Betty/emdash-sites/travelpec.com-v3/02-voice/\
03-voice-calibration.md (already loaded). Use only facts present in \
body_excerpt or the frontmatter values shown in parsed_data_summary. \
Do not infer distances, capacities, ratings, or business names not in source. \
Omit, don't invent.

4. Rewrite parsed_persona — one concise editorial sentence describing the \
property. Source-grounded only.

5. Validate description: call mcp_betty_validate_against_voice with \
site="travelpec", text=YOUR_DESCRIPTION, source_text=SOURCE_TEXT_FROM_STEP_2. \
If violations, fix and re-call. Cap at 3 iterations.

6. Validate persona the same way. Cap at 3 iterations.

7. (Optional) Call mcp_betty_score_editorial_quality on description with \
the same source_text. If score < 8 with violations, revise description and \
re-validate from step 5.

8. Call mcp_betty_compose_stays_draft_publish with:
     site="travelpec"
     dossier_path="{dossier_path}"
     description=YOUR_FINAL_DESCRIPTION
     persona=YOUR_FINAL_PERSONA
   Use the EXACT same dossier_path string from step 1. Do not modify it. \
Do not abbreviate it. Do not paraphrase it.

9. Report ONLY: draft_id, dossier filename, the final description, the \
final persona. Stop. Do not call any more tools. Do not clean up older \
drafts. Do not start another dossier. Do not query the worklist. \
Your task is complete after the publish response."""


@mcp.tool()
def generate_pending_worker_prompts(site: str) -> dict[str, Any]:
    """Generate one Kanban-ready worker prompt per pending Airbnb dossier.

    For each dossier in the site's pending list (from
    list_pending_airbnb_dossiers), produces a fully-specified worker
    prompt that a fresh Hermes sub-conversation can execute end-to-end:
    parse → rewrite both description and persona → validate both →
    optionally score → publish → stop.

    Each prompt has the exact dossier_path baked in (eliminating the
    path-discovery failure mode we observed in chat sessions), explicit
    "do not call discovery tools" directives, and an explicit
    stop-after-publish instruction.

    The output is intended to be fed into Hermes Kanban as task
    descriptions — one Kanban task per pending dossier — so the queue
    can run autonomously to completion with context reset between
    dossiers. This sidesteps the chat-layer composition failure modes
    we hit during ad-hoc prompting.

    Args:
        site: site_id slug. Currently must be "travelpec" — worker
            template references travelpec-specific paths (voice doc
            location, etc.).

    Returns:
        Dict with:
          - count: number of worker prompts generated.
          - worker_prompts: list of {dossier_filename, dossier_path,
            worker_prompt}. Each `worker_prompt` is the full task
            description ready to drop into a Kanban task.
          - skipped: list of dossiers we couldn't extract a URL from
            (passed through from list_pending_airbnb_dossiers).
          - already_published_count: dossiers already in EmDash.
    """
    logger.info("generate_pending_worker_prompts called with site=%r", site)
    pending_result = list_pending_airbnb_dossiers(site)
    pending = pending_result["pending"]
    skipped = pending_result["skipped"]
    already_published_count = pending_result["counts"]["published"]

    worker_prompts: list[dict[str, str]] = []
    for entry in pending:
        prompt = _WORKER_PROMPT_TEMPLATE.format(
            dossier_path=entry["dossier_path"],
        )
        worker_prompts.append({
            "dossier_filename": entry["dossier_filename"],
            "dossier_path": entry["dossier_path"],
            "worker_prompt": prompt,
        })

    logger.info(
        "generate_pending_worker_prompts produced %d prompts (skipped %d, "
        "already published %d)",
        len(worker_prompts), len(skipped), already_published_count,
    )
    return {
        "count": len(worker_prompts),
        "worker_prompts": worker_prompts,
        "skipped": skipped,
        "already_published_count": already_published_count,
    }


# ---------------------------------------------------------------------------
# Composed workflow: Airbnb dossier → Stays draft (Phase 2.0 — tokenless)
# ---------------------------------------------------------------------------
# Two-call workflow keyed by dossier_path. preview parses and returns
# the materials Betty needs to rewrite; publish accepts the same
# dossier_path plus Betty's rewrites and writes the draft.
#
# Why no token: Qwen3.5-35B cannot reliably copy state-carrying strings
# between tool calls. Earlier token-based design saw ~30% failure rate
# from Betty inventing a different token string at publish time than
# was returned by begin. dossier_path is in Betty's original prompt
# (not a tool response), so she copies it from instructions rather than
# from short-term memory. The server-side cache is keyed by
# (site, dossier_path) for performance; on cache miss publish re-parses
# the dossier itself, so correctness no longer depends on cache hit.

@dataclass(frozen=True)
class _ComposeState:
    """One parsed-dossier state entry.

    Stored in the server-side cache for up to _COMPOSE_STATE_TTL_SECONDS
    as a performance optimization. publish recovers the same state from
    cache or by re-parsing; this is a perf concern, not a correctness one.
    """
    site: str
    dossier_filename: str
    parsed_data: dict[str, Any]
    source_text: str
    body_excerpt: str
    frontmatter: dict[str, Any]
    created_at: float


# Keyed by (site, dossier_path) tuple so the cache lookup uses values
# Betty already has in her prompt rather than a random UUID she has to
# carry between tool calls.
_compose_state_cache: dict[tuple[str, str], _ComposeState] = {}

# TTL bounds stale cache entries. publish on cache miss re-parses, so
# expiry isn't a correctness issue — just controls memory growth on
# long-running MCP servers.
_COMPOSE_STATE_TTL_SECONDS = 30 * 60


def _prune_compose_state() -> None:
    """Drop expired entries. Called at the start of preview/publish.

    Cheap O(n) scan; the cache rarely holds more than a handful of
    entries since publish evicts on success and ttl reaps abandoned
    previews.
    """
    now = time.time()
    expired = [
        key for key, state in _compose_state_cache.items()
        if now - state.created_at > _COMPOSE_STATE_TTL_SECONDS
    ]
    for key in expired:
        del _compose_state_cache[key]
    if expired:
        logger.info("Pruned %d expired compose state(s)", len(expired))


def _parse_dossier_state(site: str, dossier_path: str) -> _ComposeState:
    """Parse a dossier and build the compose state.

    Used by both preview (which caches the result) and publish (which
    falls back to this on cache miss). Centralizing here means the two
    tools cannot drift in how they interpret the dossier.
    """
    config = load_site_config(site)
    parser_cfg = config.parser("airbnb_dossier")
    if not parser_cfg.target_collection:
        raise ValueError(
            f"Site {site!r}: parsers.airbnb_dossier.target_collection is "
            f"not configured."
        )
    if parser_cfg.target_collection not in config.collections:
        raise ValueError(
            f"Site {site!r}: parsers.airbnb_dossier.target_collection="
            f"{parser_cfg.target_collection!r} is not declared in "
            f"collections."
        )

    result = _parse_airbnb_dossier(
        {"path": dossier_path},
        allowed_roots=config.read_roots,
        fixed_fields=parser_cfg.fixed_fields,
        target_collection_schema=config.collections[parser_cfg.target_collection],
    )
    payload = result.payload
    parsed_data: dict[str, Any] = dict(payload["data"])
    frontmatter: dict[str, Any] = dict(payload.get("frontmatter") or {})
    body_excerpt: str = str(payload.get("body_excerpt") or "")
    source_text = f"{frontmatter}\n{body_excerpt}"

    return _ComposeState(
        site=site,
        dossier_filename=Path(dossier_path).name,
        parsed_data=parsed_data,
        source_text=source_text,
        body_excerpt=body_excerpt,
        frontmatter=frontmatter,
        created_at=time.time(),
    )


def _get_or_parse_compose_state(site: str, dossier_path: str) -> _ComposeState:
    """Return cached compose state or parse fresh on miss.

    Cache key is the (site, dossier_path) tuple — values Betty already
    has in her prompt. No token. On cache miss the dossier is re-parsed
    and the result cached; this happens transparently to Betty.
    """
    _prune_compose_state()
    key = (site, dossier_path)
    state = _compose_state_cache.get(key)
    if state is not None:
        return state
    state = _parse_dossier_state(site, dossier_path)
    _compose_state_cache[key] = state
    return state


@mcp.tool()
def compose_stays_draft_preview(site: str, dossier_path: str) -> dict[str, Any]:
    """Parse an Airbnb dossier and return the materials needed to compose
    a Stays draft rewrite.

    Tokenless workflow (Phase 2.0): this call no longer issues a session
    token. The dossier_path you pass here is the same string you pass
    to compose_stays_draft_publish — no intermediate state to carry.
    Server caches the parse for performance but publish re-parses on
    cache miss, so correctness doesn't depend on cache hit.

    After this call, rewrite parsed_description and parsed_persona
    following the voice rules. Use mcp_betty_validate_against_voice
    and mcp_betty_score_editorial_quality to iterate. When both pass,
    call mcp_betty_compose_stays_draft_publish with the same site and
    dossier_path plus your final rewrites.

    Args:
        site: site_id slug (e.g., "travelpec").
        dossier_path: Absolute path to the dossier .md file. Must
            resolve under one of the site's read roots.

    Returns:
        Dict with:
          - source_text: precomputed string for validate/score calls.
          - parsed_description: the parser's raw description. Your
            starting point for editorial rewrite.
          - parsed_persona: the parser's raw persona, extracted from
            Airbnb body text. Almost always marketing voice; you MUST
            rewrite it before publish.
          - body_excerpt: cruft-stripped body, your source of truth.
          - parsed_data_summary: human-readable field summary.
          - frontmatter_keys: list of frontmatter keys (their values
            are valid sources for numbers in your rewrite).
          - dossier_filename: short filename for your report.
          - instructions: next-steps reminder.
    """
    logger.info(
        "compose_stays_draft_preview called with site=%r, dossier_path=%r",
        site, dossier_path,
    )
    state = _get_or_parse_compose_state(site, dossier_path)

    # Human-readable summary of every field the parser produced.
    summary_lines = []
    for k, v in state.parsed_data.items():
        v_str = str(v)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        summary_lines.append(f"  {k}: {v_str}")
    parsed_data_summary = "\n".join(summary_lines)

    return {
        "source_text": state.source_text,
        "parsed_description": state.parsed_data.get("description", ""),
        "parsed_persona": state.parsed_data.get("persona", ""),
        "body_excerpt": state.body_excerpt,
        "parsed_data_summary": parsed_data_summary,
        "frontmatter_keys": sorted(state.frontmatter.keys()),
        "dossier_filename": state.dossier_filename,
        "instructions": (
            f"Dossier {state.dossier_filename} parsed. You MUST rewrite "
            f"BOTH parsed_description AND parsed_persona following the "
            f"voice rules — both are voice-validated at publish. When "
            f"both rewrites pass validation and score, call "
            f"mcp_betty_compose_stays_draft_publish with the SAME site "
            f"and dossier_path you passed here, plus your two rewrites. "
            f"There is no token to remember — just keep the dossier_path."
        ),
    }


@mcp.tool()
def compose_stays_draft_publish(
    site: str,
    dossier_path: str,
    description: str,
    persona: str,
) -> dict[str, Any]:
    """Publish a Stays draft for the named dossier with Betty's rewrites.

    Tokenless workflow (Phase 2.0): looks up the cached parse for
    (site, dossier_path), or re-parses the dossier if the cache has
    expired. Replaces description and persona with Betty's rewrites
    and writes the Stays draft to EmDash. Mechanical voice validation
    runs as a structural backstop on BOTH fields — non-compliant
    drafts cannot reach EmDash even if Betty skipped explicit validation.

    The Stays schema's voice-validated `check_fields` are description
    and persona. The parser auto-extracts a persona from raw Airbnb
    body text, which usually contains marketing voice. Betty must
    rewrite both fields before publishing or the backstop will block.

    Args:
        site: site_id slug. Must match the site used in preview.
        dossier_path: Same dossier_path passed to preview. The server
            uses this to look up cached parse state, or to re-parse
            on cache miss.
        description: Betty's final rewritten description, voice-compliant.
        persona: Betty's final rewritten persona — one editorial sentence.
    """
    logger.info(
        "compose_stays_draft_publish called with site=%r, dossier_path=%r, "
        "description len=%d, persona len=%d",
        site, dossier_path, len(description), len(persona),
    )

    if not isinstance(description, str) or not description.strip():
        raise ValueError(
            "compose_stays_draft_publish.description must be a non-empty "
            "string."
        )
    if not isinstance(persona, str) or not persona.strip():
        raise ValueError(
            "compose_stays_draft_publish.persona must be a non-empty "
            "string. The parser's auto-extracted persona usually has "
            "marketing voice; rewrite it before publish."
        )

    # Cache hit or re-parse on miss. Either way Betty doesn't see the
    # difference — the path she has in her prompt is all that's needed.
    state = _get_or_parse_compose_state(site, dossier_path)

    config, collection_schema = _collection_schema_for(state.site, "stays")

    # Merge Betty's rewrites into the parsed data. Fresh dict so the
    # cache entry stays clean.
    final_data = dict(state.parsed_data)
    final_data["description"] = description
    final_data["persona"] = persona

    # Backstop: mechanical voice validation on every check_field.
    _enforce_voice_validation(
        config, "stays", final_data, source_text=state.source_text,
    )

    client = _emdash_client_for_site(state.site)
    result = _emdash_create_content_draft(
        {"collection": "stays", "data": final_data},
        client=client,
        collection_schema=collection_schema,
    )
    response = result.payload.get("data") or {}
    draft_id = (
        response.get("id")
        if isinstance(response, dict)
        else None
    ) or "(unknown)"
    title = final_data.get("title", "(no title)")

    # Evict on successful publish so the same dossier doesn't accidentally
    # double-publish from a stale cache entry. (Betty can preview again
    # to re-parse and re-cache if she wants to write another draft for
    # the same dossier.)
    _compose_state_cache.pop((site, dossier_path), None)
    logger.info(
        "compose_stays_draft_publish wrote draft_id=%s for dossier=%r",
        draft_id, state.dossier_filename,
    )

    return {
        "draft_id": draft_id,
        "title": title,
        "dossier_filename": state.dossier_filename,
        "summary": (
            f"Published draft {draft_id} for {state.dossier_filename!r}: "
            f"title={title!r}. Cache entry consumed; preview again if "
            f"you need to write another draft for this dossier."
        ),
    }


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
        "Exposing 22 tools: list_sites, betty_ping, parse_airbnb_dossier, "
        "read_file, list_directory, git_status, git_diff, "
        "validate_against_voice, score_editorial_quality, "
        "list_pending_airbnb_dossiers, generate_pending_worker_prompts, "
        "compose_stays_draft_preview, compose_stays_draft_publish, "
        "emdash_list_collections, emdash_get_collection_schema, "
        "emdash_list_content, emdash_get_content, "
        "emdash_list_taxonomies, emdash_list_taxonomy_terms, "
        "emdash_create_content_draft, emdash_update_content_draft, "
        "emdash_create_taxonomy_term"
    )
    _preflight_sites()
    mcp.run()


if __name__ == "__main__":
    main()
