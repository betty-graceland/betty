"""
Tool registry for Betty's actor.

Each tool is a (callable, schema) pair. The callable validates its
arguments and writes a proposal JSON file to disk; the schema is the
OpenAI/Ollama function-calling shape that Qwen sees so it knows how
to invoke the tool.

Tools must validate their own arguments before generating a call_id or
writing to disk. See draft_email.py for the canonical pattern. The
schema and validator MUST agree on required fields, types, and
additionalProperties policy — drift between them produces calls that
Qwen will emit but the tool will reject, or calls the tool would accept
but Qwen doesn't know how to construct.

The TOOLS registry is the single source of truth for what tools exist.
The actor (Phase 4.3) reads TOOLS to dispatch calls and to surface
schemas to Ollama via get_ollama_tools_schema().

Phase 4.3 note: this module's shape changed from Phase 4.2. Phase 4.2
had TOOLS: Dict[str, Callable] only. Phase 4.3 wraps each entry in a
ToolEntry (callable + schema) because the actor now needs to pass
schemas to Ollama for native tool-calling. No existing code consumed
the Phase 4.2 shape, so this is an additive change in spirit despite
the type signature change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from betty_claw.tools.draft_email import DRAFT_EMAIL_SCHEMA, draft_email
from betty_claw.types import ToolResult


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool: its callable and its Ollama-facing schema.

    The callable accepts a dict of arguments (the raw, untrusted shape
    produced by Qwen's tool-call output) and returns a ToolResult.

    The schema is the OpenAI/Ollama function-calling shape that Qwen
    sees. It MUST match the callable's validation rules — same
    required fields, same types, same additionalProperties policy.
    """
    callable: Callable[[dict], ToolResult]
    schema: dict


TOOLS: dict[str, ToolEntry] = {
    "draft_email": ToolEntry(
        callable=draft_email,
        schema=DRAFT_EMAIL_SCHEMA,
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
    print("Phase 4.3 tool registry self-test")
    print()

    print(f"Registered tools: {sorted(TOOLS.keys())}")
    assert "draft_email" in TOOLS, "draft_email should be registered"
    assert isinstance(TOOLS["draft_email"], ToolEntry), (
        f"TOOLS values must be ToolEntry, got {type(TOOLS['draft_email'])}"
    )
    print("  [ok] TOOLS is dict[str, ToolEntry]")

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
