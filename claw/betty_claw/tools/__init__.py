"""
Tool registry for Betty's actor.

Each tool is a callable that accepts a dict of arguments (the raw, untrusted
shape produced by Qwen's tool-call output) and returns a ToolResult.

Tools must validate their own arguments before generating a call_id or writing
to disk. See draft_email.py for the canonical pattern.

The TOOLS registry is the single source of truth for what tools exist. The
Judge (Phase 4.3) reads from this registry to build its system prompt; the
actor reads from it to dispatch calls.
"""

from __future__ import annotations

from typing import Callable, Dict

from betty_claw.tools.draft_email import draft_email

# Tool registry. Keyed by the tool name that appears in ToolCall.tool_name
# and that Qwen emits in its tool-calling output. Values are the callables.
#
# To register a new tool:
#   1. Add the module under betty_claw/tools/
#   2. Import the callable above
#   3. Add the (name, callable) entry below
#
# The tool name in this dict MUST match the name Qwen sees in the tools
# parameter handed to Ollama. Mismatches surface as silent dispatch failures.
TOOLS: Dict[str, Callable[[dict], "ToolResult"]] = {
    "draft_email": draft_email,
}


def get_tool(name: str) -> Callable[[dict], "ToolResult"]:
    """
    Look up a tool by name. Raises KeyError with a helpful message if the
    tool is not registered.
    """
    if name not in TOOLS:
        available = ", ".join(sorted(TOOLS.keys()))
        raise KeyError(
            f"Unknown tool {name!r}. Registered tools: {available}"
        )
    return TOOLS[name]


__all__ = ["TOOLS", "get_tool"]
