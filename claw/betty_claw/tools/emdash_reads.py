"""
EmDash MCP read-only tools for Betty's Phase 4.6 actor.

Six tools wrap the read surface of the EmDash MCP server, all with
risk_class=read_only — the actor inner loop skips the Judge for these
per Phase 4.5 Decision C. They still write SKIP_READ_ONLY audit rows
to judge_decisions for the run trail.

  - emdash_list_collections        → schema_list_collections
  - emdash_get_collection_schema   → schema_get_collection
  - emdash_list_content            → content_list
  - emdash_get_content             → content_get
  - emdash_list_taxonomies         → taxonomy_list
  - emdash_list_taxonomy_terms     → taxonomy_list_terms

Validation is light: arg-shape (required keys, types, no extras) is
enforced here so schema/validator drift is loud. Field-level constraints
(e.g., valid collection slugs, ID format) are enforced by the EmDash
server — errors come back via EmdashMCPError and the actor's tool-result
loop surfaces them to Qwen for retry.

A module-level EmdashClient instance is created lazily on first call.
This avoids paying the env-var read cost during module import (matters
for `python -m betty_claw.tools` self-test runs that don't touch the
EmDash tools) while keeping client lifecycle simple — one client per
process, reused across tool calls.
"""

from __future__ import annotations

import uuid
from typing import Any

from betty_claw.contracts import ToolResult
from betty_claw.emdash_client import EmdashClient, EmdashMCPError


# ---------------------------------------------------------------------------
# Lazy client
# ---------------------------------------------------------------------------

_client: EmdashClient | None = None


def _get_client() -> EmdashClient:
    """Return the module-level EmdashClient, constructing on first call."""
    global _client
    if _client is None:
        _client = EmdashClient()
    return _client


def _set_client_for_test(client: EmdashClient | None) -> None:
    """Test seam: inject a client (or reset to None to force lazy re-init)."""
    global _client
    _client = client


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _assert_dict_keys(args: dict, required: set[str], optional: set[str], tool_name: str) -> None:
    """Validate args has exactly required keys + optional subset; no extras."""
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    provided = set(args.keys())
    missing = required - provided
    if missing:
        raise ValueError(f"{tool_name} missing required keys: {sorted(missing)}")
    extra = provided - required - optional
    if extra:
        raise ValueError(
            f"{tool_name} received unknown keys: {sorted(extra)}. "
            f"Allowed: {sorted(required | optional)}"
        )


def _assert_str(value: Any, field: str, tool_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(
            f"{tool_name}.{field} must be str, got {type(value).__name__}"
        )
    if not value.strip():
        raise ValueError(f"{tool_name}.{field} must be non-empty")


def _wrap_result(tool_name: str, data: Any, summary: str) -> ToolResult:
    """Build a ToolResult for a read tool. status='executed' always."""
    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name=tool_name,
        status="executed",
        payload={
            "data": data,
            "summary": summary,
        },
    )


def _call(tool_name: str, mcp_name: str, arguments: dict) -> Any:
    """Call the MCP tool, surfacing EmdashMCPError as ValueError.

    EmdashMCPError → ValueError lets the actor's existing tool-validation
    error handling (which already catches ValueError/TypeError) surface
    MCP-side errors to Qwen without a new exception path. The MCP error
    code and message are preserved in the ValueError text for diagnosis.
    """
    try:
        return _get_client().call_tool(mcp_name, arguments)
    except EmdashMCPError as e:
        raise ValueError(
            f"{tool_name}: EmDash MCP returned error {e.code}: {e.message}"
        ) from e


# ---------------------------------------------------------------------------
# Tool: emdash_list_collections
# ---------------------------------------------------------------------------

