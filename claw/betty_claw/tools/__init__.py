"""
Tool registry for Betty's actor.

Each tool is a (callable, schema, risk_class) triple. The callable validates
its arguments and writes a proposal JSON file to disk; the schema is the
OpenAI/Ollama function-calling shape that Qwen sees so it knows how to
invoke the tool; the risk_class is the mechanical metadata the adapter
reads to construct the OB1 Envelope before the Judge sees it.

Tools must validate their own arguments before generating a call_id or
writing to disk. See draft_email.py for the canonical pattern. The
schema and validator MUST agree on required fields, types, and
additionalProperties policy — drift between them produces calls that
Qwen will emit but the tool will reject, or calls the tool would accept
but Qwen doesn't know how to construct.

The TOOLS registry is the single source of truth for what tools exist
AND for their risk classification. Per Phase 4.4 Q1 Decisions A+B
(locked 2026-05-24):

  - risk_class is a per-tool constant declared here in the registry.
    Tools that would span multiple risk classes must be split into
    multiple atomic tools, each with its own constant risk_class.

  - The adapter populates risk_class onto the Envelope at envelope-
    construction time by reading TOOLS[tool_name].risk_class. The
    actor (Qwen) never reasons about risk_class — never sees it,
    never emits it.

Phase 4.3 wrapped each entry in a ToolEntry (callable + schema) because
the actor needs to pass schemas to Ollama for native tool-calling.
Phase 4.5 adds risk_class as a third required field. No defaults; every
tool must declare its risk class explicitly — that's the structural
forcing function that produces atomic, intent-driven tools instead of
monolithic CRUD wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from betty_claw.tools.draft_email import DRAFT_EMAIL_SCHEMA, draft_email

from betty_claw.tools.filesystem import (
    LIST_DIRECTORY_SCHEMA,
    READ_FILE_SCHEMA,
    WRITE_FILE_SCHEMA,
    list_directory,
    read_file,
    write_file,
)
from betty_claw.tools.git_ops import (
    GIT_COMMIT_ALL_SCHEMA,
    GIT_DIFF_SCHEMA,
    GIT_PUSH_SCHEMA,
    GIT_STATUS_SCHEMA,
    git_commit_all,
    git_diff,
    git_push,
    git_status,
)
from betty_claw.tools.emdash_reads import (
    EMDASH_GET_COLLECTION_SCHEMA_SCHEMA,
    EMDASH_GET_CONTENT_SCHEMA,
    EMDASH_LIST_COLLECTIONS_SCHEMA,
    EMDASH_LIST_CONTENT_SCHEMA,
    EMDASH_LIST_TAXONOMIES_SCHEMA,
    EMDASH_LIST_TAXONOMY_TERMS_SCHEMA,
    emdash_get_collection_schema,
    emdash_get_content,
    emdash_list_collections,
    emdash_list_content,
    emdash_list_taxonomies,
    emdash_list_taxonomy_terms,
)
from betty_claw.tools.emdash_writes import (
    EMDASH_CREATE_COLLECTION_SCHEMA,
    EMDASH_CREATE_CONTENT_DRAFT_SCHEMA,
    EMDASH_CREATE_FIELD_SCHEMA,
    EMDASH_CREATE_TAXONOMY_TERM_SCHEMA,
    EMDASH_PUBLISH_CONTENT_SCHEMA,
    EMDASH_UNPUBLISH_CONTENT_SCHEMA,
    EMDASH_UPDATE_CONTENT_DRAFT_SCHEMA,
    emdash_create_collection,
    emdash_create_content_draft,
    emdash_create_field,
    emdash_create_taxonomy_term,
    emdash_publish_content,
    emdash_unpublish_content,
    emdash_update_content_draft,
)
from betty_claw.tools.airbnb_parser import (
    PARSE_AIRBNB_DOSSIER_SCHEMA,
    parse_airbnb_dossier,
)

from betty_claw.contracts import RiskClass, ToolResult


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool: its callable, its Ollama-facing schema, and its
    risk class.

    The callable accepts a dict of arguments (the raw, untrusted shape
    produced by Qwen's tool-call output) and returns a ToolResult.

    The schema is the OpenAI/Ollama function-calling shape that Qwen
    sees. It MUST match the callable's validation rules — same
    required fields, same types, same additionalProperties policy.

    The risk_class is one of the four locked OB1 risk classes. It is a
    per-tool constant; if a tool's design spans multiple risk classes
    it must be split into multiple atomic tools. The adapter reads
    this value to populate Envelope.risk_class before the Judge sees
    the proposal. The actor never sees or reasons about risk_class.
    """
    callable: Callable[[dict], ToolResult]
    schema: dict
    risk_class: RiskClass


