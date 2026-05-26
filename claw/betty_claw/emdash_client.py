"""
claw/betty_claw/emdash_client.py

Thin httpx wrapper around the EmDash MCP server. Mirrors the
anthropic_client.py pattern: sync httpx, explicit error types,
caller owns retries.

Phase 4.6 use case: Betty's emdash_* tools call this to read and
write content in the EmDash CMS that backs travelpec.com (and future
sites). The MCP transport is Streamable HTTP (JSON-RPC 2.0 over HTTPS
with SSE responses). Failure semantics matter — Betty's tools must
distinguish between transport failures (retry candidate),
JSON-RPC errors (don't retry, return error to actor), and tool-level
errors signaled inside the result envelope (e.g., "Tool not found").

The MCP server requires both `application/json` AND `text/event-stream`
in the Accept header. Responses are SSE — one or more `event: message\n
data: {jsonrpc...}\n\n` blocks. Phase 4.6 assumes single-event responses
(true for all current EmDash tools); if EmDash later streams partial
results, the SSE parser here will need to accumulate.

Two response shapes:

  1. `tools/list` → `{"result": {"tools": [...]}, "jsonrpc": "2.0", "id": N}`
     The list lives directly under result.tools.

  2. `tools/call` → `{"result": {"content": [{"type": "text", "text": "JSON-string"}]}, ...}`
     The actual data is JSON-encoded inside content[0].text. If the
     called tool itself errored, `result.isError == true` and
     content[0].text holds the error message.

The client unpacks both shapes so callers get a clean dict/list.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Module configuration
# ---------------------------------------------------------------------------

# Load .env from repo root (~/code/betty/.env). Same pattern as
# anthropic_client.py — anchored to this file's location, not CWD.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH)

# Default MCP URL; overrideable via env or constructor.
DEFAULT_MCP_URL = "https://travelpec.com/_emdash/api/mcp"

# Request timeouts. EmDash content_list against the live site has been
# observed under 1s; 30s is generous headroom for slow responses (e.g.,
# a cold worker on Cloudflare's edge) without hanging the actor loop.
DEFAULT_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class EmdashClientError(Exception):
    """Base for all EmDash client failures. Tools catch this broadly."""


class EmdashAPIError(EmdashClientError):
    """Transport failure: network error, timeout, or non-2xx HTTP."""


class EmdashResponseError(EmdashClientError):
    """2xx response but the SSE body is malformed or missing required fields."""


class EmdashMCPError(EmdashClientError):
    """JSON-RPC error or tool-level isError from the MCP server.

    `code` is the JSON-RPC error code (e.g. -32601 for method not found,
    -32602 for invalid params, -32000 for application errors including
    "Tool not found"). `message` is the human-readable description.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"MCP error {code}: {message}")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class EmdashClient:
    """Sync httpx wrapper around the EmDash MCP server.

    Construction reads `EMDASH_TOKEN` and `EMDASH_MCP_URL` from env
    (loaded from `~/code/betty/.env`). Both can be overridden via
    constructor args for testing or multi-site setups.

    Single-purpose: call one MCP method, return the unpacked result.
    No batching, no async, no streaming (single-event SSE assumption).
    """

    def __init__(
        self,
        token: str | None = None,
        url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        resolved_token = token or os.environ.get("EMDASH_TOKEN")
        if not resolved_token:
            raise EmdashClientError(
                "EMDASH_TOKEN not set. Add it to ~/code/betty/.env or pass "
                "explicitly to EmdashClient(token=...)."
            )

        self._token = resolved_token
        self._url = url or os.environ.get("EMDASH_MCP_URL") or DEFAULT_MCP_URL
        self._timeout = timeout

    @property
    def url(self) -> str:
        """Exposed for diagnostic logging. Token is never exposed."""
        return self._url

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the full roster of MCP tools the server exposes.

        Each entry is a dict with at minimum `name`, `description`, and
        `inputSchema`. Returned in server order (deterministic).
        """
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/list",
        })
        result = self._unpack_jsonrpc(response)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise EmdashResponseError(
                f"tools/list response missing 'tools' list: {result!r}"
            )
        return tools

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Call an MCP tool and return its unpacked result.

        For tools/call responses, the actual data is JSON-encoded inside
        result.content[0].text. This method parses that wrapping and
        returns the inner data structure directly.

        If the tool returns isError=true (e.g., "Tool not found",
        validation failures), raises EmdashMCPError with code -32000
        and the server's error message.

        If the request itself fails JSON-RPC validation (method not
        found, etc.), raises EmdashMCPError with the server's code.
        """
        response = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
            },
        })
        result = self._unpack_jsonrpc(response)

        # Tool-level isError flag. The error message is inside content[0].text.
        if result.get("isError"):
            err_text = self._extract_content_text(result) or "(no error message)"
            raise EmdashMCPError(-32000, err_text)

        # Normal content-wrapped response. Parse JSON from text payload.
        return self._extract_content_data(result)

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    def _next_id(self) -> str:
        """Generate a unique JSON-RPC request id. UUID4 hex.

        The server echoes this back; useful for log correlation but not
        load-bearing for response parsing — we treat each POST as a
        request/response pair, not a multiplexed channel.
        """
        return uuid.uuid4().hex

    def _post(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON-RPC envelope, return the parsed JSON-RPC response.

        Wraps HTTP errors, SSE parse errors, and timeout into typed
        exceptions. The returned dict still includes JSON-RPC wrapper
        fields (jsonrpc, id, result, error) — call _unpack_jsonrpc to
        extract the application-level result.
        """
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        try:
            response = httpx.post(
                self._url,
                json=envelope,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as e:
            raise EmdashAPIError(f"Request timed out after {self._timeout}s: {e}") from e
        except httpx.HTTPError as e:
            raise EmdashAPIError(f"Transport failure: {e}") from e

        if response.status_code != 200:
            raise EmdashAPIError(
                f"HTTP {response.status_code}: {response.text[:500]!r}"
            )

        # SSE parse. The server returns one or more `event: message\n
        # data: {json}\n\n` blocks. Phase 4.6 assumes single-event;
        # if multiple events arrive, we take the last `data:` line as
        # the final result (consistent with MCP "final result" semantics).
        data_lines = [
            line[len("data: "):]
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        if not data_lines:
            raise EmdashResponseError(
                f"No `data:` line in SSE response. "
                f"Got Content-Type={response.headers.get('content-type')!r}, "
                f"body[:500]={response.text[:500]!r}"
            )

        payload_text = data_lines[-1]
        try:
            return json.loads(payload_text)
        except json.JSONDecodeError as e:
            raise EmdashResponseError(
                f"SSE data line is not valid JSON: {e}. "
                f"text[:500]={payload_text[:500]!r}"
            ) from e

    def _unpack_jsonrpc(self, response: dict[str, Any]) -> dict[str, Any]:
        """Strip the JSON-RPC envelope. Raise on JSON-RPC errors."""
        if "error" in response:
            err = response["error"]
            if isinstance(err, dict):
                raise EmdashMCPError(
                    code=err.get("code", -32000),
                    message=err.get("message", str(err)),
                )
            raise EmdashMCPError(-32000, str(err))

        result = response.get("result")
        if not isinstance(result, dict):
            raise EmdashResponseError(
                f"JSON-RPC response missing 'result' object: {response!r}"
            )
        return result

    def _extract_content_text(self, result: dict[str, Any]) -> str | None:
        """Extract the text payload from a tools/call result.content[0]."""
        content = result.get("content")
        if not isinstance(content, list) or not content:
            return None
        first = content[0]
        if not isinstance(first, dict) or first.get("type") != "text":
            return None
        text = first.get("text")
        return text if isinstance(text, str) else None

    def _extract_content_data(self, result: dict[str, Any]) -> Any:
        """Extract and JSON-parse the application data from a tools/call result.

        EmDash wraps responses as content[0].text containing JSON. This
        method unwraps to the inner dict/list. If the text isn't JSON
        (rare; would indicate a server bug or a non-standard tool),
        returns the raw text under a `_raw_text` key.
        """
        text = self._extract_content_text(result)
        if text is None:
            # Some tools may return non-text content (images, etc.). Pass
            # the raw result through so callers can handle the shape.
            return result

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw_text": text}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Live self-test against the configured EmDash MCP server.

    Read-only probes only — does not mutate any server state.

    Scenarios:
      1. Client construction: env vars present, token loaded.
      2. tools/list: returns a non-empty roster of tools.
      3. schema_list_collections: returns the four expected collections
         (stays, villages, articles, itineraries).
      4. Error handling: calling a nonexistent tool raises EmdashMCPError.

    Cost: zero (EmDash MCP is self-hosted; no per-call billing).
    """
    print("Phase 4.6 EmDash MCP client self-test\n")

    # Scenario 1: construction.
    client = EmdashClient()
    print(f"  URL: {client.url}")
    print(f"  [ok] client constructed; token loaded from env\n")

    # Scenario 2: tools/list.
    tools = client.list_tools()
    assert isinstance(tools, list), f"expected list, got {type(tools).__name__}"
    assert len(tools) > 0, "tools/list returned an empty roster"
    tool_names = sorted(t["name"] for t in tools)
    print(f"  tools/list returned {len(tools)} tools")
    print(f"  first 5: {tool_names[:5]}")
    print(f"  [ok] tools/list works\n")

    # Sanity-check the expected tools exist. These are the names betty_claw
    # wrappers will call against — if any are missing, Phase 4.6 implementation
    # has a wrong assumption to fix.
    expected = {
        "schema_list_collections", "schema_get_collection",
        "content_list", "content_get", "content_create", "content_update",
        "content_publish", "content_unpublish",
        "taxonomy_list", "taxonomy_list_terms", "taxonomy_create_term",
    }
    missing = expected - set(tool_names)
    assert not missing, (
        f"Expected MCP tools missing from server: {sorted(missing)}. "
        f"Phase 4.6 wrappers assume these exist."
    )
    print(f"  [ok] all 11 expected MCP tools present\n")

    # Scenario 3: schema_list_collections.
    collections = client.call_tool("schema_list_collections")
    # Response shape: {"items": [{"slug": "stays", ...}, ...]} based on probe data.
    items = (
        collections.get("items") if isinstance(collections, dict)
        else collections
    )
    assert isinstance(items, list), (
        f"schema_list_collections returned unexpected shape: {collections!r}"
    )
    slugs = sorted(c.get("slug") for c in items if isinstance(c, dict))
    print(f"  collection slugs: {slugs}")
    expected_collections = {"stays", "villages", "articles", "itineraries"}
    found_collections = set(slugs)
    missing_collections = expected_collections - found_collections
    assert not missing_collections, (
        f"Expected collections missing: {sorted(missing_collections)}. "
        f"Found: {slugs}"
    )
    print(f"  [ok] all four expected collections present\n")

    # Scenario 4: nonexistent tool error.
    try:
        client.call_tool("definitely_not_a_real_tool")
    except EmdashMCPError as e:
        print(f"  [ok] EmdashMCPError raised on unknown tool: code={e.code} "
              f"msg={e.message[:80]!r}\n")
    else:
        print("  [FAIL] calling unknown tool did not raise")
        raise SystemExit(1)

    print("emdash_client.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