EMDASH_LIST_COLLECTIONS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_list_collections",
        "description": (
            "List all content collections defined in the EmDash CMS. Returns "
            "slug, label, and supported features for each collection. Use to "
            "discover what content types exist before reading or writing."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def emdash_list_collections(args: dict) -> ToolResult:
    """risk_class=read_only."""
    _assert_dict_keys(args, required=set(), optional=set(),
                      tool_name="emdash_list_collections")
    data = _call("emdash_list_collections", "schema_list_collections", {})
    items = data.get("items") if isinstance(data, dict) else data
    count = len(items) if isinstance(items, list) else "?"
    return _wrap_result(
        "emdash_list_collections",
        data,
        f"{count} collections registered in EmDash.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_get_collection_schema
# ---------------------------------------------------------------------------

EMDASH_GET_COLLECTION_SCHEMA_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_get_collection_schema",
        "description": (
            "Get the full schema of one EmDash collection — every field "
            "with its type, required flag, and validation constraints. "
            "Required before any content_create/update so Betty knows the "
            "expected field shape."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": (
                        "Collection slug. For travelpec.com: one of "
                        "'stays', 'villages', 'articles', 'itineraries'."
                    ),
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
    },
}


def emdash_get_collection_schema(args: dict) -> ToolResult:
    """risk_class=read_only."""
    _assert_dict_keys(args, required={"slug"}, optional=set(),
                      tool_name="emdash_get_collection_schema")
    _assert_str(args["slug"], "slug", "emdash_get_collection_schema")
    data = _call(
        "emdash_get_collection_schema",
        "schema_get_collection",
        {"slug": args["slug"]},
    )
    field_count = "?"
    if isinstance(data, dict):
        fields = data.get("fields")
        if isinstance(fields, list):
            field_count = len(fields)
    return _wrap_result(
        "emdash_get_collection_schema",
        data,
        f"Schema for {args['slug']!r}: {field_count} fields.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_list_content
# ---------------------------------------------------------------------------

EMDASH_LIST_CONTENT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_list_content",
        "description": (
            "List content items in a collection with optional status "
            "filtering and pagination. Returns items sorted by the server's "
            "default order. Pass status='published' to list only live items."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug (e.g. 'stays', 'articles').",
                },
                "status": {
                    "type": "string",
                    "enum": ["draft", "published", "scheduled"],
                    "description": "Filter by status. Omit for all non-trashed.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max items to return (default 50, max 100).",
                },
                "cursor": {
                    "type": "string",
                    "description": "Pagination cursor from a previous response.",
                },
            },
            "required": ["collection"],
            "additionalProperties": False,
        },
    },
}


def emdash_list_content(args: dict) -> ToolResult:
    """risk_class=read_only."""
    _assert_dict_keys(
        args,
        required={"collection"},
        optional={"status", "limit", "cursor"},
        tool_name="emdash_list_content",
    )
    _assert_str(args["collection"], "collection", "emdash_list_content")
    if "status" in args:
        if args["status"] not in {"draft", "published", "scheduled"}:
            raise ValueError(
                f"emdash_list_content.status must be one of "
                f"draft|published|scheduled; got {args['status']!r}"
            )
    if "limit" in args:
        if not isinstance(args["limit"], int) or isinstance(args["limit"], bool):
            raise ValueError(
                f"emdash_list_content.limit must be int, "
                f"got {type(args['limit']).__name__}"
            )
        if not 1 <= args["limit"] <= 100:
            raise ValueError(
                f"emdash_list_content.limit must be 1..100, got {args['limit']}"
            )
    if "cursor" in args:
        _assert_str(args["cursor"], "cursor", "emdash_list_content")

    mcp_args = {k: v for k, v in args.items() if k in {"collection", "status", "limit", "cursor"}}
    data = _call("emdash_list_content", "content_list", mcp_args)
    items = data.get("items") if isinstance(data, dict) else None
    count = len(items) if isinstance(items, list) else "?"
    return _wrap_result(
        "emdash_list_content",
        data,
        f"{count} items in {args['collection']!r}"
        + (f" (status={args['status']!r})" if "status" in args else "") + ".",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_get_content
# ---------------------------------------------------------------------------

EMDASH_GET_CONTENT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_get_content",
        "description": (
            "Get a single content item by its ID or slug. Returns the full "
            "data plus a `_rev` token for optimistic concurrency on the next "
            "update. Always call this before emdash_update_content_draft to "
            "obtain a fresh _rev."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": "Collection slug.",
                },
                "id": {
                    "type": "string",
                    "description": "Content item ID (ULID) or slug.",
                },
            },
            "required": ["collection", "id"],
            "additionalProperties": False,
        },
    },
}


