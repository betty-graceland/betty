"""
Phase 4.6 substage (c) smoke test runner.

Runs the BRIEF's Phase 0 smoke test — three sequential atomic tool
calls that together exercise both write paths the milestone depends
on:

  T01a — emdash_create_collection("smoketest", "Smoke Test")
         Proves: orchestrator → Qwen → EmDash MCP write (DDL).

  T01b — emdash_create_field(collection="smoketest", slug="note",
                              label="Note", type="text")
         Proves: orchestrator → Qwen → EmDash MCP write (DDL,
         second call against newly-created collection).

  T02  — write_file(path="src/smoketest_marker.txt",
                    content="Smoke test <UTC iso>\\n")
         Proves: orchestrator → Qwen → filesystem write.
         NB: writes a NEW marker file rather than appending to
         src/pages/index.astro; same proof of write path, zero risk
         of mutating a load-bearing file on first run.

Architectural note: the actor's inner loop (Phase 4.3) returns
immediately on the first approved tool call — "multi-tool-call-per-
response is a Stage 5+ concern". So the smoke test is three
sequential actor_turn calls, not one chained call. Each turn produces
one Judge round-trip; the judge_decisions table should accumulate
three APPROVE rows by the end of the run.

NOT included in this smoke test:
  - git_commit_all / git_push — autonomous deploy graduates in a
    follow-on after T01 + T02 pass. Peter eyeballs the marker file
    and commits/pushes manually.
  - Dossier parser — deferred to Phase 4.6.1.
  - Read-path tools — exercised by the unit-test sweep, not by this
    smoke test (the smoke test is a write-path validator).

Expected Anthropic spend: ~$0.06–0.10 (three Judge calls @ $0.02–
0.03 each). Well inside the $5/day cap.

Run with: `uv run python -m betty_claw.smoke_test`
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# Load env from repo root before any betty_claw imports — anthropic_client
# and emdash_client both read env at module import time.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from betty_claw.actor import actor_turn  # noqa: E402
from betty_claw.anthropic_client import AnthropicClient  # noqa: E402
from betty_claw.judge import Judge  # noqa: E402


# ---------------------------------------------------------------------------
# Smoke test configuration
# ---------------------------------------------------------------------------

# Collection + field slug for T01. `smoketest` collides if the smoke test
# ran successfully before — EmDash will reject the second create. That's
# OK as a signal of prior success; Peter can clean up via admin UI between
# runs. A timestamped slug would be more idempotent but obscures the
# diagnostic "the collection already exists" signal.
SMOKETEST_COLLECTION_SLUG = "smoketest"
SMOKETEST_COLLECTION_LABEL = "Smoke Test"
SMOKETEST_FIELD_SLUG = "note"
SMOKETEST_FIELD_LABEL = "Note"
SMOKETEST_FIELD_TYPE = "text"

# Marker file lands at BETTY_SITE_DIR/src/smoketest_marker.txt. The path
# stays inside the Astro source tree (allow-list root) so write_file
# accepts it. A new file rather than appending to an existing one keeps
# this smoke test minimally invasive.
SMOKETEST_MARKER_RELPATH = "src/smoketest_marker.txt"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def _prompt_t01a() -> str:
    return (
        "Run smoke test step T01a. Call the emdash_create_collection tool "
        f"to create a new EmDash collection. Use these exact arguments: "
        f"slug={SMOKETEST_COLLECTION_SLUG!r}, label={SMOKETEST_COLLECTION_LABEL!r}. "
        "Do not include any other arguments. Just make the one tool call."
    )


def _prompt_t01b() -> str:
    return (
        "Run smoke test step T01b. Call the emdash_create_field tool to "
        f"add a field to the {SMOKETEST_COLLECTION_SLUG!r} collection. "
        f"Use these exact arguments: collection={SMOKETEST_COLLECTION_SLUG!r}, "
        f"slug={SMOKETEST_FIELD_SLUG!r}, label={SMOKETEST_FIELD_LABEL!r}, "
        f"type={SMOKETEST_FIELD_TYPE!r}. Do not include any other arguments. "
        "Just make the one tool call."
    )


def _prompt_t02(absolute_marker_path: str, marker_content: str) -> str:
    return (
        "Run smoke test step T02. Call the write_file tool to create a "
        "marker file proving the filesystem write path works. Use these "
        f"exact arguments: path={absolute_marker_path!r}, "
        f"content={marker_content!r}. Do not include any other arguments. "
        "Just make the one tool call."
    )


# ---------------------------------------------------------------------------
# Smoke test runner
# ---------------------------------------------------------------------------

def _run_step(
    name: str,
    user_message: str,
    judge: Judge,
    expected_outcome: str,
    expected_tool_name: str,
) -> tuple[bool, float, dict[str, Any]]:
    """Run one actor_turn, return (passed, anthropic_cost, details).

    `expected_outcome` is one of the ActorOutcome literals: typically
    `"tool_approved"` for Judge-gated calls or `"tool_read_only"` for
    Judge-skip calls. `expected_tool_name` is the betty_claw tool the
    actor should dispatch.

    On any deviation, prints diagnostic output and returns passed=False.
    """
    print()
    print("=" * 72)
    print(f"  Step: {name}")
    print("=" * 72)
    print(f"  Expected outcome: {expected_outcome}")
    print(f"  Expected tool:    {expected_tool_name}")
    print(f"  Prompt preview:   {user_message[:120]!r}…")
    print()

    judge.reset_turn()
    turn = actor_turn(user_message=user_message, judge=judge)

    cost = sum(v.cost_usd for v in turn.judge_verdicts)
    details: dict[str, Any] = {
        "outcome": turn.outcome,
        "iterations": turn.iterations,
        "verdicts": len(turn.judge_verdicts),
        "cost_usd": cost,
        "response_preview": turn.response[:200],
        "proposal_path": turn.proposal_path,
    }

    print(f"  Actual outcome:   {turn.outcome}")
    print(f"  Iterations:       {turn.iterations}")
    print(f"  Judge verdicts:   {len(turn.judge_verdicts)} "
          f"(cost ${cost:.4f})")
    if turn.judge_verdicts:
        first = turn.judge_verdicts[0]
        print(f"  Judge reasoning:  {first.reasoning[:200]!r}")
    print(f"  Response: {turn.response[:300]!r}")

    if turn.outcome != expected_outcome:
        print()
        print(f"  >>> FAILED: outcome {turn.outcome!r} != "
              f"expected {expected_outcome!r}")
        return False, cost, details

    # We don't have direct access to the tool name the actor dispatched
    # from turn.outcome alone. The response text typically includes the
    # tool name (synthesize_approval_response uses it); we soft-check
    # for it as a diagnostic but don't fail the step on mismatch since
    # the response synthesis isn't a hard contract.
    if expected_tool_name not in turn.response:
        print(f"  [warn] expected tool {expected_tool_name!r} not "
              f"mentioned in response; check actor logs to confirm "
              f"the right tool ran")

    print(f"  [ok] step {name} passed")
    return True, cost, details


def run_smoke_test() -> int:
    """Run the three-step smoke test. Returns exit code (0 = pass)."""
    print()
    print("#" * 72)
    print("#  Phase 4.6 substage (c) — smoke test")
    print(f"#  {datetime.now(timezone.utc).isoformat()}")
    print("#" * 72)

    # Resolve the marker file's absolute path from BETTY_SITE_DIR.
    # Imported lazily so the env-driven resolution happens after
    # load_dotenv() at module top.
    from betty_claw.tools.filesystem import BETTY_SITE_DIR
    marker_abspath = str(BETTY_SITE_DIR / SMOKETEST_MARKER_RELPATH)
    marker_content = f"Smoke test {datetime.now(timezone.utc).isoformat()}\n"

    print()
    print(f"  Site dir: {BETTY_SITE_DIR}")
    print(f"  Marker:   {marker_abspath}")
    print(f"  Content:  {marker_content!r}")

    judge = Judge(anthropic_client=AnthropicClient())

    steps_passed = 0
    total_cost = 0.0
    failures: list[str] = []

    # ---- T01a: create_collection ----
    ok, cost, _ = _run_step(
        name="T01a (emdash_create_collection)",
        user_message=_prompt_t01a(),
        judge=judge,
        expected_outcome="tool_approved",
        expected_tool_name="emdash_create_collection",
    )
    total_cost += cost
    if ok:
        steps_passed += 1
    else:
        failures.append("T01a")

    # ---- T01b: create_field ----
    # Only attempt if T01a succeeded — the field create requires the
    # collection to exist, so chaining past a T01a failure would
    # produce a misleading MCP error.
    if "T01a" not in failures:
        ok, cost, _ = _run_step(
            name="T01b (emdash_create_field)",
            user_message=_prompt_t01b(),
            judge=judge,
            expected_outcome="tool_approved",
            expected_tool_name="emdash_create_field",
        )
        total_cost += cost
        if ok:
            steps_passed += 1
        else:
            failures.append("T01b")
    else:
        print()
        print("  [skip] T01b — depends on T01a which failed")
        failures.append("T01b (skipped)")

    # ---- T02: write_file ----
    ok, cost, _ = _run_step(
        name="T02 (write_file marker)",
        user_message=_prompt_t02(marker_abspath, marker_content),
        judge=judge,
        expected_outcome="tool_approved",
        expected_tool_name="write_file",
    )
    total_cost += cost
    if ok:
        steps_passed += 1
        # Confirm the marker file actually exists on disk.
        marker_path = Path(marker_abspath)
        if marker_path.exists():
            actual_content = marker_path.read_text(encoding="utf-8")
            if actual_content == marker_content:
                print(f"  [ok] marker file verified on disk "
                      f"({len(actual_content)} bytes)")
            else:
                print(f"  [warn] marker file exists but content differs: "
                      f"expected {marker_content!r}, got {actual_content!r}")
        else:
            print(f"  [warn] marker file not found at {marker_abspath!r} "
                  f"after write_file claimed success")
    else:
        failures.append("T02")

    # ---- Summary ----
    print()
    print("#" * 72)
    print(f"#  Summary: {steps_passed}/3 steps passed; "
          f"total Anthropic cost ${total_cost:.4f}")
    if failures:
        print(f"#  Failed: {', '.join(failures)}")
    print("#" * 72)
    print()

    if steps_passed == 3:
        print("Phase 4.6 substage (c) smoke test PASSED.")
        print()
        print("Next steps (manual):")
        print(f"  1. Verify the {SMOKETEST_COLLECTION_SLUG!r} collection "
              f"exists in the EmDash admin UI with one '{SMOKETEST_FIELD_SLUG}' "
              f"field.")
        print(f"  2. Inspect the marker file at:")
        print(f"     {marker_abspath}")
        print(f"  3. If both look right, commit + push to vic-overnight-test "
              f"manually (Betty did NOT push autonomously this round).")
        print(f"  4. Cloudflare's CI/CD picks up the push and deploys to a "
              f"preview URL.")
        print()
        return 0
    else:
        print(f"Phase 4.6 substage (c) smoke test FAILED "
              f"({len(failures)} step(s) did not pass).")
        print()
        print("Diagnostic next steps:")
        print(f"  - Query judge_decisions for the recent rows: "
              f"SELECT * FROM judge_decisions ORDER BY timestamp DESC LIMIT 10;")
        print(f"  - Check Ollama logs for the actor's last tool-call attempt.")
        print(f"  - If Qwen emitted the wrong tool or wrong args, revise the "
              f"prompts in this file (they may need to be more explicit "
              f"about argument names).")
        return 1


if __name__ == "__main__":
    sys.exit(run_smoke_test())
