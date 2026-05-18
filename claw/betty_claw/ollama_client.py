"""
Ollama client wrapper. Sync, single-process.

Used by the actor (Stage 3) and the actor's tool-calling integration
(Phase 4.3). The wrapper is intentionally thin: it builds the /api/chat
payload, sends it, parses the response into a typed dataclass. It does
not retry, stream, or batch.

## Tool-calling protocol (Phase 4.3 extension)

The wrapper supports Ollama's native tool-calling. Pass `tools=` to
`chat()` with a list of JSON-schema tool specs (OpenAI function-calling
shape). When Qwen 3 decides to invoke a tool, the response's
`tool_calls` field is populated with the structured invocations.

To feed a tool result back into the conversation, append a ChatMessage
with `role="tool"` whose `content` is the result text (in Betty's case,
the Judge's rejection reasoning). Qwen sees its previous tool_call AND
the tool message and generates the next turn with that context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import httpx


# ---------- Configuration ----------

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MODEL = "betty-generalist:latest"
DEFAULT_NUM_CTX = 16384
DEFAULT_NUM_PREDICT = 1024


# ---------- Types ----------

Role = Literal["system", "user", "assistant", "tool"]
"""Roles supported by Ollama's chat API. Phase 4.3 adds 'tool' for
feeding tool-call results (specifically Judge rejection reasoning)
back into the conversation history."""


@dataclass
class ToolCall:
    """One tool invocation emitted by the model.

    Distinct from betty_claw.types.ToolCall (which carries a UUID4
    call_id and is what the Judge sees). This dataclass is the wire
    shape coming out of Ollama; the actor translates these into
    betty_claw.types.ToolCall objects by minting a call_id and
    correlating with the tool registry.
    """
    name: str
    arguments: dict[str, Any]


@dataclass
class ChatMessage:
    """One message in a chat conversation.

    For role='assistant' messages that include tool calls, `content`
    may be empty and `tool_calls` carries the structured invocations.
    For role='tool' messages, `content` is the tool-call result
    (Phase 4.3: the Judge's rejection reasoning) fed back to the
    model so it can course-correct on the next turn.
    """
    role: Role
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [
                {"function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return out


@dataclass
class ChatResponse:
    """The result of a chat() call.

    `content` is the visible response Betty produces for the user.
    `thinking` is the model's internal reasoning, captured separately
    by Ollama when using reasoning-capable models (Qwen 3 family).
    Most callers should ignore `thinking` — it's exposed for debugging
    and for Stage 9's reflector to inspect Betty's reasoning trace.

    `tool_calls` (Phase 4.3) is populated when the model invoked one
    or more tools instead of (or in addition to) emitting text. An
    empty list means no tool calls; callers branch on that.
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
    tool_calls: list[ToolCall] = field(default_factory=list)
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
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Run a non-streaming chat completion.

        For Stage 3 we use non-streaming because it's simpler and the
        actor loop blocks on the full response anyway. Streaming can
        come later if the review UI wants token-by-token rendering.

        Phase 4.3: `tools` accepts a list of OpenAI function-calling
        schema dicts. When provided, Qwen may emit tool_calls in
        ChatResponse instead of (or alongside) text content.
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

        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "options": options,
        }
        if tools:
            payload["tools"] = tools

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
        # When the model invokes tools, message.tool_calls is a list of
        # {function: {name, arguments}} dicts.
        msg = data.get("message") or {}

        tool_calls_raw = msg.get("tool_calls") or []
        tool_calls: list[ToolCall] = []
        for tc in tool_calls_raw:
            fn = tc.get("function") or {}
            tool_calls.append(
                ToolCall(
                    name=fn.get("name", ""),
                    arguments=fn.get("arguments") or {},
                )
            )

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
            tool_calls=tool_calls,
            raw=data,
        )


# ---------- Self-test ----------

