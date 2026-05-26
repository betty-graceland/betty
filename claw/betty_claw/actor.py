"""
Betty's actor loop.

Stitches together the Stage 3 retrieval pipeline with the Phase 4.3
tool-calling and Judge layers:

  1. Markdown OS (AGENTS, USER, MEMORY) — system prompt, KV-cached prefix
  2. OpenBrain retrieval — top-K relevant chunks for the user's turn
  3. Ollama (betty-generalist) — generates the response, possibly with
     tool calls when a Judge is supplied
  4. Judge (Phase 4.3) — evaluates each tool call; rejection feedback
     is plumbed back to Qwen via role='tool' messages
  5. Tool registry — schemas surfaced to Ollama, callables dispatched
     after Judge approval (Phase 4.3 means the callable writes a
     proposal; actual execution is Stage 5+)

Stage 3 behavior is preserved exactly when no Judge is supplied: no
tools are surfaced to Ollama, no inner loop runs, ActorTurn.outcome
defaults to "text".

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

## Inner loop (Phase 4.3)

When a Judge is supplied, the actor enters an inner loop:

  generate -> branch on tool_calls ->
    if no tool_calls: return text response (terminal)
    if tool_calls:
      dispatch tool (validates + writes proposal)
      ask Judge for verdict
      if approve: return synthesized response with proposal_path (terminal)
      if reject (cost_usd > 0): feed reasoning back to Qwen, loop
      if reject (cost_usd == 0): halt — terminal (breaker/cap/corrupt)

The loop is bounded by max_iterations = rejection_limit + 1 as defense
in depth. In practice the Judge's circuit breaker trips first.

Terminal-vs-substantive rejection is detected structurally via
verdict.cost_usd == 0.0. A no-API rejection always means the Judge
short-circuited (breaker tripped, cap exceeded, or ledger corrupt) and
all three halt the turn. String matching on verdict.reasoning is used
only for classifying the outcome label, not for control flow.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from betty_etl.retrieval import RetrievalHit, retrieve

from betty_claw import judge_decisions
from betty_claw.judge import Judge
from betty_claw.ollama_client import (
    ChatMessage,
    ChatResponse,
    OllamaClient,
)
from betty_claw.tools import TOOLS, get_ollama_tools_schema, get_tool
from betty_claw.contracts import Envelope, JudgeVerdict, ToolCall, ToolResult


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


# Outcomes the actor can produce. "text" is the Stage 3 baseline; the
# others are Phase 4.3 + 4.5 tool-calling terminal states.
ActorOutcome = Literal[
    "text",              # Qwen emitted text, no tool calls
    "tool_approved",     # Judge approved a tool call; proposal written
    "tool_read_only",    # Phase 4.5: read_only tool executed directly, Judge skipped
    "breaker_tripped",   # Per-turn rejection limit hit; halt and escalate
    "cap_exceeded",      # Daily spend cap would be exceeded; halt
    "ledger_corrupt",    # Spend ledger unparseable; halt
    "max_iterations",    # Outer defense bound hit (should never happen in practice)
]


# ---------- Types ----------

@dataclass
class ActorTurn:
    """One turn through the actor: user input in, response and trace out.

    Phase 4.3 adds outcome/proposal_path/judge_verdicts/iterations
    fields with defaults. Stage 3 callers see the same shape as before
    plus default-value additions.
    """
    user_message: str
    response: str
    hits: list[RetrievalHit] = field(default_factory=list)
    chat_response: ChatResponse | None = None
    # Phase 4.3 additions:
    outcome: ActorOutcome = "text"
    proposal_path: str | None = None
    judge_verdicts: list[JudgeVerdict] = field(default_factory=list)
    iterations: int = 1

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


# ---------- Inner-loop helpers ----------

def _classify_short_circuit(reasoning: str) -> ActorOutcome:
    """Map a no-API Judge rejection's reasoning to an outcome label.

    Used for the outcome field only; the actor's terminal-vs-substantive
    branching is structural (cost_usd == 0.0), not string-based.
    """
    lower = reasoning.lower()
    if "circuit breaker" in lower:
        return "breaker_tripped"
    if "cap" in lower:
        return "cap_exceeded"
    if "corrupt" in lower:
        return "ledger_corrupt"
    # Unknown no-API rejection — fall back to breaker_tripped so the
    # outcome is still terminal and visible to the caller.
    return "breaker_tripped"


def _synthesize_approval_response(
    wire_call_name: str,
    proposal_path: str,
) -> str:
    """User-facing message when the Judge approves a tool call.

    Phase 4.3 writes proposals but does not execute them. Stage 5+
    will add a send step; for now we surface the proposal path so
    Peter can inspect what Betty would have done.
    """
    return (
        f"I prepared a {wire_call_name} proposal for your review. "
        f"The full details are at: {proposal_path}"
    )


def _synthesize_read_only_response(
    wire_call_name: str,
    tool_result: ToolResult,
) -> str:
    """User-facing message when a read_only tool returns data directly.

    Phase 4.5: read_only tools skip the Judge per Q1 Decisions A+B.
    Their ToolResult.payload IS the answer to the user; the actor
    surfaces it inline rather than pointing at a proposal file.

    The surface here is intentionally minimal. Phase 4.6 will likely
    refine this once we have real read-only tools (read_file, git_status,
    etc.) producing structured payloads worth richer formatting.
    """
    payload = tool_result.payload
    if "summary" in payload:
        return f"{wire_call_name}: {payload['summary']}"
    return f"{wire_call_name} result: {payload!r}"


# ---------- The actor turn ----------

def actor_turn(
    user_message: str,
    *,
    workspace_id: str = DEFAULT_WORKSPACE,
    model: str = DEFAULT_ACTOR_MODEL,
    retrieval_limit: int = DEFAULT_RETRIEVAL_LIMIT,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    ollama: OllamaClient | None = None,
    judge: Judge | None = None,
    max_iterations: int | None = None,
) -> ActorTurn:
    """Run one full actor turn: retrieve -> assemble -> generate -> (judge).

    Args:
        user_message: What Peter just said to Betty.
        workspace_id: Which OpenBrain tenant to retrieve against.
        model: Ollama model name. Default is the locked actor.
        retrieval_limit: Max chunks to surface as context.
        min_similarity: Drop retrieval hits below this cosine similarity.
        ollama: Optionally pass a shared OllamaClient. If None, a fresh
            one is created and closed per call.
        judge: Optional. When supplied, tools are surfaced to Ollama and
            the inner loop activates. When None, Stage 3 behavior:
            no tools surfaced, single Ollama call, outcome="text".
        max_iterations: Outer safety bound on the inner loop. Defaults
            to 4 (rejection_limit 3 + 1 defensive) when a Judge is
            supplied. Ignored when judge is None.

    Returns:
        ActorTurn. Check .outcome to know what happened. .response is
        the user-facing string in every outcome.
    """
    # 1. Retrieve relevant chunks (unchanged from Stage 3).
    hits = retrieve(
        user_message,
        workspace_id=workspace_id,
        limit=retrieval_limit,
        min_similarity=min_similarity,
    )

    # 2. Build the conversation seed.
    system_prompt = load_markdown_os()
    user_content = build_user_message(user_message, hits)
    messages: list[ChatMessage] = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_content),
    ]

    # 3. Client lifecycle.
    owns_client = ollama is None
    if ollama is None:
        ollama = OllamaClient()

    try:
        if judge is None:
            # Stage 3 path: single call, no tools, return text.
            chat_response = ollama.chat(messages=messages, model=model)
            return ActorTurn(
                user_message=user_message,
                response=chat_response.content,
                hits=hits,
                chat_response=chat_response,
                outcome="text",
                iterations=1,
            )

        # Phase 4.3 path: inner loop with tool-calling and Judge.
        judge.reset_turn()
        schemas = get_ollama_tools_schema()
        verdicts: list[JudgeVerdict] = []
        last_chat_response: ChatResponse | None = None

        if max_iterations is None:
            max_iterations = 4  # rejection_limit 3 + 1 defense

        for iteration in range(1, max_iterations + 1):
            chat_response = ollama.chat(
                messages=messages,
                model=model,
                tools=schemas,
            )
            last_chat_response = chat_response

            # Terminal case A: Qwen emitted text, no tool calls.
            if not chat_response.tool_calls:
                return ActorTurn(
                    user_message=user_message,
                    response=chat_response.content,
                    hits=hits,
                    chat_response=chat_response,
                    outcome="text",
                    judge_verdicts=verdicts,
                    iterations=iteration,
                )

            # Tool-call branch. Phase 4.3 handles the first tool_call only;
            # multi-tool-call-per-response is a Stage 5+ concern.
            wire_call = chat_response.tool_calls[0]

            # Dispatch the tool. This validates and writes the proposal.
            try:
                tool_callable = get_tool(wire_call.name)
            except KeyError as e:
                # Unknown tool name from Qwen. Feed back and let it retry.
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[wire_call],
                    )
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=f"Unknown tool {wire_call.name!r}: {e}",
                    )
                )
                continue

            try:
                tool_result: ToolResult = tool_callable(wire_call.arguments)
            except (ValueError, TypeError) as e:
                # Tool's own validation rejected the arguments. Feed back.
                messages.append(
                    ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[wire_call],
                    )
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=f"Tool validation error: {e}",
                    )
                )
                continue

            executed_at = datetime.now(timezone.utc)

            # Build the ToolCall and wrap it in the Envelope. Per Phase 4.4
            # Q1 Decision B (locked 2026-05-24), the adapter populates
            # risk_class from the registry — the actor reads
            # TOOLS[name].risk_class as mechanical metadata, never reasoning
            # about it. authorization_refs is forward-compat empty for now;
            # semantic enforcement is deferred per the scoping decisions log.
            tool_call = ToolCall(
                tool_name=wire_call.name,
                arguments=wire_call.arguments,
                call_id=tool_result.call_id,
            )
            envelope = Envelope(
                tool_call=tool_call,
                risk_class=TOOLS[wire_call.name].risk_class,
            )

            # Phase 4.5 Decision C: read_only tools skip the Judge.
            # The tool already executed above; its payload IS the answer.
            # Write a SKIP_READ_ONLY audit row and return tool_read_only.
            if envelope.risk_class == "read_only":
                judge_decisions.write_skip(
                    envelope=envelope,
                    executed_at=executed_at,
                    execution_result=tool_result.payload,
                )
                return ActorTurn(
                    user_message=user_message,
                    response=_synthesize_read_only_response(
                        wire_call.name, tool_result
                    ),
                    hits=hits,
                    chat_response=chat_response,
                    outcome="tool_read_only",
                    judge_verdicts=verdicts,
                    iterations=iteration,
                )

            # Non-read_only: existing Judge path. The Judge sees the
            # full envelope, not just the ToolCall, so risk_class is
            # part of the prompt context Opus uses to weigh consequence.
            verdict = judge.before_tool_call(
                envelope=envelope,
                user_request=user_message,
            )
            verdicts.append(verdict)

            # Terminal case B: Judge approved.
            if verdict.decision == "approve":
                proposal_path = tool_result.payload["proposal_path"]
                judge_decisions.write_verdict(
                    envelope=envelope,
                    verdict=verdict,
                    executed_at=executed_at,
                    execution_result=tool_result.payload,
                )
                return ActorTurn(
                    user_message=user_message,
                    response=_synthesize_approval_response(
                        wire_call.name, proposal_path
                    ),
                    hits=hits,
                    chat_response=chat_response,
                    outcome="tool_approved",
                    proposal_path=proposal_path,
                    judge_verdicts=verdicts,
                    iterations=iteration,
                )

            # Reject. Structural branch on cost_usd == 0.0:
            # no-API rejections are terminal (breaker / cap / corrupt);
            # substantive rejections feed back to Qwen and loop.
            # Both paths land an audit row — the trail captures every
            # envelope evaluated, not just the approved ones.
            judge_decisions.write_verdict(
                envelope=envelope,
                verdict=verdict,
                executed_at=None,  # rejected; tool didn't really commit
                execution_result=None,
            )

            if verdict.cost_usd == 0.0:
                # Terminal short-circuit.
                outcome = _classify_short_circuit(verdict.reasoning)
                response_text = (
                    f"I tried to {wire_call.name} but had to halt: "
                    f"{verdict.reasoning}"
                )
                return ActorTurn(
                    user_message=user_message,
                    response=response_text,
                    hits=hits,
                    chat_response=chat_response,
                    outcome=outcome,
                    judge_verdicts=verdicts,
                    iterations=iteration,
                )

            # Substantive rejection. Append the prior tool_call as an
            # assistant turn and the rejection as a tool turn, then loop.
            messages.append(
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[wire_call],
                )
            )
            messages.append(
                ChatMessage(
                    role="tool",
                    content=f"Judge rejected: {verdict.reasoning}",
                )
            )

        # Outer-bound: we exited the for loop without a terminal case.
        # In practice the Judge's breaker should trip first.
        return ActorTurn(
            user_message=user_message,
            response=(
                "I tried multiple approaches but couldn't get a tool call "
                "approved within the iteration limit. Halt and escalate."
            ),
            hits=hits,
            chat_response=last_chat_response,
            outcome="max_iterations",
            judge_verdicts=verdicts,
            iterations=max_iterations,
        )

    finally:
        if owns_client:
            ollama.close()


# ---------- Self-test ----------

class _MockJudge:
    """Duck-typed Judge for testing the actor's rejection loop.

    Implements only the surface actor_turn consumes:
      - reset_turn()
      - before_tool_call(envelope, user_request) -> JudgeVerdict

    Phase 4.5 signature: takes Envelope, not ToolCall directly. The mock
    extracts call_id from envelope.tool_call to populate the verdict.

    Mirrors the real Judge's two rejection modes:
      - Substantive reject (cost_usd > 0): actor should loop, feed
        rejection back to Qwen.
      - Short-circuit reject (cost_usd == 0): actor should halt.

    Rejects every call. On the rejection_limit-th call and beyond,
    emits short-circuit-style rejections so the actor halts.
    """

    def __init__(self, rejection_limit: int = 3):
        self._rejection_limit = rejection_limit
        self._rejections_this_turn = 0
        self.verdicts_issued = 0

    def reset_turn(self) -> None:
        self._rejections_this_turn = 0

    @property
    def rejections_this_turn(self) -> int:
        return self._rejections_this_turn

    def before_tool_call(
        self, envelope: Envelope, user_request: str
    ) -> JudgeVerdict:
        self.verdicts_issued += 1
        self._rejections_this_turn += 1

        if self._rejections_this_turn >= self._rejection_limit:
            cost = 0.0
            reasoning = (
                f"Circuit breaker tripped: {self._rejections_this_turn} "
                f"rejections this turn (limit {self._rejection_limit}). "
                f"Halt and escalate to operator."
            )
        else:
            cost = 0.001
            reasoning = (
                "Mock substantive rejection for self-test. The tool call "
                "looks fine but the mock rejects it anyway. Please try a "
                "slightly different formulation."
            )

        return JudgeVerdict(
            call_id=envelope.tool_call.call_id,
            decision="reject",
            reasoning=reasoning,
            input_tokens=0,
            output_tokens=0,
            cost_usd=cost,
        )


def _self_test() -> None:
    """End-to-end self-test against real Qwen + real spend ledger.

    Scenario A: text path. User asks a knowledge question; Qwen emits
        text; actor returns outcome='text'. No Judge involved.

    Scenario B: approve path. User asks Betty to draft an email; Qwen
        emits a tool call; real Judge approves; actor returns
        outcome='tool_approved' with proposal_path on disk.

    Scenario C: reject-loop + breaker. MockJudge rejects every call,
        emits short-circuit rejection on the 3rd; actor should halt
        with outcome='breaker_tripped' after exactly 3 iterations.

    Scenario D (Phase 4.5): read_only Judge-skip. A synthetic read_only
        tool is temporarily registered. The actor must execute the tool
        directly and return outcome='tool_read_only' without calling
        the Judge. _MockJudge is supplied that raises if invoked — proves
        the Judge-skip path is structural, not best-effort.

    Anthropic API cost: scenario B makes one Judge call (~$0.02-0.03).
    Scenarios A, C, D make no Anthropic calls. Total cost roughly $0.02-0.04.
    """
    import shutil

    from dotenv import load_dotenv

    from betty_claw.anthropic_client import AnthropicClient

    load_dotenv(Path.home() / "code" / "betty" / ".env")

    print("Actor self-test (Phase 4.3)\n")

    # ---- Scenario A: Stage 3 text path ----
    print("Scenario A: text path (Stage 3 baseline, no Judge)")
    turn = actor_turn(
        user_message="In one sentence, what is photosynthesis?",
    )
    print(f"  outcome={turn.outcome}")
    print(f"  iterations={turn.iterations}")
    print(f"  response: {turn.response[:150]!r}")
    assert turn.outcome == "text", f"expected text, got {turn.outcome}"
    assert turn.iterations == 1
    assert turn.proposal_path is None
    assert turn.judge_verdicts == []
    assert turn.response.strip(), "empty response"
    print("  [ok] Stage 3 text path works unchanged\n")

    # ---- Scenario B: approve path with real Judge ----
    print("Scenario B: approve path (real Judge + real Qwen)")
    judge = Judge(anthropic_client=AnthropicClient())
    turn = actor_turn(
        user_message=(
            "Please draft an email to alice@example.com with subject "
            "'Tomorrow's meeting' confirming we're meeting at 2pm Eastern."
        ),
        judge=judge,
    )
    print(f"  outcome={turn.outcome}")
    print(f"  iterations={turn.iterations}")
    print(f"  verdicts={len(turn.judge_verdicts)}")
    print(f"  proposal_path={turn.proposal_path}")
    print(f"  response: {turn.response[:200]!r}")
    if turn.judge_verdicts:
        first = turn.judge_verdicts[0]
        print(f"  judge cost_usd=${first.cost_usd:.4f}")
        print(f"  judge reasoning: {first.reasoning[:150]}")

    assert turn.outcome == "tool_approved", (
        f"expected tool_approved, got {turn.outcome}. "
        f"response={turn.response[:300]!r}"
    )
    assert turn.proposal_path is not None
    assert Path(turn.proposal_path).exists(), (
        f"proposal file missing: {turn.proposal_path}"
    )
    assert len(turn.judge_verdicts) == 1
    assert turn.judge_verdicts[0].decision == "approve"
    print("  [ok] approve path: Qwen invoked tool, Judge approved, "
          "proposal written\n")

    # Clean up the proposal we just wrote so it doesn't accumulate.
    try:
        Path(turn.proposal_path).unlink()
    except FileNotFoundError:
        pass

    # ---- Scenario C: reject loop + breaker with MockJudge ----
    print("Scenario C: reject loop and breaker trip (MockJudge)")
    mock = _MockJudge(rejection_limit=3)
    turn = actor_turn(
        user_message=(
            "Please draft an email to alice@example.com with subject "
            "'Tomorrow's meeting' confirming we're meeting at 2pm Eastern. "
            "If your tool call is rejected, please try a corrected version."
        ),
        judge=mock,
    )
    print(f"  outcome={turn.outcome}")
    print(f"  iterations={turn.iterations}")
    print(f"  verdicts_issued (mock)={mock.verdicts_issued}")
    print(f"  judge_verdicts={len(turn.judge_verdicts)}")
    print(f"  response: {turn.response[:200]!r}")

    # The breaker must trip and the actor must halt.
    assert turn.outcome == "breaker_tripped", (
        f"expected breaker_tripped, got {turn.outcome}. "
        f"This is the load-bearing financial safety mechanism — failure "
        f"here means the actor's while loop can run unbounded. "
        f"iterations={turn.iterations}, verdicts={mock.verdicts_issued}, "
        f"response={turn.response[:300]!r}"
    )
    assert mock.verdicts_issued == 3, (
        f"expected 3 verdicts, got {mock.verdicts_issued}"
    )
    assert turn.iterations == 3, (
        f"expected 3 iterations, got {turn.iterations}"
    )
    assert len(turn.judge_verdicts) == 3
    assert all(v.decision == "reject" for v in turn.judge_verdicts)
    # First two should be substantive (cost > 0), third should be short-circuit.
    assert turn.judge_verdicts[0].cost_usd > 0
    assert turn.judge_verdicts[1].cost_usd > 0
    assert turn.judge_verdicts[2].cost_usd == 0.0
    print("  [ok] reject loop halted at breaker, no unbounded iteration\n")

    # ---- Scenario D (Phase 4.5): read_only Judge-skip ----
    print("Scenario D: read_only tool skips Judge entirely")

    # Temporarily register a synthetic read_only tool. The TOOLS registry
    # is module-level; we mutate it with try/finally cleanup so we don't
    # leak the synthetic tool to other consumers.
    SYNTHETIC_TOOL_NAME = "betty_self_check"

    def _synthetic_read_only_tool(args: dict) -> ToolResult:
        """Synthetic read_only tool used only by this self-test.

        Validates that args is an empty dict, then returns a canned
        ToolResult that the actor surfaces back to the user. status is
        'executed' rather than 'proposed' — read_only tools do the real
        thing rather than writing a proposal stub.
        """
        if args:
            raise ValueError(
                f"{SYNTHETIC_TOOL_NAME} takes no arguments, got {args!r}"
            )
        return ToolResult(
            call_id=str(uuid.uuid4()),
            tool_name=SYNTHETIC_TOOL_NAME,
            status="executed",
            payload={
                "summary": (
                    "Betty self-check OK: actor loop intact, registry "
                    "intact, Phase 4.5 contract responsive."
                ),
            },
        )

    SYNTHETIC_SCHEMA = {
        "type": "function",
        "function": {
            "name": SYNTHETIC_TOOL_NAME,
            "description": (
                "Run a quick self-health check on Betty's actor loop. "
                "Returns a one-sentence summary of system health. Takes "
                "no arguments. Use when the user asks for a system "
                "health check or self-check."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        },
    }

    class _ExplosiveJudge:
        """MockJudge that raises if before_tool_call is ever called.

        Proves the read_only Judge-skip is structural: if the actor
        accidentally routes a read_only envelope through the Judge,
        this raises and the test fails loudly.
        """

        def reset_turn(self) -> None:
            pass

        @property
        def rejections_this_turn(self) -> int:
            return 0

        def before_tool_call(self, envelope: Envelope, user_request: str) -> JudgeVerdict:
            raise AssertionError(
                f"Judge.before_tool_call invoked on read_only envelope! "
                f"risk_class={envelope.risk_class!r} tool={envelope.tool_call.tool_name!r}. "
                f"Phase 4.5 Decision C requires Judge-skip for read_only."
            )

    # Register the synthetic tool. Use the same ToolEntry shape the real
    # registry uses so the actor sees no difference.
    from betty_claw.tools import ToolEntry
    TOOLS[SYNTHETIC_TOOL_NAME] = ToolEntry(
        callable=_synthetic_read_only_tool,
        schema=SYNTHETIC_SCHEMA,
        risk_class="read_only",
    )

    try:
        explosive = _ExplosiveJudge()
        turn = actor_turn(
            user_message=(
                "Please run the betty_self_check tool to confirm Betty's "
                "actor loop is healthy. Just call the tool with no arguments."
            ),
            judge=explosive,
        )
        print(f"  outcome={turn.outcome}")
        print(f"  iterations={turn.iterations}")
        print(f"  judge_verdicts={len(turn.judge_verdicts)}")
        print(f"  response: {turn.response[:200]!r}")

        # The actor must NOT have routed through the Judge.
        assert turn.outcome == "tool_read_only", (
            f"expected tool_read_only outcome, got {turn.outcome!r}. "
            f"response={turn.response[:300]!r}"
        )
        # No Judge verdicts should be recorded for read_only path.
        assert turn.judge_verdicts == [], (
            f"read_only path should produce zero verdicts, "
            f"got {len(turn.judge_verdicts)}: {turn.judge_verdicts}"
        )
        # The user-facing response should surface the tool's summary.
        assert "Betty self-check OK" in turn.response, (
            f"response should include tool summary; "
            f"got {turn.response[:300]!r}"
        )
        print("  [ok] read_only tool executed, Judge skipped, summary surfaced\n")

    finally:
        # Always remove the synthetic tool from the registry so we don't
        # pollute downstream consumers in the same Python session.
        TOOLS.pop(SYNTHETIC_TOOL_NAME, None)

    print("actor.py self-test PASSED")


if __name__ == "__main__":
    _self_test()
