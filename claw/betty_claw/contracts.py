"""
claw/betty_claw/contracts.py

Stage 4 data contracts. Frozen dataclasses for immutability across the
Qwen -> Adapter -> Judge -> Tool boundary. Defining these before transport
(httpx clients, file I/O) is deliberate — every component downstream
consumes or produces one of these shapes.

Phase 4.5 (envelope minimum) adds the Envelope dataclass and the
RiskClass type alias. The Envelope wraps a ToolCall with adapter-populated
mechanical metadata (risk_class from the tool registry; authorization_refs
reserved forward-compatibly with semantic enforcement deferred per
Phase 4.4 scoping decisions). See phases/phase-4.5-4.6-execution-kickoff.md.

Per Q1 Decision B (locked 2026-05-24): the actor (Qwen) emits ToolCall;
the adapter (currently inline in actor.py) constructs the Envelope by
reading risk_class from the tool registry. The actor never reasons about
risk_class, never sees it, never emits it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal


# Type alias for the four locked risk classes from Phase 4.4 Q1 Decision A.
# Per-tool constant. Declared on ToolEntry in tools/__init__.py.
# Populated onto Envelope by the adapter at envelope-construction time.
RiskClass = Literal[
    "read_only",
    "reversible_write",
    "external_side_effect",
    "high_risk",
]


@dataclass(frozen=True)
class ToolCall:
    """A proposal from Qwen to execute a tool. Immutable once created.

    `arguments` is a dict because tool schemas vary — validation against
    each tool's schema happens in actor.py before the ToolCall is built.
    """
    tool_name: str
    arguments: dict[str, Any]
    call_id: str  # UUID4 string; used to correlate with proposal JSON on disk


@dataclass(frozen=True)
class Envelope:
    """OB1 Action Proposal Envelope — Phase 4.5 minimum shape.

    Bundles a ToolCall (semantic, actor-produced) with adapter-populated
    mechanical metadata. The unit the Judge evaluates.

    Phase 4.5 minimum fields:
      - tool_call: what the actor wants to do
      - risk_class: mechanical metadata from TOOLS[tool_name].risk_class
      - authorization_refs: forward-compat field; no validation in Phase 4.5,
        empty list by default. Semantic enforcement deferred per Phase 4.4
        decisions log (the authorization sub-decision is the contested
        actor-vs-adapter split that the ship-the-win scope deferred).

    Future phases extend this dataclass with the rest of the OB1 envelope
    fields (evidence_refs, expected_consequence, rollback, sensitivity, etc.)
    as they become load-bearing for executors. Phase 4.5 ships the minimum
    the travelpec.com autonomous deploy needs.
    """
    tool_call: ToolCall
    risk_class: RiskClass
    authorization_refs: list[str] = field(default_factory=list)


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
