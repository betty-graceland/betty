"""Daily Anthropic spend ledger.

Persists at ~/code/betty/var/spend_ledger.json. Tracks cumulative USD
spent on Judge API calls within the current local-Toronto day.

The day boundary is local Toronto midnight (ZoneInfo("America/Toronto")).
DST transitions produce 23-hour and 25-hour days twice a year; this is
accepted behavior, logged in BUILD_LOG, not an incident.

Read/write pattern: read-modify-write per Judge call, no caching across
calls. Atomic writes via atomic_io.atomic_write_json. Single-process
operation; no file locking.

Corruption response: fail-loud. load() returns LedgerResult with status
"ok", "fresh", or "corrupt". Callers (the Judge) MUST check .status
before using .ledger. The Judge halts on "corrupt".

Phase 4.1 contract (SpendLedger in types.py) stores both
cumulative_cost_usd and entries. This module enforces the invariant
that cumulative_cost_usd == sum(e[2] for e in entries) at load time
via internal validation (Option b2 from Phase 4.3 design discussion).
Drift between the two fields routes to "corrupt".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from betty_claw.atomic_io import atomic_write_json
from betty_claw.contracts import SpendLedger


# Module-level constants
DAILY_CAP_USD: float = 5.00
TORONTO = ZoneInfo("America/Toronto")
SCHEMA_VERSION: int = 1
LEDGER_PATH: Path = Path.home() / "code" / "betty" / "var" / "spend_ledger.json"

# Float consistency tolerance for the b2 validator. Floating-point sum
# of repeated additions accumulates error; bit-exact equality would
# reject legitimate ledgers after many entries. 1e-9 USD (one nanodollar)
# is generous — individual Judge calls are ~$0.05-$0.20.
CONSISTENCY_TOLERANCE_USD: float = 1e-9


@dataclass(frozen=True)
class LedgerResult:
    """Result of load(). Callers MUST check .status before using .ledger.

    status="ok": ledger loaded successfully, reflects today's spend
    status="fresh": no file existed, ledger is a zero-entry ledger for today
    status="corrupt": file existed but was malformed; ledger is a
        zero-entry sentinel for today and corruption_reason explains why.
        Callers MUST halt; do not call record() with a corrupt-state ledger.
    """
    status: Literal["ok", "fresh", "corrupt"]
    ledger: SpendLedger
    corruption_reason: str | None = None


class LedgerCorruptError(RuntimeError):
    """Raised when record() is called while the on-disk ledger is corrupt.

    Backstop: load() returns LedgerResult(status="corrupt") and the
    Judge should halt without calling record(). This raise defends
    against callers that forget the check.
    """


def load(path: Path | None = None) -> LedgerResult:
    """Load the ledger from disk.

    Args:
        path: Override the ledger file path. Defaults to LEDGER_PATH.
            Used by tests to redirect to a temp file.

    Returns:
        LedgerResult — see class docstring for status semantics.
    """
    ledger_path = path if path is not None else LEDGER_PATH
    now = datetime.now(TORONTO)
    today = now.date()

    if not ledger_path.exists():
        return LedgerResult(
            status="fresh",
            ledger=SpendLedger(
                ledger_date=today,
                cumulative_cost_usd=0.0,
                entries=[],
            ),
        )

    try:
        raw = json.loads(ledger_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _corrupt_result(f"Ledger file unparseable (corrupt): {e}", today)

    if not isinstance(raw, dict):
        return _corrupt_result(
            f"Ledger root is not an object (got {type(raw).__name__})",
            today,
        )

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        return _corrupt_result(
            f"Ledger schema_version is {schema_version!r}, "
            f"this code expects {SCHEMA_VERSION}",
            today,
        )

    try:
        ledger_date = date.fromisoformat(raw["ledger_date"])
        cumulative_cost_usd = float(raw["cumulative_cost_usd"])
        entries_raw = raw["entries"]
        if not isinstance(entries_raw, list):
            raise ValueError(
                f"entries is not a list (got {type(entries_raw).__name__})"
            )
        entries: list[tuple[str, str, float]] = []
        for i, e in enumerate(entries_raw):
            if not isinstance(e, (list, tuple)) or len(e) != 3:
                raise ValueError(
                    f"entry {i} is not a 3-element sequence: {e!r}"
                )
            entries.append((str(e[0]), str(e[1]), float(e[2])))
    except (KeyError, ValueError, TypeError, IndexError) as e:
        return _corrupt_result(f"Ledger fields malformed or missing: {e}", today)

    loaded = SpendLedger(
        ledger_date=ledger_date,
        cumulative_cost_usd=cumulative_cost_usd,
        entries=entries,
    )

    # b2 validator: enforce internal consistency between
    # cumulative_cost_usd and entries.
    entries_sum = sum(e[2] for e in loaded.entries)
    if abs(loaded.cumulative_cost_usd - entries_sum) > CONSISTENCY_TOLERANCE_USD:
        return _corrupt_result(
            f"Ledger internal inconsistency: "
            f"cumulative_cost_usd={loaded.cumulative_cost_usd}, "
            f"sum(entries)={entries_sum}",
            today,
        )

    # Rollover check: file is for a prior day, return zero ledger for today.
    if loaded.ledger_date != today:
        return LedgerResult(
            status="ok",
            ledger=SpendLedger(
                ledger_date=today,
                cumulative_cost_usd=0.0,
                entries=[],
            ),
        )

    return LedgerResult(status="ok", ledger=loaded)


def _corrupt_result(reason: str, today: date) -> LedgerResult:
    """Build a corrupt-status result with a fresh today ledger inside.

    The inner ledger is well-formed and zero-cost so type-checkers don't
    have to deal with Optional, but callers MUST check .status before
    using .ledger.
    """
    return LedgerResult(
        status="corrupt",
        ledger=SpendLedger(
            ledger_date=today,
            cumulative_cost_usd=0.0,
            entries=[],
        ),
        corruption_reason=reason,
    )


def check(ledger: SpendLedger, proposed_cost_usd: float) -> bool:
    """Return True if recording proposed_cost_usd would keep total <= cap.

    Pure function. No I/O. Inclusive upper bound: $5.00 total is allowed,
    $5.01 is not.
    """
    return (ledger.cumulative_cost_usd + proposed_cost_usd) <= DAILY_CAP_USD


def record(
    call_id: str,
    cost_usd: float,
    path: Path | None = None,
) -> SpendLedger:
    """Record a Judge API call's cost. Read-modify-write.

    Args:
        call_id: Anthropic message id (msg_...) for cross-referencing
            with Anthropic billing logs.
        cost_usd: Cost of the call in USD.
        path: Override the ledger file path. Defaults to LEDGER_PATH.

    Returns:
        The new SpendLedger after the entry is appended.

    Raises:
        LedgerCorruptError: if the on-disk ledger is corrupt. Caller
            should have checked load() first and halted on corrupt
            status; this raise is a backstop.
    """
    ledger_path = path if path is not None else LEDGER_PATH

    result = load(ledger_path)
    if result.status == "corrupt":
        raise LedgerCorruptError(
            f"Cannot record on corrupt ledger: {result.corruption_reason}"
        )

    now = datetime.now(TORONTO)
    today = now.date()
    timestamp = now.isoformat()

    # Single-instant discipline: use the same `now` for timestamp and today.
    # Rollover at record time: if loaded date differs from today, start fresh.
    # (load() already handles this by returning a fresh ledger in the "ok"
    # rollover branch, but defensive re-check is cheap and explicit.)
    if result.ledger.ledger_date != today:
        prior_entries: list[tuple[str, str, float]] = []
        prior_cost = 0.0
    else:
        prior_entries = list(result.ledger.entries)
        prior_cost = result.ledger.cumulative_cost_usd

    new_entry: tuple[str, str, float] = (timestamp, call_id, cost_usd)
    new_entries = prior_entries + [new_entry]
    new_cost = prior_cost + cost_usd

    new_ledger = SpendLedger(
        ledger_date=today,
        cumulative_cost_usd=new_cost,
        entries=new_entries,
    )

    _write(new_ledger, ledger_path)
    return new_ledger


def _write(ledger: SpendLedger, path: Path) -> None:
    """Serialize and atomically write the ledger to disk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ledger_date": ledger.ledger_date.isoformat(),
        "cumulative_cost_usd": ledger.cumulative_cost_usd,
        "entries": [list(e) for e in ledger.entries],
    }
    atomic_write_json(path, payload)