def emdash_get_content(args: dict) -> ToolResult:
    """risk_class=read_only."""
    _assert_dict_keys(args, required={"collection", "id"}, optional=set(),
                      tool_name="emdash_get_content")
    _assert_str(args["collection"], "collection", "emdash_get_content")
    _assert_str(args["id"], "id", "emdash_get_content")
    data = _call(
        "emdash_get_content",
        "content_get",
        {"collection": args["collection"], "id": args["id"]},
    )
    # Robust title extraction across possible response shapes:
    # - {"data": {"title": "..."}, ...}  (content_list-style item)
    # - {"title": "...", ...}            (flat field-promotion shape)
    # - {"item": {"data": {"title"...}}} (wrapper variant)
    title = "?"
    if isinstance(data, dict):
        item_data = data.get("data")
        if isinstance(item_data, dict) and "title" in item_data:
            title = item_data["title"]
        elif "title" in data:
            title = data["title"]
        elif isinstance(data.get("item"), dict):
            inner = data["item"].get("data")
            if isinstance(inner, dict) and "title" in inner:
                title = inner["title"]
    return _wrap_result(
        "emdash_get_content",
        data,
        f"Fetched {args['collection']!r}/{args['id']!r}: title={title!r}.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_list_taxonomies
# ---------------------------------------------------------------------------

EMDASH_LIST_TAXONOMIES_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_list_taxonomies",
        "description": (
            "List all taxonomy definitions in the CMS (e.g. categories, "
            "tags). Returns name, label, hierarchical flag for each."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def emdash_list_taxonomies(args: dict) -> ToolResult:
    """risk_class=read_only."""
    _assert_dict_keys(args, required=set(), optional=set(),
                      tool_name="emdash_list_taxonomies")
    data = _call("emdash_list_taxonomies", "taxonomy_list", {})
    # Try common list-bearing keys before falling back to "?".
    count: int | str = "?"
    if isinstance(data, list):
        count = len(data)
    elif isinstance(data, dict):
        for key in ("items", "taxonomies", "results"):
            value = data.get(key)
            if isinstance(value, list):
                count = len(value)
                break
    return _wrap_result(
        "emdash_list_taxonomies",
        data,
        f"{count} taxonomies defined.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_list_taxonomy_terms
# ---------------------------------------------------------------------------

EMDASH_LIST_TAXONOMY_TERMS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_list_taxonomy_terms",
        "description": (
            "List terms within one taxonomy (e.g. all Region values). "
            "Returns slug, label, and parent linkage for hierarchical "
            "taxonomies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "taxonomy": {
                    "type": "string",
                    "description": "Taxonomy name (e.g. 'region', 'best_for').",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max terms (default 50, max 100).",
                },
            },
            "required": ["taxonomy"],
            "additionalProperties": False,
        },
    },
}


