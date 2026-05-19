"""
claw/betty_claw/contracts.py

Stage 4 data contracts. Frozen dataclasses for immutability across the
Qwen -> Judge -> Tool boundary. Defining these before transport (httpx
clients, file I/O) is deliberate — every component downstream consumes
or produces one of these four shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal


@dataclass(frozen=True)
class ToolCall:
    """A proposal from Qwen to execute a tool. Immutable once created.

    The Judge sees this exact object; no intermediate code may mutate it.
    `arguments` is a dict because tool schemas vary — validation against
    each tool's schema happens in actor.py before the ToolCall is built.
    """
    tool_name: str
    arguments: dict[str, Any]
    call_id: str  # UUID4 string; used to correlate with proposal JSON on disk


@dataclass(frozen=True)
class ToolResult:
    """The outcome of a tool call after Judge evaluation.

    status semantics:
      - "proposed":  stub tool wrote proposal JSON but took no real action
      - "executed":  Judge approved and the tool ran successfully
      - "rejected":  Judge rejected; tool did NOT run
      - "failed":    tool ran but raised; partial state may exist
    """
    call_id: str
    tool_name: str
    status: Literal["proposed", "executed", "rejected", "failed"]
    payload: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class JudgeVerdict:
    """The Judge's decision on a single ToolCall.

    approve/reject only in Stage 4. "revise" is deferred to Stage 6 where
    the revise loop actually exists. reasoning is preserved verbatim from
    the Judge response — useful for the spend ledger audit trail and for
    debugging false rejections.
    """
    call_id: str
    decision: Literal["approve", "reject"]
    reasoning: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


@dataclass(frozen=True)
class SpendLedger:
    """On-disk shape of the daily Anthropic spend ledger.

    Persisted as JSON at ~/code/betty/var/spend_ledger.json. Read/write
    logic lives in spend_ledger.py — this is just the contract.

    Per-turn rejection counting is transient state and lives in-memory in
    the Judge instance, NOT here. The ledger is for dollars only.

    `entries` is a list of (timestamp_iso, call_id, cost_usd) tuples so
    we can audit which Judge calls contributed to the day's spend.
    """
    ledger_date: date
    cumulative_cost_usd: float
    entries: list[tuple[str, str, float]] = field(default_factory=list)