TOOLS: dict[str, ToolEntry] = {
    # ----- Phase 4.3: proposal-writing tool -----
    # draft_email writes a proposal JSON file to disk. No external side
    # effect (no SMTP send), but it does mutate local filesystem state.
    # Reversible: deleting the proposal file undoes it. The Stage 5 send
    # tool will be a separate registration with risk_class="external_side_effect"
    # and an adapter-level legal boilerplate footer (operational boundary #2
    # from Phase 4.4 scoping decisions).
    "draft_email": ToolEntry(
        callable=draft_email,
        schema=DRAFT_EMAIL_SCHEMA,
        risk_class="reversible_write",
    ),

    # ----- Phase 4.6: filesystem tools (Astro source side) -----
    # Allow-list bounded by BETTY_SITE_DIR + BETTY_DOCS_DIR; path
    # traversal blocked structurally by the validators.
    "read_file": ToolEntry(
        callable=read_file,
        schema=READ_FILE_SCHEMA,
        risk_class="read_only",
    ),
    "list_directory": ToolEntry(
        callable=list_directory,
        schema=LIST_DIRECTORY_SCHEMA,
        risk_class="read_only",
    ),
    "write_file": ToolEntry(
        callable=write_file,
        schema=WRITE_FILE_SCHEMA,
        risk_class="reversible_write",
    ),

    # ----- Phase 4.6: git tools (Astro source side) -----
    # Hard Rule 3 (BRIEF) enforced structurally:
    #   - git_commit_all refuses on `main`
    #   - git_push hard-codes refspec to HEAD:vic-overnight
    "git_status": ToolEntry(
        callable=git_status,
        schema=GIT_STATUS_SCHEMA,
        risk_class="read_only",
    ),
    "git_diff": ToolEntry(
        callable=git_diff,
        schema=GIT_DIFF_SCHEMA,
        risk_class="read_only",
    ),
    "git_commit_all": ToolEntry(
        callable=git_commit_all,
        schema=GIT_COMMIT_ALL_SCHEMA,
        risk_class="reversible_write",
    ),
    "git_push": ToolEntry(
        callable=git_push,
        schema=GIT_PUSH_SCHEMA,
        risk_class="external_side_effect",
    ),

    # ----- Phase 4.6: EmDash MCP read tools (content side) -----
    # All Judge-skip per Phase 4.5 Decision C. SKIP_READ_ONLY rows
    # still land in judge_decisions so the audit trail is complete.
    "emdash_list_collections": ToolEntry(
        callable=emdash_list_collections,
        schema=EMDASH_LIST_COLLECTIONS_SCHEMA,
        risk_class="read_only",
    ),
    "emdash_get_collection_schema": ToolEntry(
        callable=emdash_get_collection_schema,
        schema=EMDASH_GET_COLLECTION_SCHEMA_SCHEMA,
        risk_class="read_only",
    ),
    "emdash_list_content": ToolEntry(
        callable=emdash_list_content,
        schema=EMDASH_LIST_CONTENT_SCHEMA,
        risk_class="read_only",
    ),
    "emdash_get_content": ToolEntry(
        callable=emdash_get_content,
        schema=EMDASH_GET_CONTENT_SCHEMA,
        risk_class="read_only",
    ),
    "emdash_list_taxonomies": ToolEntry(
        callable=emdash_list_taxonomies,
        schema=EMDASH_LIST_TAXONOMIES_SCHEMA,
        risk_class="read_only",
    ),
    "emdash_list_taxonomy_terms": ToolEntry(
        callable=emdash_list_taxonomy_terms,
        schema=EMDASH_LIST_TAXONOMY_TERMS_SCHEMA,
        risk_class="read_only",
    ),

    # ----- Phase 4.6: EmDash MCP write tools (content side) -----
    "emdash_create_content_draft": ToolEntry(
        callable=emdash_create_content_draft,
        schema=EMDASH_CREATE_CONTENT_DRAFT_SCHEMA,
        risk_class="reversible_write",
    ),
    "emdash_update_content_draft": ToolEntry(
        callable=emdash_update_content_draft,
        schema=EMDASH_UPDATE_CONTENT_DRAFT_SCHEMA,
        risk_class="reversible_write",
    ),
    "emdash_unpublish_content": ToolEntry(
        callable=emdash_unpublish_content,
        schema=EMDASH_UNPUBLISH_CONTENT_SCHEMA,
        risk_class="reversible_write",
    ),
    "emdash_create_taxonomy_term": ToolEntry(
        callable=emdash_create_taxonomy_term,
        schema=EMDASH_CREATE_TAXONOMY_TERM_SCHEMA,
        risk_class="reversible_write",
    ),

    # ----- Phase 4.6 substage (c): schema DDL tools (smoke test T01) -----
    # Add new collections / fields to EmDash. Both reversible_write —
    # server-side CMS state changes, reversible via admin UI delete; no
    # public-facing impact until content lands.
    "emdash_create_collection": ToolEntry(
        callable=emdash_create_collection,
        schema=EMDASH_CREATE_COLLECTION_SCHEMA,
        risk_class="reversible_write",
    ),
    "emdash_create_field": ToolEntry(
        callable=emdash_create_field,
        schema=EMDASH_CREATE_FIELD_SCHEMA,
        risk_class="reversible_write",
    ),

    # ----- Phase 4.6: EmDash external-side-effect tool -----
    # emdash_publish_content makes content live on travelpec.com — the
    # only tool that pushes data into the public-facing internet.
    "emdash_publish_content": ToolEntry(
        callable=emdash_publish_content,
        schema=EMDASH_PUBLISH_CONTENT_SCHEMA,
        risk_class="external_side_effect",
    ),

    # ----- Phase 4.6.1: domain-specific parser tools -----
    # parse_airbnb_dossier reads a research dossier (YAML frontmatter +
    # markdown body) and returns a Stays-compatible dict. read_only —
    # no MCP calls, no filesystem writes. Bridges the 35 scraped Airbnb
    # dossiers into the content-population pipeline.
    "parse_airbnb_dossier": ToolEntry(
        callable=parse_airbnb_dossier,
        schema=PARSE_AIRBNB_DOSSIER_SCHEMA,
        risk_class="read_only",
    ),
}


