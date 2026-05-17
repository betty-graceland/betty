"""
Betty's Ollama client.

Thin httpx wrapper around Ollama's /api/chat endpoint. This module is
deliberately minimal: it owns transport, error mapping, and message
formatting. It does NOT own conversation state, system prompts,
retrieval context assembly, or anything Betty-specific — that lives
in actor.py.

The chat endpoint (not /api/generate) is used because it has first-class
support for multi-turn role-based messages and is what Ollama's
KV-prefix caching is tuned for. Per Friction 4 mitigation, the
caller must always present the same system message and conversation
prefix in the same order across turns or caching is lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Literal

import httpx


# ---------- Configuration ----------

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "betty-generalist:latest"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_NUM_CTX = 8192  # match the 8k context budget locked in architecture
DEFAULT_NUM_PREDICT = 1024  # enough for Qwen 3 thinking + ~500 token response


# ---------- Types ----------

Role = Literal["system", "user", "assistant"]


@dataclass
class ChatMessage:
    """One message in a chat conversation."""
    role: Role
    content: str

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResponse:
    """The result of a chat() call.

    `content` is the visible response Betty produces for the user.
    `thinking` is the model's internal reasoning, captured separately
    by Ollama when using reasoning-capable models (Qwen 3 family).
    Most callers should ignore `thinking` — it's exposed for debugging
    and for Stage 9's reflector to inspect Betty's reasoning trace.
    """
    content: str
    model: str
    prompt_eval_count: int  # tokens in the input
    eval_count: int          # tokens generated
    eval_duration_seconds: float
    load_duration_seconds: float
    total_duration_seconds: float
    thinking: str = ""
    done_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def tokens_per_second(self) -> float:
        if self.eval_duration_seconds <= 0:
            return 0.0
        return self.eval_count / self.eval_duration_seconds


# ---------- Errors ----------

class OllamaError(Exception):
    """Base class for Ollama client errors."""


class OllamaConnectionError(OllamaError):
    """Ollama daemon is unreachable."""


class OllamaModelNotFoundError(OllamaError):
    """Requested model not in `ollama list`."""


class OllamaTimeoutError(OllamaError):
    """Request exceeded the timeout."""


# ---------- Client ----------

class OllamaClient:
    """Synchronous Ollama client. One instance per process is enough."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OllamaClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ---------- Health ----------

    def ping(self) -> list[str]:
        """Return the list of available model names. Raises if Ollama is down."""
        try:
            r = self._client.get("/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except httpx.ConnectError as e:
            raise OllamaConnectionError(
                f"Cannot reach Ollama at {self.base_url}. Is it running?"
            ) from e

    # ---------- Chat ----------

    def chat(
        self,
        messages: list[ChatMessage],
        model: str = DEFAULT_MODEL,
        num_ctx: int = DEFAULT_NUM_CTX,
        temperature: float = 0.7,
        num_predict: int = DEFAULT_NUM_PREDICT,
        stop: list[str] | None = None,
        extra_options: dict[str, Any] | None = None,
    ) -> ChatResponse:
        """Run a non-streaming chat completion.

        For Stage 3 we use non-streaming because it's simpler and the
        actor loop blocks on the full response anyway. Streaming can
        come later if the review UI wants token-by-token rendering.
        """
        options: dict[str, Any] = {
            "num_ctx": num_ctx,
            "temperature": temperature,
            "num_predict": num_predict,
        }
        if stop:
            options["stop"] = stop
        if extra_options:
            options.update(extra_options)

        payload = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "options": options,
        }

        try:
            r = self._client.post("/api/chat", json=payload)
        except httpx.ConnectError as e:
            raise OllamaConnectionError(
                f"Cannot reach Ollama at {self.base_url}. Is it running?"
            ) from e
        except httpx.ReadTimeout as e:
            raise OllamaTimeoutError(
                f"Ollama request timed out after {self._client.timeout.read}s"
            ) from e

        if r.status_code == 404:
            raise OllamaModelNotFoundError(
                f"Model {model!r} not found. Run `ollama list` to see available."
            )
        r.raise_for_status()

        data = r.json()
        # Ollama nests assistant content under message.content for /api/chat.
        # For reasoning-capable models (Qwen 3 family), an additional
        # message.thinking field holds the internal monologue separately.
        msg = data.get("message") or {}
        return ChatResponse(
            content=msg.get("content", ""),
            thinking=msg.get("thinking", ""),
            model=data.get("model", model),
            prompt_eval_count=data.get("prompt_eval_count", 0),
            eval_count=data.get("eval_count", 0),
            eval_duration_seconds=data.get("eval_duration", 0) / 1e9,
            load_duration_seconds=data.get("load_duration", 0) / 1e9,
            total_duration_seconds=data.get("total_duration", 0) / 1e9,
            done_reason=data.get("done_reason", ""),
            raw=data,
        )


# ---------- Self-test ----------

def _self_test() -> None:
    """Verify Ollama is reachable and betty-generalist responds correctly."""
    print("Pinging Ollama...")
    with OllamaClient() as ollama:
        models = ollama.ping()
        print(f"  Available models: {len(models)}")
        for m in models:
            print(f"    - {m}")

        assert "betty-generalist:latest" in models, (
            "betty-generalist:latest not found in `ollama list`"
        )

        print("\nSending test chat to betty-generalist...")
        response = ollama.chat(
            messages=[
                ChatMessage(role="system", content="You are a terse assistant."),
                ChatMessage(role="user", content="What is 2 + 2? Answer in one word."),
            ],
            model="betty-generalist:latest",
            # Use the default num_predict (1024) — Qwen 3 needs budget
            # for both thinking and visible content.
        )
        print(f"  Response:   {response.content!r}")
        if response.thinking:
            preview = response.thinking[:120].replace(chr(10), " ")
            print(f"  Thinking:   {preview}... ({len(response.thinking)} chars)")
        print(f"  Model:      {response.model}")
        print(f"  Done:       {response.done_reason!r}")
        print(f"  Tokens in:  {response.prompt_eval_count}")
        print(f"  Tokens out: {response.eval_count}")
        print(f"  Eval:       {response.eval_duration_seconds:.2f}s")
        print(f"  Load:       {response.load_duration_seconds:.2f}s")
        print(f"  Total:      {response.total_duration_seconds:.2f}s")
        print(f"  Speed:      {response.tokens_per_second:.1f} tok/s")

        assert response.content.strip(), (
            f"Empty response from model. done_reason={response.done_reason!r}. "
            f"If 'length', increase num_predict so the model has budget "
            f"for both thinking and visible content."
        )
        assert response.eval_count > 0, "No tokens generated"
        print("\n  Ollama client self-test passed.")


if __name__ == "__main__":
    _self_test()
