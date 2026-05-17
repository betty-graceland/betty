"""
Betty's actor loop.

Stitches the three components Stage 3 has produced:
  1. Markdown OS (AGENTS, USER, MEMORY) — system prompt, KV-cached prefix
  2. OpenBrain retrieval — top-K relevant chunks for the user's turn
  3. Ollama (betty-generalist) — generates the response

The actor is read-only conversation. It cannot send email, modify
files, or take any action that touches the outside world. Stage 4
adds the Judge adapter and the first action tool.

## KV cache discipline

The system prompt is built once from AGENTS + USER + MEMORY in that
order and reused across every turn. Reordering the files or changing
their content between turns invalidates Ollama's KV prefix cache and
costs us full prompt re-evaluation. Don't modify the Markdown OS
files mid-conversation unless you genuinely intend the cache miss.

## Retrieval context placement

Retrieved chunks are formatted into the user message, not the system
message. This is deliberate: it keeps the system prefix stable for
caching while letting Betty see "here's what I just retrieved for
this specific question" inline with what the user asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from betty_etl.retrieval import RetrievalHit, retrieve

from betty_openclaw.ollama_client import (
    ChatMessage,
    ChatResponse,
    OllamaClient,
)


# ---------- Configuration ----------

# Markdown OS load order is LOCKED. Reordering breaks KV prefix caching
# and Friction 4 mitigation. AGENTS rarely changes, USER changes weekly,
# MEMORY changes per turn — stable to volatile is the cache-friendly order.
MARKDOWN_OS_LOAD_ORDER = ("AGENTS.md", "USER.md", "MEMORY.md")

BETTY_OS_DIR = Path(__file__).parent / "betty_os"

DEFAULT_ACTOR_MODEL = "betty-generalist:latest"
DEFAULT_RETRIEVAL_LIMIT = 5
DEFAULT_MIN_SIMILARITY = 0.5
DEFAULT_WORKSPACE = "betty-dev"


# ---------- Types ----------

@dataclass
class ActorTurn:
    """One turn through the actor: user input in, response and trace out."""
    user_message: str
    response: str
    hits: list[RetrievalHit] = field(default_factory=list)
    chat_response: ChatResponse | None = None

    @property
    def latency_seconds(self) -> float:
        if self.chat_response is None:
            return 0.0
        return self.chat_response.total_duration_seconds

    @property
    def tokens_per_second(self) -> float:
        if self.chat_response is None:
            return 0.0
        return self.chat_response.tokens_per_second


# ---------- Markdown OS loading ----------

def load_markdown_os(directory: Path = BETTY_OS_DIR) -> str:
    """Load the three Markdown OS files in locked order, concatenated.

    Returns one string suitable for use as the system prompt. The
    delimiter between files is "\n\n---\n\n" so Betty can recognize
    section boundaries if she introspects them.
    """
    parts: list[str] = []
    for filename in MARKDOWN_OS_LOAD_ORDER:
        path = directory / filename
        if not path.exists():
            raise FileNotFoundError(
                f"Markdown OS file missing: {path}. "
                f"Cannot construct system prompt without all of "
                f"{MARKDOWN_OS_LOAD_ORDER}."
            )
        parts.append(path.read_text().strip())
    return "\n\n---\n\n".join(parts)


# ---------- Retrieval context formatting ----------

def format_retrieval_context(hits: list[RetrievalHit]) -> str:
    """Format retrieval hits as a context block for the user message.

    Empty list -> empty string (caller can omit the section entirely).
    Hits get presented with title, similarity, and content. Citation
    by title is the contract — AGENTS.md tells Betty to cite by
    document title when she uses retrieved passages.
    """
    if not hits:
        return ""

    lines = ["## Retrieved context\n"]
    for i, hit in enumerate(hits, start=1):
        title = hit.document_title or hit.document_uri
        lines.append(
            f"### [{i}] {title} (similarity {hit.similarity:.2f})"
        )
        lines.append(hit.content.strip())
        lines.append("")  # blank line between hits
    return "\n".join(lines).rstrip()


def build_user_message(
    user_message: str,
    hits: list[RetrievalHit],
) -> str:
    """Combine retrieved context + user message into one user-role string."""
    context = format_retrieval_context(hits)
    if not context:
        return user_message
    return f"{context}\n\n---\n\n## User question\n\n{user_message}"


# ---------- The actor turn ----------

def actor_turn(
    user_message: str,
    *,
    workspace_id: str = DEFAULT_WORKSPACE,
    model: str = DEFAULT_ACTOR_MODEL,
    retrieval_limit: int = DEFAULT_RETRIEVAL_LIMIT,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ollama: OllamaClient | None = None,
) -> ActorTurn:
    """Run one full actor turn: retrieve -> assemble -> generate.

    Args:
        user_message: What Peter just said to Betty.
        workspace_id: Which OpenBrain tenant to retrieve against.
        model: Ollama model name. Default is the locked actor.
        retrieval_limit: Max chunks to surface as context. Larger
            means more grounding but more tokens consumed.
        min_similarity: Drop retrieval hits below this cosine
            similarity. Tighter (0.6+) for noise reduction, looser
            (0.4) when you want Betty to see borderline matches.
        ollama: Optionally pass a shared OllamaClient. If None, a
            fresh one is created and closed per call.

    Returns:
        ActorTurn with response text, retrieval trace, and metrics.
    """
    # 1. Retrieve relevant chunks
    hits = retrieve(
        user_message,
        workspace_id=workspace_id,
        limit=retrieval_limit,
        min_similarity=min_similarity,
    )

    # 2. Build prompt
    system_prompt = load_markdown_os()
    user_content = build_user_message(user_message, hits)

    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_content),
    ]

    # 3. Call Ollama
    owns_client = ollama is None
    if ollama is None:
        ollama = OllamaClient()

    try:
        chat_response = ollama.chat(messages=messages, model=model)
    finally:
        if owns_client:
            ollama.close()

    return ActorTurn(
        user_message=user_message,
        response=chat_response.content,
        hits=hits,
        chat_response=chat_response,
    )


# ---------- Self-test ----------

def _self_test() -> None:
    """Run a real turn against the ingested Attention paper.

    Proves the full chain: Markdown OS loads -> retrieval works ->
    Ollama responds -> Betty cites her source.
    """
    from betty_etl.db import close_pool

    print("Loading Markdown OS...")
    system_prompt = load_markdown_os()
    print(f"  System prompt size: {len(system_prompt):,} chars")
    print(f"  Section count:      {system_prompt.count('---') + 1}")

    test_question = (
        "Based on what's in my OpenBrain, can you summarize the core "
        "idea behind self-attention in one paragraph? Cite your source."
    )
    print(f"\nUser question:\n  {test_question}")

    print("\nRunning actor turn...")
    turn = actor_turn(test_question)

    print("\n--- Retrieved chunks ---")
    if not turn.hits:
        print("  (none above threshold)")
    else:
        for i, hit in enumerate(turn.hits, start=1):
            title = hit.document_title or hit.document_uri
            preview = hit.content[:100].replace("\n", " ")
            print(f"  [{i}] sim={hit.similarity:.3f}  {title}")
            print(f"      {preview}...")

    print("\n--- Betty's response ---")
    print(turn.response)

    print("\n--- Metrics ---")
    if turn.chat_response is not None:
        cr = turn.chat_response
        print(f"  Tokens in:  {cr.prompt_eval_count}")
        print(f"  Tokens out: {cr.eval_count}")
        print(f"  Eval:       {cr.eval_duration_seconds:.2f}s")
        print(f"  Total:      {cr.total_duration_seconds:.2f}s")
        print(f"  Speed:      {cr.tokens_per_second:.1f} tok/s")

    assert turn.response.strip(), "Empty response from Betty"
    assert len(turn.hits) > 0, "Retrieval returned nothing — read path broken?"
    print("\nActor self-test passed.")

    close_pool()


if __name__ == "__main__":
    _self_test()
