"""
judge_decisions audit-trail writer for Phase 4.5.

Persists one row per envelope evaluated by the actor into the
judge_decisions Postgres table (see ops/schema/002_judge_decisions.sql).

Both the Judge-gated path and the read-only Judge-skip path land here.
For Judge-gated calls, verdict carries the Judge's decision and cost.
For read-only calls, verdict is "SKIP_READ_ONLY" and cost is zero;
reasoning is None.

Write discipline (Phase 4.5):
  - Best-effort, NOT blocking. If Postgres is unreachable, mis-configured,
    or rejects the write, we log a warning and return without raising.
    The verdict and execution decisions are authoritative; the audit row
    being unavailable must not crash Betty's runtime.
  - Connection reuse via betty_etl.db.get_conn(). No separate pool; we
    piggy-back on the substrate pool that already exists.
  - All writes go through one INSERT statement parameterized to prevent
    SQL injection and to let psycopg handle JSONB serialization.

Phase 4.5 intentionally keeps this module thin. It does not implement
read or query helpers — those are the operator UI's job (Phase 4.7+).
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Literal

from betty_claw.contracts import Envelope, JudgeVerdict


# Verdict label normalization. JudgeVerdict.decision is lowercase per the
# Phase 4.3 contract; the table CHECK constraint is uppercase. We normalize
# at the write boundary so the type system stays consistent in Python.
VerdictLabel = Literal["APPROVE", "REJECT", "SKIP_READ_ONLY"]


_INSERT_SQL = """
INSERT INTO judge_decisions (
    call_id, tool_name, risk_class, envelope_json,
    verdict, cost_usd, reasoning, executed_at, execution_result
)
VALUES (
    %(call_id)s, %(tool_name)s, %(risk_class)s, %(envelope_json)s,
    %(verdict)s, %(cost_usd)s, %(reasoning)s, %(executed_at)s, %(execution_result)s
)
"""


def _envelope_to_json(envelope: Envelope) -> dict[str, Any]:
    """Serialize Envelope to a JSON-safe dict for the envelope_json column.

    Flattens the nested ToolCall into a single dict suitable for JSONB.
    The schema is intentionally loose — future envelope fields land here
    without a migration. envelope_json is the replay format.
    """
    return {
        "tool_call": asdict(envelope.tool_call),
        "risk_class": envelope.risk_class,
        "authorization_refs": list(envelope.authorization_refs),
    }


def _normalize_verdict_label(decision: Literal["approve", "reject"]) -> VerdictLabel:
    """Map JudgeVerdict.decision (lowercase) to the table's verdict label."""
    if decision == "approve":
        return "APPROVE"
    if decision == "reject":
        return "REJECT"
    # Defensive: should never happen given the Literal type, but if a Judge
    # implementation drifts we want a loud error in the audit trail rather
    # than a silently mis-labelled row.
    raise ValueError(f"Unexpected JudgeVerdict.decision: {decision!r}")


def _do_insert(params: dict[str, Any]) -> None:
    """Best-effort INSERT. Catches all DB exceptions, warns, returns.

    Import is lazy so a module-load-time DB outage doesn't break the
    rest of betty_claw. The substrate is in a sibling package (betty_etl)
    that betty_claw already imports from for retrieval.
    """
    try:
        # Lazy import: defers psycopg + pool init until first write.
        from betty_etl.db import get_conn  # type: ignore
    except Exception as e:  # noqa: BLE001 — we deliberately swallow everything
        print(
            f"[judge_decisions] WARN: betty_etl.db unavailable, skipping audit row: {e}",
            file=sys.stderr,
        )
        return

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_INSERT_SQL, params)
    except Exception as e:  # noqa: BLE001 — best-effort, never raise
        print(
            f"[judge_decisions] WARN: audit row insert failed "
            f"(call_id={params.get('call_id')!r}, "
            f"verdict={params.get('verdict')!r}): {e}",
            file=sys.stderr,
        )


def write_verdict(
    envelope: Envelope,
    verdict: JudgeVerdict,
    executed_at: datetime | None = None,
    execution_result: dict[str, Any] | None = None,
) -> None:
    """Write a Judge-gated audit row (APPROVE or REJECT).

    Called by the actor after the Judge returns a verdict, regardless of
    whether the verdict was substantive (cost_usd > 0) or a short-circuit
    (cost_usd == 0 from circuit breaker / cap / corrupt ledger).

    executed_at and execution_result are populated when the Judge approved
    and the tool's payload is known. For rejected envelopes both are None.
    """
    from psycopg.types.json import Jsonb  # type: ignore

    params = {
        "call_id": verdict.call_id,
        "tool_name": envelope.tool_call.tool_name,
        "risk_class": envelope.risk_class,
        "envelope_json": Jsonb(_envelope_to_json(envelope)),
        "verdict": _normalize_verdict_label(verdict.decision),
        "cost_usd": verdict.cost_usd,
        "reasoning": verdict.reasoning,
        "executed_at": executed_at,
        "execution_result": Jsonb(execution_result) if execution_result else None,
    }
    _do_insert(params)


