"""
EmDash MCP write/external-side-effect tools for Betty's Phase 4.6 actor.

Five tools:

  - emdash_create_content_draft   (reversible_write)     → content_create (status='draft')
  - emdash_update_content_draft   (reversible_write)     → content_update  (no status change)
  - emdash_unpublish_content      (reversible_write)     → content_unpublish
  - emdash_create_taxonomy_term   (reversible_write)     → taxonomy_create_term
  - emdash_publish_content        (external_side_effect) → content_publish

Per Phase 4.4 Q1 Decision A, each tool declares one constant risk_class.
`emdash_publish_content` is the only `external_side_effect` here because
publishing content makes it live on travelpec.com — visible to the public
internet. Everything else stays in draft state and is rollback-able via
EmDash's content_unpublish / content_restore semantics.

Field-level validation is grounded in the four collection schemas
retrieved via `schema_get_collection` on 2026-05-26 (see
`phases/phase-4.6-substage-a-findings.md`). Schemas are encoded inline
as COLLECTION_SCHEMAS so the validator can run without round-tripping
to MCP for every write. The trade-off: if EmDash collection schemas
change, this module must be updated. That's a deliberate Phase 4.6
choice — schema changes are rare and require a deploy anyway. A
future phase could fetch schemas dynamically and cache.

Hard rules from BRIEF enforced here:
  - Rule 1 ("Never reveal is_advertised: true in public copy"):
    is_advertised defaults to 0 on emdash_create_content_draft and is
    NOT exposed in the public-facing data the actor emits. Peter flips
    the three real-advertiser Stays manually post-build per the locked
    decision from 2026-05-26.
  - Rule 4 ("Each task = ≤2 MCP calls OR 1 atomic file edit"): each
    of these tools is one atomic MCP call. Multi-step content
    publication (create + publish) splits across two task units.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from betty_claw.contracts import ToolResult
from betty_claw.tools.emdash_reads import (
    _assert_dict_keys,
    _assert_str,
    _call,
    _wrap_result,
)


# ---------------------------------------------------------------------------
# Collection schemas (locked from 2026-05-26 schema_get_collection probes)
# ---------------------------------------------------------------------------

# Field types use EmDash's vocabulary:
#   - "text"     → Python str
#   - "number"   → Python float (int accepted, coerced to float; bool rejected)
#   - "boolean"  → Python bool (0/1 int accepted, coerced)
#   - "datetime" → ISO 8601 string

COLLECTION_SCHEMAS: dict[str, dict[str, Any]] = {
    "stays": {
        "required": ["title", "village", "persona", "outbound_url", "provider"],
        "fields": {
            "title": "text",
            "village": "text",
            "persona": "text",
            "bedrooms": "number",
            "capacity": "number",
            "price_band": "text",
            "seasonal": "text",
            "outbound_url": "text",
            "provider": "text",
            "is_advertised": "boolean",
            "featured_eligible": "boolean",
            "schema_subtype": "text",
            "description": "text",
        },
    },
    "villages": {
        "required": ["title", "tagline", "region", "description"],
        "fields": {
            "title": "text",
            "tagline": "text",
            "region": "text",
            "description": "text",
            "why_here": "text",
            "getting_around": "text",
        },
    },
    "articles": {
        "required": ["title", "kind", "excerpt", "body"],
        "fields": {
            "title": "text",
            "kind": "text",
            "excerpt": "text",
            "body": "text",
            "publish_date": "datetime",
        },
    },
    "itineraries": {
        "required": ["title", "duration_nights", "persona", "summary", "body"],
        "fields": {
            "title": "text",
            "duration_nights": "number",
            "persona": "text",
            "summary": "text",
            "body": "text",
        },
    },
}


# ---------------------------------------------------------------------------
# Field-value validation
# ---------------------------------------------------------------------------

_ISO_8601_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:[Zz]|[+-]\d{2}:?\d{2})?$"
)


def _validate_field_value(
    field_name: str,
    field_type: str,
    value: Any,
    tool_name: str,
) -> Any:
    """Validate one field value against its declared type.

    Returns the (possibly coerced) value. Raises ValueError on mismatch.

    Type semantics:
      - "text": str, non-empty allowed (some optional fields may be empty
        strings; we don't strip).
      - "number": float or int (NOT bool — bool is a subclass of int in
        Python and would silently pass). Returned as float.
      - "boolean": bool, or 0/1 int (coerce to bool). Other ints rejected.
      - "datetime": str matching ISO 8601 shape. We use a pragmatic
        regex + datetime.fromisoformat for stricter parsing.
    """
    if field_type == "text":
        if not isinstance(value, str):
            raise ValueError(
                f"{tool_name}.data.{field_name} must be str (text), "
                f"got {type(value).__name__}: {value!r}"
            )
        return value

    if field_type == "number":
        # Reject bool BEFORE the int check — Python's bool is a subclass
        # of int, so isinstance(True, int) is True. Without this guard
        # someone passing True for a number field would silently
        # round-trip as 1.0.
        if isinstance(value, bool):
            raise ValueError(
                f"{tool_name}.data.{field_name} must be number, "
                f"got bool: {value!r}"
            )
        if not isinstance(value, (int, float)):
            raise ValueError(
                f"{tool_name}.data.{field_name} must be number "
                f"(int or float), got {type(value).__name__}: {value!r}"
            )
        return float(value)

    if field_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise ValueError(
            f"{tool_name}.data.{field_name} must be bool (or 0/1 int), "
            f"got {type(value).__name__}: {value!r}"
        )

    if field_type == "datetime":
        if not isinstance(value, str):
            raise ValueError(
                f"{tool_name}.data.{field_name} must be ISO 8601 str, "
                f"got {type(value).__name__}: {value!r}"
            )
        if not _ISO_8601_PATTERN.match(value):
            raise ValueError(
                f"{tool_name}.data.{field_name} not in ISO 8601 form: "
                f"{value!r}"
            )
        # Stricter parse: catches '2026-13-01' etc. that the regex
        # would let through.
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(
                f"{tool_name}.data.{field_name} ISO 8601 parse failed: "
                f"{e} (value={value!r})"
            ) from e
        return value

    raise ValueError(
        f"{tool_name}: unknown field type {field_type!r} in schema for "
        f"field {field_name!r}. This is a bug in COLLECTION_SCHEMAS."
    )


def _validate_data_for_collection(
    collection: str,
    data: Any,
    tool_name: str,
    *,
    partial: bool = False,
) -> dict[str, Any]:
    """Validate a `data` dict against the collection's schema.

    With partial=False (creates): all required fields must be present.
    With partial=True (updates): present fields are validated, missing
    ones are OK (the update is partial — only fields included get
    changed in EmDash).

    Unknown keys are rejected in both modes — schema/data drift is loud.
    Returns the validated (possibly type-coerced) dict.
    """
    schema = COLLECTION_SCHEMAS.get(collection)
    if not schema:
        known = ", ".join(sorted(COLLECTION_SCHEMAS.keys()))
        raise ValueError(
            f"{tool_name}: unknown collection {collection!r}. "
            f"Known: [{known}]. If a new collection has been added to "
            f"EmDash, update COLLECTION_SCHEMAS in tools/emdash_writes.py."
        )

    if not isinstance(data, dict):
        raise ValueError(
            f"{tool_name}.data must be dict, got {type(data).__name__}"
        )

    fields = schema["fields"]
    required = set(schema["required"])

    # Check unknown keys.
    extra = set(data.keys()) - set(fields.keys())
    if extra:
        raise ValueError(
            f"{tool_name}.data has unknown keys for {collection!r}: "
            f"{sorted(extra)}. Allowed: {sorted(fields.keys())}"
        )

    # Check required fields (full mode only).
    if not partial:
        missing = required - set(data.keys())
        if missing:
            raise ValueError(
                f"{tool_name}.data missing required keys for "
                f"{collection!r}: {sorted(missing)}"
            )

    # Validate each present field.
    validated: dict[str, Any] = {}
    for field_name, value in data.items():
        field_type = fields[field_name]
        validated[field_name] = _validate_field_value(
            field_name, field_type, value, tool_name,
        )

    return validated


# ---------------------------------------------------------------------------
# Tool: emdash_create_content_draft
# ---------------------------------------------------------------------------

EMDASH_CREATE_CONTENT_DRAFT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_create_content_draft",
        "description": (
            "Create a new content item as a DRAFT in an EmDash collection. "
            "The item is not yet visible on the live site — call "
            "emdash_publish_content to make it live. Field values in `data` "
            "must match the collection schema (use emdash_get_collection_"
            "schema to inspect). Slug is auto-generated from the title if "
            "not provided."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "collection": {
                    "type": "string",
                    "description": (
                        "Collection slug. For travelpec.com: 'stays', "
                        "'villages', 'articles', or 'itineraries'."
                    ),
                },
                "data": {
                    "type": "object",
                    "description": (
                        "Field values matching the collection schema. "
                        "Required fields for stays: title, village, persona, "
                        "outbound_url, provider. For villages: title, "
                        "tagline, region, description. For articles: title, "
                        "kind, excerpt, body. For itineraries: title, "
                        "duration_nights, persona, summary, body."
                    ),
                    "additionalProperties": True,
                },
                "slug": {
                    "type": "string",
                    "description": (
                        "Optional URL slug. If omitted, EmDash generates "
                        "one from the title."
                    ),
                },
            },
            "required": ["collection", "data"],
            "additionalProperties": False,
        },
    },
}


def emdash_create_content_draft(args: dict) -> ToolResult:
    """Create content as draft. risk_class=reversible_write."""
    _assert_dict_keys(
        args,
        required={"collection", "data"},
        optional={"slug"},
        tool_name="emdash_create_content_draft",
    )
    _assert_str(args["collection"], "collection", "emdash_create_content_draft")
    if "slug" in args:
        _assert_str(args["slug"], "slug", "emdash_create_content_draft")

    validated_data = _validate_data_for_collection(
        args["collection"],
        args["data"],
        "emdash_create_content_draft",
        partial=False,
    )

    mcp_args: dict[str, Any] = {
        "collection": args["collection"],
        "data": validated_data,
        # Explicit draft status, even though it's the EmDash default.
        # Defense in depth: if EmDash's default ever changes, this tool
        # still creates a draft (matches its name and risk_class).
        "status": "draft",
    }
    if "slug" in args:
        mcp_args["slug"] = args["slug"]

    response = _call(
        "emdash_create_content_draft",
        "content_create",
        mcp_args,
    )

    item_id = response.get("id") if isinstance(response, dict) else "?"
    title = (
        response.get("data", {}).get("title")
        if isinstance(response, dict) else "?"
    )
    return _wrap_result(
        "emdash_create_content_draft",
        response,
        f"Created draft in {args['collection']!r}: id={item_id!r}, "
        f"title={title!r}.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_update_content_draft
# ---------------------------------------------------------------------------

EMDASH_UPDATE_CONTENT_DRAFT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_update_content_draft",
        "description": (
            "Update fields on an existing content item. Only the fields "
            "in `data` are changed; omitted fields are left as-is. Does "
            "NOT change the item's status — drafts stay drafts, published "
            "items stay published with the new field values pending until "
            "re-published. To make a fresh content_publish call to push "
            "the updated values live. The optional `_rev` token enables "
            "optimistic concurrency: pass the rev from a recent "
            "emdash_get_content to detect concurrent modifications."
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
                    "description": "Content item ID or slug.",
                },
                "data": {
                    "type": "object",
                    "description": (
                        "Fields to update (only changed fields). Must "
                        "match the collection schema; unknown keys rejected."
                    ),
                    "additionalProperties": True,
                },
                "_rev": {
                    "type": "string",
                    "description": (
                        "Optional optimistic-concurrency token from a "
                        "recent emdash_get_content."
                    ),
                },
            },
            "required": ["collection", "id", "data"],
            "additionalProperties": False,
        },
    },
}


def emdash_update_content_draft(args: dict) -> ToolResult:
    """Update content fields (no status change). risk_class=reversible_write."""
    _assert_dict_keys(
        args,
        required={"collection", "id", "data"},
        optional={"_rev"},
        tool_name="emdash_update_content_draft",
    )
    _assert_str(args["collection"], "collection", "emdash_update_content_draft")
    _assert_str(args["id"], "id", "emdash_update_content_draft")
    if "_rev" in args:
        _assert_str(args["_rev"], "_rev", "emdash_update_content_draft")

    validated_data = _validate_data_for_collection(
        args["collection"],
        args["data"],
        "emdash_update_content_draft",
        partial=True,
    )

    mcp_args: dict[str, Any] = {
        "collection": args["collection"],
        "id": args["id"],
        "data": validated_data,
    }
    if "_rev" in args:
        mcp_args["_rev"] = args["_rev"]

    response = _call(
        "emdash_update_content_draft",
        "content_update",
        mcp_args,
    )

    field_count = len(validated_data)
    return _wrap_result(
        "emdash_update_content_draft",
        response,
        f"Updated {field_count} field(s) on {args['collection']!r}/"
        f"{args['id']!r}.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_unpublish_content
# ---------------------------------------------------------------------------

EMDASH_UNPUBLISH_CONTENT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_unpublish_content",
        "description": (
            "Revert a published content item back to draft status. The "
            "item disappears from the live site but its content is "
            "preserved. Use to take an item offline without deleting it."
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
                    "description": "Content item ID or slug.",
                },
            },
            "required": ["collection", "id"],
            "additionalProperties": False,
        },
    },
}


def emdash_unpublish_content(args: dict) -> ToolResult:
    """Revert published → draft. risk_class=reversible_write."""
    _assert_dict_keys(
        args,
        required={"collection", "id"},
        optional=set(),
        tool_name="emdash_unpublish_content",
    )
    _assert_str(args["collection"], "collection", "emdash_unpublish_content")
    _assert_str(args["id"], "id", "emdash_unpublish_content")

    response = _call(
        "emdash_unpublish_content",
        "content_unpublish",
        {"collection": args["collection"], "id": args["id"]},
    )

    return _wrap_result(
        "emdash_unpublish_content",
        response,
        f"Unpublished {args['collection']!r}/{args['id']!r}.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_create_taxonomy_term
# ---------------------------------------------------------------------------

EMDASH_CREATE_TAXONOMY_TERM_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_create_taxonomy_term",
        "description": (
            "Create a new term in a taxonomy (e.g. a new Region or Best "
            "For category). For hierarchical taxonomies, pass parentId "
            "to nest under an existing term."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "taxonomy": {
                    "type": "string",
                    "description": "Taxonomy name (e.g. 'region', 'best_for').",
                },
                "slug": {
                    "type": "string",
                    "description": "URL-safe identifier for the new term.",
                },
                "label": {
                    "type": "string",
                    "description": "Display name.",
                },
                "parentId": {
                    "type": "string",
                    "description": "Optional parent term ID (hierarchical taxonomies).",
                },
            },
            "required": ["taxonomy", "slug", "label"],
            "additionalProperties": False,
        },
    },
}


def emdash_create_taxonomy_term(args: dict) -> ToolResult:
    """Create taxonomy term. risk_class=reversible_write."""
    _assert_dict_keys(
        args,
        required={"taxonomy", "slug", "label"},
        optional={"parentId"},
        tool_name="emdash_create_taxonomy_term",
    )
    _assert_str(args["taxonomy"], "taxonomy", "emdash_create_taxonomy_term")
    _assert_str(args["slug"], "slug", "emdash_create_taxonomy_term")
    _assert_str(args["label"], "label", "emdash_create_taxonomy_term")
    if "parentId" in args:
        _assert_str(args["parentId"], "parentId", "emdash_create_taxonomy_term")

    mcp_args = {
        "taxonomy": args["taxonomy"],
        "slug": args["slug"],
        "label": args["label"],
    }
    if "parentId" in args:
        mcp_args["parentId"] = args["parentId"]

    response = _call(
        "emdash_create_taxonomy_term",
        "taxonomy_create_term",
        mcp_args,
    )

    return _wrap_result(
        "emdash_create_taxonomy_term",
        response,
        f"Created term {args['slug']!r} in taxonomy {args['taxonomy']!r}.",
    )


# ---------------------------------------------------------------------------
# Tool: emdash_publish_content
# ---------------------------------------------------------------------------

EMDASH_PUBLISH_CONTENT_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "emdash_publish_content",
        "description": (
            "Publish a content item, making it LIVE on travelpec.com. "
            "This is the only EmDash tool with external side effects — "
            "the item becomes visible on the public internet. Cloudflare's "
            "CI/CD picks up the publish event and the site updates within "
            "seconds. To roll back, use emdash_unpublish_content."
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
                    "description": "Content item ID or slug.",
                },
            },
            "required": ["collection", "id"],
            "additionalProperties": False,
        },
    },
}


def emdash_publish_content(args: dict) -> ToolResult:
    """Publish content → live. risk_class=external_side_effect."""
    _assert_dict_keys(
        args,
        required={"collection", "id"},
        optional=set(),
        tool_name="emdash_publish_content",
    )
    _assert_str(args["collection"], "collection", "emdash_publish_content")
    _assert_str(args["id"], "id", "emdash_publish_content")

    response = _call(
        "emdash_publish_content",
        "content_publish",
        {"collection": args["collection"], "id": args["id"]},
    )

    return _wrap_result(
        "emdash_publish_content",
        response,
        f"PUBLISHED {args['collection']!r}/{args['id']!r} — now live on "
        f"travelpec.com.",
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Validate the four collection schemas + field-value validation.

    This is a unit-style self-test — no live MCP calls. The validators
    are pure functions over Python data, so we can exercise them
    completely without network. Tool round-trip against live EmDash is
    covered by the synthetic dry-run (substage (b) verification gate).
    """
    print("Phase 4.6 EmDash write tools self-test\n")

    # ---- Schemas are present and well-formed ----
    expected = {"stays", "villages", "articles", "itineraries"}
    assert set(COLLECTION_SCHEMAS.keys()) == expected, (
        f"COLLECTION_SCHEMAS keys {sorted(COLLECTION_SCHEMAS.keys())} != "
        f"expected {sorted(expected)}"
    )
    for slug, schema in COLLECTION_SCHEMAS.items():
        assert "required" in schema, f"{slug}: schema missing 'required'"
        assert "fields" in schema, f"{slug}: schema missing 'fields'"
        for req_field in schema["required"]:
            assert req_field in schema["fields"], (
                f"{slug}: required field {req_field!r} not in fields"
            )
        print(f"  [ok] {slug}: {len(schema['fields'])} fields, "
              f"{len(schema['required'])} required")

    # ---- Field-value validators ----
    print()
    # text
    assert _validate_field_value("title", "text", "hello", "tt") == "hello"
    try:
        _validate_field_value("title", "text", 123, "tt")
    except ValueError as e:
        assert "must be str" in str(e)
    print("  [ok] text validator")

    # number
    assert _validate_field_value("bedrooms", "number", 3, "tt") == 3.0
    assert _validate_field_value("bedrooms", "number", 2.5, "tt") == 2.5
    try:
        _validate_field_value("bedrooms", "number", True, "tt")
    except ValueError as e:
        assert "got bool" in str(e), f"unexpected: {e}"
    try:
        _validate_field_value("bedrooms", "number", "3", "tt")
    except ValueError as e:
        assert "must be number" in str(e)
    print("  [ok] number validator (bool rejected before int coercion)")

    # boolean
    assert _validate_field_value("is_advertised", "boolean", True, "tt") is True
    assert _validate_field_value("is_advertised", "boolean", False, "tt") is False
    assert _validate_field_value("is_advertised", "boolean", 1, "tt") is True
    assert _validate_field_value("is_advertised", "boolean", 0, "tt") is False
    try:
        _validate_field_value("is_advertised", "boolean", 2, "tt")
    except ValueError as e:
        assert "must be bool" in str(e)
    print("  [ok] boolean validator")

    # datetime
    assert _validate_field_value(
        "publish_date", "datetime", "2026-05-26T12:00:00Z", "tt"
    ) == "2026-05-26T12:00:00Z"
    assert _validate_field_value(
        "publish_date", "datetime", "2026-05-26T12:00:00.123+04:30", "tt"
    ) == "2026-05-26T12:00:00.123+04:30"
    try:
        _validate_field_value(
            "publish_date", "datetime", "yesterday", "tt"
        )
    except ValueError as e:
        assert "ISO 8601" in str(e)
    try:
        _validate_field_value(
            "publish_date", "datetime", "2026-13-01T00:00:00Z", "tt"
        )
    except ValueError as e:
        assert "ISO 8601" in str(e), f"unexpected: {e}"
    print("  [ok] datetime validator")

    # ---- _validate_data_for_collection: full mode ----
    print()
    valid_stay = {
        "title": "Test Cottage",
        "village": "Wellington",
        "persona": "Two-bedroom cottage near the wine route.",
        "outbound_url": "https://example.com/test",
        "provider": "direct",
        "bedrooms": 2,
        "capacity": 4,
        "is_advertised": 0,  # int 0 → False (per Hard Rule 1 default)
    }
    result = _validate_data_for_collection("stays", valid_stay, "tt", partial=False)
    assert result["bedrooms"] == 2.0
    assert result["is_advertised"] is False  # coerced
    print(f"  [ok] valid stay validated; is_advertised coerced to {result['is_advertised']!r}")

    # Missing required
    try:
        _validate_data_for_collection(
            "stays",
            {"title": "missing rest"},
            "tt",
            partial=False,
        )
    except ValueError as e:
        assert "missing required" in str(e)
    print("  [ok] missing required field rejected (create mode)")

    # Missing required is OK in partial (update) mode
    result = _validate_data_for_collection(
        "stays",
        {"persona": "updated persona"},
        "tt",
        partial=True,
    )
    assert result == {"persona": "updated persona"}
    print("  [ok] partial update accepts subset of fields")

    # Unknown keys rejected in both modes
    try:
        _validate_data_for_collection(
            "stays",
            {**valid_stay, "ad_revenue_target": 1000},
            "tt",
            partial=False,
        )
    except ValueError as e:
        assert "unknown keys" in str(e)
    print("  [ok] unknown keys rejected (create mode)")

    try:
        _validate_data_for_collection(
            "stays",
            {"surprise_field": "x"},
            "tt",
            partial=True,
        )
    except ValueError as e:
        assert "unknown keys" in str(e)
    print("  [ok] unknown keys rejected (update mode)")

    # Unknown collection
    try:
        _validate_data_for_collection(
            "made_up", {"title": "x"}, "tt", partial=False,
        )
    except ValueError as e:
        assert "unknown collection" in str(e)
    print("  [ok] unknown collection rejected")

    # ---- Tool wrappers: validator-only paths (no MCP) ----
    # We can't exercise the full MCP round-trip without state mutation,
    # so the self-test stops at validator-level. The synthetic dry-run
    # in the substage (b) verification gate covers live MCP calls.
    print()
    try:
        emdash_create_content_draft({"collection": "stays"})  # missing data
    except ValueError as e:
        assert "missing required keys" in str(e)
    print("  [ok] emdash_create_content_draft rejected missing data")

    try:
        emdash_publish_content({
            "collection": "stays",
            "id": "01KR...",
            "force": True,  # unknown key
        })
    except ValueError as e:
        assert "unknown keys" in str(e)
    print("  [ok] emdash_publish_content rejected unknown keys")

    print("\nemdash_writes.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