def _self_test() -> None:
    """Verify Ollama is reachable, betty-generalist responds, and
    tool-calling works end-to-end against the local model.

    Three scenarios:
      1. Stage 3 baseline: text response, content non-empty.
      2. Phase 4.3 tool-calling: model is given a draft_email schema and
         a request that should obviously invoke it. We assert at least
         one tool_call comes back with the right tool name.
      3. Phase 4.3 tool-result feedback: send the model an assistant
         message with a tool_call and a role='tool' rejection, verify
         the model accepts the message shape and responds again.
    """
    print("Pinging Ollama...")
    with OllamaClient() as ollama:
        models = ollama.ping()
        print(f"  Available models: {len(models)}")
        for m in models:
            print(f"    - {m}")

        assert "betty-generalist:latest" in models, (
            "betty-generalist:latest not found in `ollama list`"
        )

        # ---- Scenario 1: Stage 3 baseline ----
        print("\nScenario 1: Stage 3 baseline text response")
        response = ollama.chat(
            messages=[
                ChatMessage(role="system", content="You are a terse assistant."),
                ChatMessage(role="user", content="What is 2 + 2? Answer in one word."),
            ],
            model="betty-generalist:latest",
        )
        print(f"  Response:   {response.content!r}")
        if response.thinking:
            preview = response.thinking[:120].replace(chr(10), " ")
            print(f"  Thinking:   {preview}... ({len(response.thinking)} chars)")
        print(f"  Tool calls: {len(response.tool_calls)}")
        print(f"  Tokens out: {response.eval_count}")
        print(f"  Speed:      {response.tokens_per_second:.1f} tok/s")

        assert response.content.strip(), (
            f"Empty response from model. done_reason={response.done_reason!r}. "
            f"If 'length', increase num_predict."
        )
        assert response.eval_count > 0, "No tokens generated"
        assert response.tool_calls == [], "Expected no tool calls in Scenario 1"
        print("  [ok] text response received, no tool calls")

        # ---- Scenario 2: Tool-calling ----
        print("\nScenario 2: tool-calling against draft_email schema")
        draft_email_schema = {
            "type": "function",
            "function": {
                "name": "draft_email",
                "description": (
                    "Draft an email for the user to review before sending. "
                    "Writes the proposal to disk and returns a proposal_path. "
                    "Does NOT send the email."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Recipient email address.",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Subject line.",
                        },
                        "body": {
                            "type": "string",
                            "description": "Email body in plain text.",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            },
        }

        response = ollama.chat(
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "You are Betty, an assistant that uses tools when "
                        "appropriate. When the user asks you to send or draft "
                        "an email, you MUST call draft_email."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content=(
                        "Please draft an email to alice@example.com with "
                        "subject 'Tomorrow's meeting' and body confirming "
                        "we're meeting at 2pm Eastern."
                    ),
                ),
            ],
            model="betty-generalist:latest",
            tools=[draft_email_schema],
        )
        print(f"  Response content: {response.content!r}")
        print(f"  Tool calls: {len(response.tool_calls)}")
        for tc in response.tool_calls:
            print(f"    - name={tc.name!r}")
            print(f"      arguments={tc.arguments!r}")

        assert len(response.tool_calls) >= 1, (
            f"Expected at least one tool call, got {len(response.tool_calls)}. "
            f"Response content: {response.content!r}"
        )
        assert any(tc.name == "draft_email" for tc in response.tool_calls), (
            f"Expected a draft_email call, got tool names: "
            f"{[tc.name for tc in response.tool_calls]}"
        )
        # Soft check: the arguments should at least be a dict with some keys.
        first_call = next(tc for tc in response.tool_calls if tc.name == "draft_email")
        assert isinstance(first_call.arguments, dict), (
            f"Tool arguments should be a dict, got {type(first_call.arguments)}"
        )
        assert first_call.arguments, "Tool arguments dict is empty"
        print("  [ok] tool call emitted and parsed correctly")

        # ---- Scenario 3: Tool-result feedback ----
        print("\nScenario 3: feeding a Judge rejection back via role='tool'")
        # Build a conversation where the assistant previously called draft_email
        # and the Judge rejected; we want to verify Ollama accepts the message
        # shape and the model responds (even if just with text).
        prior_call = first_call
        response = ollama.chat(
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "You are Betty. You previously tried to call draft_email "
                        "but the Judge rejected the call. Read the rejection "
                        "reasoning and either try a corrected tool call or "
                        "explain to the user why you cannot proceed."
                    ),
                ),
                ChatMessage(
                    role="user",
                    content="Please draft an email to alice@example.com.",
                ),
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[prior_call],
                ),
                ChatMessage(
                    role="tool",
                    content=(
                        "Judge rejected: the email body is too vague. Add a "
                        "specific time and a one-line confirmation."
                    ),
                ),
            ],
            model="betty-generalist:latest",
            tools=[draft_email_schema],
        )
        print(f"  Response content (first 200 chars): {response.content[:200]!r}")
        print(f"  Tool calls: {len(response.tool_calls)}")
        # We accept EITHER outcome: model retries with a tool call OR explains
        # in text. What we're verifying here is that the role='tool' message
        # shape doesn't error out — both outcomes prove the conversation
        # history was accepted.
        accepted = bool(response.content.strip()) or len(response.tool_calls) > 0
        assert accepted, (
            f"Model returned no content and no tool calls. "
            f"done_reason={response.done_reason!r}, raw={response.raw}"
        )
        print("  [ok] role='tool' message accepted, model responded")

        print("\nollama_client.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
