"""
claw/betty_claw/anthropic_client.py

Thin httpx wrapper around Anthropic's Messages API. Mirrors the
ollama_client.py pattern: no SDK, sync client, explicit error types,
caller owns retries.

Stage 4 use case: the Judge calls this once per ToolCall proposal.
Failure semantics matter — per Stage 4 safety property #5, a failed
Judge call must fail-safe (tool does NOT execute), so this client
raises distinguishable exceptions rather than returning sentinel values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Module configuration
# ---------------------------------------------------------------------------

# Load .env from repo root (~/code/betty/.env). Pathing is anchored to this
# file so the loader works regardless of CWD when the module is imported.
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_PATH)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Opus 4.7 pricing. Adjust here if Anthropic changes rates.
# $15.00 / 1M input tokens, $75.00 / 1M output tokens.
INPUT_COST_PER_MTOK = 15.00
OUTPUT_COST_PER_MTOK = 75.00


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class AnthropicClientError(Exception):
    """Base for all Anthropic client failures. Judge catches this broadly."""


class AnthropicAPIError(AnthropicClientError):
    """Network failure, timeout, or non-2xx HTTP response."""


class AnthropicResponseError(AnthropicClientError):
    """2xx response but body is malformed or missing required fields."""


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnthropicResponse:
    """Structured result from a Messages API call.

    Token counts feed JudgeVerdict.input_tokens / output_tokens and the
    cost_usd computation. content is the assistant's text reply.
    """
    content: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    model: str
    stop_reason: str


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------

def _compute_cost(input_tokens: int, output_tokens: int) -> float:
    """Cost in USD for an Opus 4.7 call given token counts.

    Float precision is fine — this feeds a $5/day operational cap, not
    accounting. See Stage 4 design note in types.py.
    """
    input_cost = (input_tokens / 1_000_000) * INPUT_COST_PER_MTOK
    output_cost = (output_tokens / 1_000_000) * OUTPUT_COST_PER_MTOK
    return input_cost + output_cost


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class AnthropicClient:
    """Sync httpx wrapper around the Messages API.

    Single-purpose: send one message, get one response back with token
    accounting. No streaming, no tool-use protocol (Anthropic-side tools
    are not Stage 4 — the Judge takes plain text in, plain text out).
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = 60.0,
    ):
        # Pull from .env if not passed explicitly. Explicit kwargs win for
        # test injection.
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("ANTHROPIC_JUDGE_MODEL")

        if not self.api_key:
            raise AnthropicClientError(
                "ANTHROPIC_API_KEY not set. Check ~/code/betty/.env"
            )
        if not self.model:
            raise AnthropicClientError(
                "ANTHROPIC_JUDGE_MODEL not set. Check ~/code/betty/.env"
            )

        self.timeout_s = timeout_s
        self._headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def send(
        self,
        prompt: str,
        max_tokens: int = 1024,
        system: str | None = None,
    ) -> AnthropicResponse:
        """Send a single user message; return structured response.

        Raises AnthropicAPIError on transport/HTTP failure.
        Raises AnthropicResponseError on malformed 2xx body.
        """
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            body["system"] = system

        try:
            with httpx.Client(timeout=self.timeout_s) as client:
                resp = client.post(API_URL, headers=self._headers, json=body)
        except httpx.HTTPError as e:
            raise AnthropicAPIError(f"Transport failure: {e!r}") from e

        if resp.status_code != 200:
            raise AnthropicAPIError(
                f"HTTP {resp.status_code}: {resp.text[:500]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise AnthropicResponseError(f"Non-JSON body: {e!r}") from e

        # Anthropic Messages API shape:
        # { content: [{type: "text", text: "..."}], usage: {input_tokens, output_tokens}, ... }
        try:
            content_blocks = data["content"]
            text_parts = [
                b["text"] for b in content_blocks if b.get("type") == "text"
            ]
            content = "".join(text_parts)
            usage = data["usage"]
            input_tokens = int(usage["input_tokens"])
            output_tokens = int(usage["output_tokens"])
            stop_reason = data.get("stop_reason", "unknown")
            model = data.get("model", self.model)
        except (KeyError, TypeError, ValueError) as e:
            raise AnthropicResponseError(
                f"Malformed response body: {e!r} | body={str(data)[:500]}"
            ) from e

        cost_usd = _compute_cost(input_tokens, output_tokens)

        return AnthropicResponse(
            content=content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            model=model,
            stop_reason=stop_reason,
        )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Ping the configured Judge model with 'Respond with the single word OK'.

    Asserts:
      - content == "OK" (exact match)
      - input_tokens > 0
      - output_tokens > 0
      - cost_usd > 0

    Prints token counts and computed cost so the operator can eyeball
    pricing before real spend begins.
    """
    client = AnthropicClient()
    print(f"Self-test: model={client.model}")

    resp = client.send(
        prompt="Respond with the single word OK. No punctuation, no other text.",
        max_tokens=10,
    )

    print(f"  content       : {resp.content!r}")
    print(f"  input_tokens  : {resp.input_tokens}")
    print(f"  output_tokens : {resp.output_tokens}")
    print(f"  cost_usd      : ${resp.cost_usd:.6f}")
    print(f"  stop_reason   : {resp.stop_reason}")
    print(f"  model         : {resp.model}")

    assert resp.content.strip() == "OK", (
        f"Expected 'OK', got {resp.content!r}"
    )
    assert resp.input_tokens > 0, "input_tokens should be > 0"
    assert resp.output_tokens > 0, "output_tokens should be > 0"
    assert resp.cost_usd > 0, "cost_usd should be > 0"

    print("Self-test PASSED")


if __name__ == "__main__":
    _self_test()