if __name__ == "__main__":
    import shutil
    import sys
    from datetime import timedelta

    test_dir = Path("/tmp/betty_spend_ledger_selftest")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()

    test_path = test_dir / "spend_ledger.json"

    # 1. Fresh load: no file exists.
    result = load(test_path)
    assert result.status == "fresh", f"expected fresh, got {result.status}"
    assert result.ledger.cumulative_cost_usd == 0.0
    assert result.ledger.entries == []
    assert result.ledger.ledger_date == datetime.now(TORONTO).date()
    print("  [ok] fresh load with no file returns status=fresh, zero ledger")

    # 2. Record a call, verify on-disk shape, reload, verify roundtrip.
    new_ledger = record("msg_test001", 0.0182, path=test_path)
    assert new_ledger.cumulative_cost_usd == 0.0182
    assert len(new_ledger.entries) == 1
    assert new_ledger.entries[0][1] == "msg_test001"
    assert new_ledger.entries[0][2] == 0.0182

    on_disk = json.loads(test_path.read_text())
    assert on_disk["schema_version"] == 1
    assert on_disk["cumulative_cost_usd"] == 0.0182
    assert len(on_disk["entries"]) == 1
    print("  [ok] record() writes correct JSON shape")

    result = load(test_path)
    assert result.status == "ok"
    assert result.ledger.cumulative_cost_usd == 0.0182
    assert len(result.ledger.entries) == 1
    assert isinstance(result.ledger.entries[0], tuple), "entries must be tuples after load"
    print("  [ok] reload roundtrips correctly, entries are tuples")

    # 3. check(): under cap, at cap exactly, over cap.
    assert check(result.ledger, 1.00) is True, "under-cap should allow"
    assert check(result.ledger, DAILY_CAP_USD - result.ledger.cumulative_cost_usd) is True, \
        "exactly at cap should allow (inclusive)"
    assert check(result.ledger, DAILY_CAP_USD - result.ledger.cumulative_cost_usd + 0.01) is False, \
        "over cap should refuse"
    print("  [ok] check() handles under-cap, at-cap, over-cap correctly")

    # 4. Multiple records accumulate.
    record("msg_test002", 0.0291, path=test_path)
    result = load(test_path)
    expected = 0.0182 + 0.0291
    assert abs(result.ledger.cumulative_cost_usd - expected) < 1e-9
    assert len(result.ledger.entries) == 2
    print(f"  [ok] multiple records accumulate: cumulative=${result.ledger.cumulative_cost_usd:.4f}")

    # 5. Corruption: unparseable JSON.
    test_path.write_text("{ this is not valid json")
    result = load(test_path)
    assert result.status == "corrupt"
    assert "unparseable" in (result.corruption_reason or "").lower()
    print(f"  [ok] unparseable JSON detected: {result.corruption_reason}")

    # 6. Corruption: schema_version mismatch.
    test_path.write_text(json.dumps({
        "schema_version": 99,
        "ledger_date": "2026-05-18",
        "cumulative_cost_usd": 0.0,
        "entries": [],
    }))
    result = load(test_path)
    assert result.status == "corrupt"
    assert "schema_version" in (result.corruption_reason or "")
    print(f"  [ok] schema_version mismatch detected: {result.corruption_reason}")

    # 7. Corruption: cumulative_cost_usd disagrees with sum(entries).
    test_path.write_text(json.dumps({
        "schema_version": 1,
        "ledger_date": datetime.now(TORONTO).date().isoformat(),
        "cumulative_cost_usd": 1.00,
        "entries": [["2026-05-18T10:00:00-04:00", "msg_x", 0.05]],
    }))
    result = load(test_path)
    assert result.status == "corrupt"
    assert "inconsistency" in (result.corruption_reason or "").lower()
    print(f"  [ok] internal inconsistency detected: {result.corruption_reason}")

    # 8. record() on corrupt ledger raises LedgerCorruptError.
    try:
        record("msg_x", 0.01, path=test_path)
    except LedgerCorruptError as e:
        print(f"  [ok] record() on corrupt ledger raises LedgerCorruptError")
    else:
        print("  [FAIL] record() did not raise on corrupt ledger")
        sys.exit(1)

    # 9. Rollover: prior-day ledger returns fresh today ledger.
    yesterday = (datetime.now(TORONTO) - timedelta(days=1)).date()
    test_path.write_text(json.dumps({
        "schema_version": 1,
        "ledger_date": yesterday.isoformat(),
        "cumulative_cost_usd": 3.50,
        "entries": [["2026-05-17T10:00:00-04:00", "msg_yesterday", 3.50]],
    }))
    result = load(test_path)
    assert result.status == "ok", f"expected ok (rollover), got {result.status}"
    assert result.ledger.cumulative_cost_usd == 0.0, "rollover should zero out"
    assert result.ledger.entries == [], "rollover should clear entries"
    assert result.ledger.ledger_date == datetime.now(TORONTO).date()
    print("  [ok] prior-day ledger triggers rollover, fresh today ledger returned")

    # 10. record() after rollover writes today's date and starts fresh count.
    record("msg_today", 0.10, path=test_path)
    result = load(test_path)
    assert result.status == "ok"
    assert result.ledger.cumulative_cost_usd == 0.10
    assert len(result.ledger.entries) == 1
    assert result.ledger.entries[0][1] == "msg_today"
    assert result.ledger.ledger_date == datetime.now(TORONTO).date()
    print("  [ok] record() after rollover writes today's date, starts fresh count")

    shutil.rmtree(test_dir)
    print("\nspend_ledger.py self-test PASSED")