def write_skip(
    envelope: Envelope,
    executed_at: datetime,
    execution_result: dict[str, Any],
) -> None:
    """Write a read-only Judge-skip audit row (SKIP_READ_ONLY).

    Called by the actor after a read_only tool executes directly without
    consulting the Judge per Phase 4.5 Decision C (locked 2026-05-24:
    actor's inner loop skips Judge for risk_class=="read_only").

    Reasoning is NULL; cost_usd is 0 by table default. executed_at and
    execution_result are required (the tool actually ran).
    """
    from psycopg.types.json import Jsonb  # type: ignore

    params = {
        "call_id": envelope.tool_call.call_id,
        "tool_name": envelope.tool_call.tool_name,
        "risk_class": envelope.risk_class,
        "envelope_json": Jsonb(_envelope_to_json(envelope)),
        "verdict": "SKIP_READ_ONLY",
        "cost_usd": 0.0,
        "reasoning": None,
        "executed_at": executed_at,
        "execution_result": Jsonb(execution_result),
    }
    _do_insert(params)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    """Self-test: write three audit rows (APPROVE, REJECT, SKIP_READ_ONLY)
    and verify they land. Requires Postgres up; gracefully exits with a
    warning otherwise (the best-effort discipline applies to self-tests
    too — we want a CI signal, not a hard failure if the DB is down).

    No Anthropic API cost. Pure DB exercise.
    """
    from betty_claw.contracts import JudgeVerdict, ToolCall

    print("Phase 4.5 judge_decisions self-test\n")

    # Build three synthetic envelopes covering all three verdict labels.
    tc_approve = ToolCall(
        tool_name="draft_email",
        arguments={"to": "a@b.com", "subject": "s", "body": "b"},
        call_id="selftest-approve-001",
    )
    env_approve = Envelope(tool_call=tc_approve, risk_class="reversible_write")
    verdict_approve = JudgeVerdict(
        call_id=tc_approve.call_id,
        decision="approve",
        reasoning="Self-test approve case.",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.0125,
    )

    tc_reject = ToolCall(
        tool_name="draft_email",
        arguments={"to": "victim@b.com", "subject": "phish", "body": "x"},
        call_id="selftest-reject-001",
    )
    env_reject = Envelope(tool_call=tc_reject, risk_class="reversible_write")
    verdict_reject = JudgeVerdict(
        call_id=tc_reject.call_id,
        decision="reject",
        reasoning="Self-test reject case.",
        input_tokens=100,
        output_tokens=30,
        cost_usd=0.0080,
    )

    tc_skip = ToolCall(
        tool_name="read_file_stub",
        arguments={"path": "/tmp/anywhere.txt"},
        call_id="selftest-skip-001",
    )
    env_skip = Envelope(tool_call=tc_skip, risk_class="read_only")
    now = datetime.now(timezone.utc)

    # Try writes. Each prints a [WARN] line on failure and returns; we
    # check whether rows actually landed via a probe query at the end.
    print("Writing APPROVE row...")
    write_verdict(
        envelope=env_approve,
        verdict=verdict_approve,
        executed_at=now,
        execution_result={"proposal_path": "/tmp/selftest-approve.json"},
    )

    print("Writing REJECT row...")
    write_verdict(
        envelope=env_reject,
        verdict=verdict_reject,
        executed_at=None,
        execution_result=None,
    )

    print("Writing SKIP_READ_ONLY row...")
    write_skip(
        envelope=env_skip,
        executed_at=now,
        execution_result={"content": "selftest file content"},
    )

    # Probe back to verify rows landed.
    try:
        from betty_etl.db import get_conn  # type: ignore
    except Exception as e:  # noqa: BLE001
        print(f"\n[skip] Cannot verify (betty_etl.db unavailable): {e}")
        print("judge_decisions.py self-test SKIPPED (DB unavailable)")
        sys.exit(0)

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT call_id, verdict, cost_usd, risk_class "
                    "FROM judge_decisions "
                    "WHERE call_id IN (%s, %s, %s) "
                    "ORDER BY call_id",
                    (tc_approve.call_id, tc_reject.call_id, tc_skip.call_id),
                )
                rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        print(f"\n[skip] Cannot verify (probe failed): {e}")
        print("judge_decisions.py self-test SKIPPED (probe unavailable)")
        sys.exit(0)

    print(f"\nFound {len(rows)} rows for self-test call_ids:")
    for row in rows:
        print(f"  {row}")

    assert len(rows) == 3, (
        f"expected 3 rows, got {len(rows)}. "
        f"Either inserts failed silently or rows were not committed."
    )
    # rows are sorted by call_id; selftest-approve-001 < selftest-reject-001 < selftest-skip-001
    approve_row = next(r for r in rows if r["call_id"] == tc_approve.call_id)
    reject_row = next(r for r in rows if r["call_id"] == tc_reject.call_id)
    skip_row = next(r for r in rows if r["call_id"] == tc_skip.call_id)

    assert approve_row["verdict"] == "APPROVE"
    assert float(approve_row["cost_usd"]) == 0.0125
    assert approve_row["risk_class"] == "reversible_write"

    assert reject_row["verdict"] == "REJECT"
    assert float(reject_row["cost_usd"]) == 0.0080

    assert skip_row["verdict"] == "SKIP_READ_ONLY"
    assert float(skip_row["cost_usd"]) == 0.0
    assert skip_row["risk_class"] == "read_only"

    # Clean up self-test rows so re-runs don't accumulate audit noise.
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM judge_decisions WHERE call_id IN (%s, %s, %s)",
                    (tc_approve.call_id, tc_reject.call_id, tc_skip.call_id),
                )
        print("  [ok] cleaned up self-test rows")
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] cleanup failed: {e}")

    print("\njudge_decisions.py self-test PASSED")