def get_tool(name: str) -> Callable[[dict], ToolResult]:
    """Look up a tool callable by name.

    Preserves the Phase 4.2 callable-only return signature for any
    future consumer that only needs to dispatch. New consumers should
    consider working with TOOLS[name] directly to get the schema too.

    Raises KeyError with a helpful message if the tool is not registered.
    """
    if name not in TOOLS:
        available = ", ".join(sorted(TOOLS.keys()))
        raise KeyError(
            f"Unknown tool {name!r}. Registered tools: {available}"
        )
    return TOOLS[name].callable


def get_ollama_tools_schema() -> list[dict]:
    """Return all registered tool schemas as a list ready for Ollama.

    Pass the return value directly as the `tools=` argument to
    OllamaClient.chat(). The order matches sorted(TOOLS.keys()) for
    stable, deterministic ordering across calls — useful for KV cache
    consistency since the actor passes this list on every tool-enabled
    turn.
    """
    return [TOOLS[name].schema for name in sorted(TOOLS.keys())]


__all__ = ["TOOLS", "ToolEntry", "get_tool", "get_ollama_tools_schema"]


def _self_test() -> None:
    """Verify the registry exposes the expected shape and contents."""
    print("Phase 4.5 tool registry self-test")
    print()

    print(f"Registered tools: {sorted(TOOLS.keys())}")
    assert "draft_email" in TOOLS, "draft_email should be registered"
    assert isinstance(TOOLS["draft_email"], ToolEntry), (
        f"TOOLS values must be ToolEntry, got {type(TOOLS['draft_email'])}"
    )
    print("  [ok] TOOLS is dict[str, ToolEntry]")

    # Phase 4.5: every tool MUST declare risk_class. No defaults, no
    # optionality — that's the structural forcing function. Verify every
    # registered tool has a valid risk_class string.
    valid_risk_classes = {
        "read_only",
        "reversible_write",
        "external_side_effect",
        "high_risk",
    }
    for name, entry in TOOLS.items():
        assert hasattr(entry, "risk_class"), (
            f"ToolEntry for {name!r} missing risk_class field"
        )
        assert entry.risk_class in valid_risk_classes, (
            f"Tool {name!r} has invalid risk_class={entry.risk_class!r}; "
            f"must be one of {sorted(valid_risk_classes)}"
        )
        print(f"  [ok] {name!r} risk_class={entry.risk_class!r}")

    # Phase 4.5 contract: draft_email is reversible_write — writes a proposal
    # file but no external side effect, deletable via filesystem.
    assert TOOLS["draft_email"].risk_class == "reversible_write", (
        f"draft_email must be reversible_write per Phase 4.5 kickoff, "
        f"got {TOOLS['draft_email'].risk_class!r}"
    )
    print("  [ok] draft_email risk_class is reversible_write per kickoff")

    fn = get_tool("draft_email")
    assert callable(fn), "get_tool should return a callable"
    print("  [ok] get_tool('draft_email') returns callable")

    try:
        get_tool("nonexistent_tool")
    except KeyError as e:
        print(f"  [ok] get_tool raises KeyError on unknown: {str(e)[:80]}")
    else:
        print("  [FAIL] get_tool did not raise on unknown")
        raise SystemExit(1)

    schemas = get_ollama_tools_schema()
    assert isinstance(schemas, list), "schemas must be a list"
    assert len(schemas) == len(TOOLS), (
        f"schema count {len(schemas)} != tool count {len(TOOLS)}"
    )
    print(f"  [ok] get_ollama_tools_schema returns {len(schemas)} schema(s)")

    for schema in schemas:
        assert schema.get("type") == "function", (
            f"schema must have type='function', got {schema.get('type')!r}"
        )
        fn_block = schema.get("function") or {}
        assert "name" in fn_block, "schema.function.name missing"
        assert "description" in fn_block, "schema.function.description missing"
        params = fn_block.get("parameters") or {}
        assert params.get("type") == "object", (
            f"parameters.type must be 'object', got {params.get('type')!r}"
        )
        assert "properties" in params, "parameters.properties missing"
        assert "required" in params, "parameters.required missing"
        print(f"  [ok] schema for {fn_block['name']!r} has expected shape")

    for name, entry in TOOLS.items():
        schema_name = entry.schema["function"]["name"]
        assert schema_name == name, (
            f"registry key {name!r} != schema function.name {schema_name!r}"
        )
    print(f"  [ok] schema function.name matches registry key for all tools")

    de_schema = TOOLS["draft_email"].schema
    required = set(de_schema["function"]["parameters"]["required"])
    assert required == {"to", "subject", "body"}, (
        f"draft_email schema required mismatch: {required}"
    )
    print(f"  [ok] draft_email schema required fields: {sorted(required)}")

    print()
    print("tools/__init__.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