def emdash_list_taxonomy_terms(args: dict) -> ToolResult:
    """risk_class=read_only."""
    _assert_dict_keys(
        args,
        required={"taxonomy"},
        optional={"limit"},
        tool_name="emdash_list_taxonomy_terms",
    )
    _assert_str(args["taxonomy"], "taxonomy", "emdash_list_taxonomy_terms")
    if "limit" in args:
        if not isinstance(args["limit"], int) or isinstance(args["limit"], bool):
            raise ValueError(
                f"emdash_list_taxonomy_terms.limit must be int, "
                f"got {type(args['limit']).__name__}"
            )
        if not 1 <= args["limit"] <= 100:
            raise ValueError(
                f"emdash_list_taxonomy_terms.limit must be 1..100, "
                f"got {args['limit']}"
            )

    mcp_args = {k: v for k, v in args.items() if k in {"taxonomy", "limit"}}
    data = _call("emdash_list_taxonomy_terms", "taxonomy_list_terms", mcp_args)
    items = data.get("items") if isinstance(data, dict) else None
    count = len(items) if isinstance(items, list) else "?"
    return _wrap_result(
        "emdash_list_taxonomy_terms",
        data,
        f"{count} terms in taxonomy {args['taxonomy']!r}.",
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Live self-test against the configured EmDash MCP server.

    Read-only — no state mutation. Hits each of the six read tools at
    least once. If EMDASH_TOKEN isn't set, exits with a skip message.
    """
    import os

    print("Phase 4.6 EmDash read tools self-test\n")

    if not os.environ.get("EMDASH_TOKEN"):
        print("  [skip] EMDASH_TOKEN not in env; cannot exercise live MCP. "
              "Set EMDASH_TOKEN and EMDASH_MCP_URL in ~/code/betty/.env.")
        return

    # ---- emdash_list_collections ----
    result = emdash_list_collections({})
    assert result.status == "executed"
    print(f"  [ok] {result.payload['summary']}")

    # Pull a slug to use in subsequent tests.
    items = result.payload["data"]["items"]
    slugs = [c["slug"] for c in items if isinstance(c, dict)]
    assert "stays" in slugs, f"expected stays in {slugs}"

    # ---- emdash_get_collection_schema ----
    result = emdash_get_collection_schema({"slug": "stays"})
    assert result.status == "executed"
    fields = result.payload["data"]["fields"]
    assert any(f["slug"] == "title" for f in fields), "stays must have title field"
    print(f"  [ok] {result.payload['summary']}")

    # ---- emdash_list_content ----
    result = emdash_list_content({"collection": "stays", "status": "published", "limit": 10})
    assert result.status == "executed"
    items = result.payload["data"]["items"]
    print(f"  [ok] {result.payload['summary']}")

    # ---- emdash_get_content (use first published stay) ----
    if items:
        first = items[0]
        result = emdash_get_content({
            "collection": "stays",
            "id": first["id"],
        })
        assert result.status == "executed"
        print(f"  [ok] {result.payload['summary']}")

    # ---- emdash_list_taxonomies ----
    result = emdash_list_taxonomies({})
    assert result.status == "executed"
    print(f"  [ok] {result.payload['summary']}")

    # ---- emdash_list_taxonomy_terms ----
    # Use the first taxonomy if any exist.
    taxonomies = result.payload["data"]
    taxonomy_items = taxonomies.get("items") if isinstance(taxonomies, dict) else taxonomies
    if isinstance(taxonomy_items, list) and taxonomy_items:
        first_taxonomy = taxonomy_items[0]
        name = (
            first_taxonomy.get("name") or first_taxonomy.get("slug")
            if isinstance(first_taxonomy, dict) else None
        )
        if name:
            result = emdash_list_taxonomy_terms({"taxonomy": name, "limit": 10})
            assert result.status == "executed"
            print(f"  [ok] {result.payload['summary']}")

    # ---- Validator: missing required keys ----
    try:
        emdash_get_content({"collection": "stays"})  # missing id
    except ValueError as e:
        assert "missing required keys" in str(e)
        print(f"  [ok] emdash_get_content rejected missing id")
    else:
        raise AssertionError("emdash_get_content should reject missing id")

    # ---- Validator: extra keys ----
    try:
        emdash_list_collections({"unexpected": "value"})
    except ValueError as e:
        assert "unknown keys" in str(e)
        print(f"  [ok] emdash_list_collections rejected extra keys")
    else:
        raise AssertionError("emdash_list_collections should reject extra keys")

    # ---- MCP-side error surfaces as ValueError ----
    try:
        emdash_get_collection_schema({"slug": "definitely_not_a_real_collection"})
    except ValueError as e:
        assert "EmDash MCP returned error" in str(e)
        print(f"  [ok] MCP-side error surfaced as ValueError")
    else:
        raise AssertionError("invalid slug should produce MCP error")

    print("\nemdash_reads.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
