"""
Phase 4.6.2 — single-dossier end-to-end chain validator.

Drives Betty through the full content-population pipeline for ONE
Airbnb dossier:

  Step 1 — parse_airbnb_dossier(path)
           Reads the dossier markdown, returns a Stays-shaped dict.
           risk_class=read_only → Judge-skip → SKIP_READ_ONLY audit row.

  Step 2 — emdash_create_content_draft(collection="stays", data=...)
           Creates a draft Stays entry in EmDash. Reversible_write → Judge.
           Pause after this step so Peter can eyeball the draft in the
           EmDash admin UI before publication.

  Step 3 — emdash_publish_content(collection="stays", id=...)
           Moves the draft to live on travelpec.com.
           external_side_effect → Judge with highest rigor.

Architecture note: the actor's inner loop returns immediately on the
first approved tool call (Phase 4.3 single-action-per-turn), so this
script wraps three sequential actor_turn invocations and threads the
state from each into the next via the Phase 4.6.2 ActorTurn.tool_result
field. Step 1's parsed Stays dict feeds Step 2's prompt; Step 2's
new-entry id feeds Step 3's prompt.

DESIGN CHOICES
==============
- **Slimmed Stays dict for Step 2 (no description field).** The parsed
  description is ~2000 chars including embedded newlines. Reliably
  round-tripping that through Qwen's tool-call JSON emission is a
  known Qwen-JSON-adherence risk. For this first chain-validation run
  we omit `description` from the create_draft args — required Stays
  fields are still satisfied (title, village, persona, outbound_url,
  provider) and the parsed description is preserved in the runner's
  log for follow-up addition via admin UI or a Phase 4.6.2.1 update
  step. If Qwen handles the slim version cleanly, a follow-on can
  include description.

- **Human-review pause** between Step 2 and Step 3. The first time
  Betty creates real Stays content from a real Airbnb dossier, Peter
  eyeballs the draft in the EmDash admin UI before authorizing the
  publish step. Future bulk-content runners can skip the pause once
  the chain is trusted.

- **Cost: ~$0.04 Anthropic spend.** Step 1 is read-only (Judge-skip,
  $0). Steps 2 and 3 each hit the Judge once at ~$0.02. The
  smoke_test cost ~$0.06 because all three steps were Judge-gated;
  this chain's first step skips the Judge.

Run with: `uv run python -m betty_claw.single_dossier_test <dossier_path>`
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


# Load env from repo root before any betty_claw imports.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from betty_claw.actor import actor_turn  # noqa: E402
from betty_claw.anthropic_client import AnthropicClient  # noqa: E402
from betty_claw.judge import Judge  # noqa: E402


# ---------------------------------------------------------------------------
# Step prompts
# ---------------------------------------------------------------------------

def _prompt_parse(dossier_path: str) -> str:
    return (
        f"Call the parse_airbnb_dossier tool to read and parse an Airbnb "
        f"research dossier into a Stays-compatible dict. Use these exact "
        f"arguments: path='{dossier_path}'. Make exactly one tool call."
    )


def _prompt_create_draft(slim_data: dict[str, Any]) -> str:
    """Build the create_draft prompt with the slim Stays data inline.

    Presents the data field-by-field rather than as one JSON blob to
    reduce the risk of Qwen paraphrasing values. The instruction is
    explicit: pass each value verbatim.
    """
    lines = [
        "Call emdash_create_content_draft to create a new draft Stays "
        "entry in EmDash. Pass these arguments exactly — do not "
        "paraphrase, summarize, or alter any values:",
        "",
        "  collection: \"stays\"",
        "  data:",
    ]
    for key in (
        "title", "village", "persona", "outbound_url", "provider",
        "is_advertised", "featured_eligible", "bedrooms", "capacity",
        "schema_subtype",
    ):
        if key not in slim_data:
            continue
        value = slim_data[key]
        # Python repr keeps strings/bools/numbers/lists in a form Qwen
        # can mirror into JSON without ambiguity. Booleans show up as
        # `True`/`False` in repr; we hand-tweak those to `true`/`false`
        # so the JSON-tool-call rendering Qwen produces is correct.
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str):
            # Escape any embedded double-quotes; preserve the string as
            # a double-quoted JSON literal.
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            rendered = f'"{escaped}"'
        else:
            rendered = repr(value)
        lines.append(f"    {key}: {rendered}")

    lines.extend([
        "",
        "Do not include any other arguments. The `description` field "
        "is intentionally omitted from this draft — it will be added "
        "later. Make exactly one tool call.",
    ])
    return "\n".join(lines)


def _prompt_publish(collection: str, content_id: str) -> str:
    return (
        f"Call emdash_publish_content to publish the Stays draft we "
        f"just created. Use these exact arguments: "
        f"collection='{collection}', id='{content_id}'. "
        f"Make exactly one tool call."
    )


# ---------------------------------------------------------------------------
# Step runner
# ---------------------------------------------------------------------------

def _run_step(
    name: str,
    user_message: str,
    judge: Judge,
    expected_outcome: str,
) -> tuple[bool, float, dict[str, Any] | None]:
    """Run one actor_turn, return (passed, anthropic_cost, tool_payload).

    tool_payload is the result tool's payload dict if outcome matched,
    otherwise None. Caller uses it to extract data for the next step.
    """
    print()
    print("=" * 72)
    print(f"  Step: {name}")
    print("=" * 72)
    print(f"  Expected outcome: {expected_outcome}")
    print(f"  Prompt (first 200 chars): {user_message[:200]!r}…")
    print()

    judge.reset_turn()
    turn = actor_turn(user_message=user_message, judge=judge)

    cost = sum(v.cost_usd for v in turn.judge_verdicts)
    print(f"  Actual outcome:   {turn.outcome}")
    print(f"  Iterations:       {turn.iterations}")
    print(f"  Judge verdicts:   {len(turn.judge_verdicts)} "
          f"(cost ${cost:.4f})")
    if turn.judge_verdicts:
        first = turn.judge_verdicts[0]
        print(f"  Judge reasoning:  {first.reasoning[:200]!r}")
    print(f"  Response: {turn.response[:300]!r}")

    if turn.outcome != expected_outcome:
        print(f"\n  >>> FAILED: outcome {turn.outcome!r} != "
              f"expected {expected_outcome!r}")
        return False, cost, None

    payload = turn.tool_result.payload if turn.tool_result else None
    print(f"  [ok] step {name} passed")
    return True, cost, payload


# ---------------------------------------------------------------------------
# Slim Stays-data extractor
# ---------------------------------------------------------------------------

# Fields we include in the first-run create_draft. Description is
# intentionally omitted (see module docstring). All other Stays fields
# the parser produces flow through.
_SLIM_STAYS_FIELDS = (
    "title", "village", "persona", "outbound_url", "provider",
    "is_advertised", "featured_eligible", "bedrooms", "capacity",
    "schema_subtype",
)


def _slim_stays_data(parsed_data: dict[str, Any]) -> dict[str, Any]:
    """Return a Stays dict trimmed to the fields used in the create_draft.

    Preserves field types as the parser produced them. The slim set is
    small enough (~400 chars JSON-serialized) that Qwen reliably emits
    it as a tool-call argument; the full description is omitted here
    and re-added in a follow-on update or via admin UI.
    """
    return {
        key: parsed_data[key]
        for key in _SLIM_STAYS_FIELDS
        if key in parsed_data
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_single_dossier_test(dossier_path: str) -> int:
    """Run the three-step chain for one Airbnb dossier. Returns exit code."""
    print()
    print("#" * 72)
    print("#  Phase 4.6.2 — single-dossier chain validator")
    print(f"#  {datetime.now(timezone.utc).isoformat()}")
    print("#" * 72)
    print()
    print(f"  Dossier: {dossier_path}")

    judge = Judge(anthropic_client=AnthropicClient())

    steps_passed = 0
    total_cost = 0.0
    failures: list[str] = []

    # ---- Step 1: parse ----
    ok, cost, payload = _run_step(
        name="Step 1 (parse_airbnb_dossier)",
        user_message=_prompt_parse(dossier_path),
        judge=judge,
        expected_outcome="tool_read_only",
    )
    total_cost += cost
    if not ok or payload is None:
        failures.append("Step 1 (parse)")
        print(f"\nPhase 4.6.2 FAILED at Step 1.")
        return 1
    steps_passed += 1
    parsed_data = payload.get("data")
    if not isinstance(parsed_data, dict):
        print(f"\n  >>> Step 1 payload missing 'data' dict: {payload!r}")
        return 1
    print()
    print(f"  Parsed Stays dict ({len(parsed_data)} fields):")
    for key in sorted(parsed_data.keys()):
        value = parsed_data[key]
        if isinstance(value, str) and len(value) > 100:
            display = f"{value[:80]!r}… ({len(value)} chars)"
        else:
            display = repr(value)
        print(f"    {key}: {display}")

    # ---- Step 2: create draft ----
    slim_data = _slim_stays_data(parsed_data)
    print()
    print(f"  Slim Stays data for create_draft "
          f"({len(slim_data)} fields, "
          f"~{len(json.dumps(slim_data))} JSON chars):")
    print(f"    {json.dumps(slim_data, indent=4)}")

    ok, cost, payload = _run_step(
        name="Step 2 (emdash_create_content_draft)",
        user_message=_prompt_create_draft(slim_data),
        judge=judge,
        expected_outcome="tool_approved",
    )
    total_cost += cost
    if not ok or payload is None:
        failures.append("Step 2 (create_draft)")
        print(f"\nPhase 4.6.2 FAILED at Step 2; spend so far ${total_cost:.4f}.")
        return 1
    steps_passed += 1

    # Extract the new entry's id. EmDash returns the created object in
    # the response; the actual id key may be `id` at the top level or
    # nested. _wrap_result's payload puts the EmDash response under
    # `data`, which itself may have shape {"id": ...} or {"item": {"id": ...}}.
    response_data = payload.get("data")
    new_id = None
    if isinstance(response_data, dict):
        new_id = response_data.get("id")
        if new_id is None and isinstance(response_data.get("item"), dict):
            new_id = response_data["item"].get("id")
    if not new_id:
        print(f"\n  >>> Step 2 payload missing entry id: "
              f"data={response_data!r}")
        print("  Cannot continue to publish step without an id.")
        return 1
    print(f"\n  Created Stays draft id: {new_id!r}")

    # ---- Pause for human review ----
    print()
    print("=" * 72)
    print("  PAUSE FOR HUMAN REVIEW")
    print("=" * 72)
    print()
    print(f"  Draft created: collection='stays', id={new_id!r}")
    print(f"  Title: {parsed_data.get('title', '?')!r}")
    print(f"  Persona: {parsed_data.get('persona', '?')[:120]!r}")
    print()
    print(f"  Open the EmDash admin UI and verify the draft looks right:")
    print(f"    1. Navigate to Stays collection → drafts.")
    print(f"    2. Open the entry with id={new_id!r}.")
    print(f"    3. Confirm title, village, persona, outbound_url, "
          f"provider, bedrooms, capacity, schema_subtype are all "
          f"populated correctly.")
    print(f"    4. is_advertised should be 0 (false). featured_eligible "
          f"should be 0 (false).")
    print(f"    5. description will be EMPTY — that's intentional for "
          f"this first run.")
    print()
    print("  Press Enter to PUBLISH the draft (it will go live on "
          "travelpec.com).")
    print("  Press Ctrl+C to ABORT (draft stays as draft; you can "
          "manually delete via admin UI).")
    print()
    try:
        input("  Continue? ")
    except KeyboardInterrupt:
        print()
        print()
        print("  Aborted by operator. Draft remains in EmDash as a "
              "draft. Spend so far: "
              f"${total_cost:.4f}.")
        return 130

    # ---- Step 3: publish ----
    ok, cost, _ = _run_step(
        name="Step 3 (emdash_publish_content)",
        user_message=_prompt_publish("stays", new_id),
        judge=judge,
        expected_outcome="tool_approved",
    )
    total_cost += cost
    if not ok:
        failures.append("Step 3 (publish)")

    if ok:
        steps_passed += 1

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
        print("Phase 4.6.2 chain validator PASSED.")
        print()
        print(f"Next steps (manual):")
        print(f"  1. Verify the entry is now published in EmDash admin "
              f"(should appear in the published list, not drafts).")
        print(f"  2. Check it renders on travelpec.com — the site "
              f"build should pick up the published Stays entry.")
        print(f"  3. If everything looks good, the chain is validated. "
              f"Phase 4.6.3+ can scale to bulk dossier processing.")
        print()
        print(f"Description field reminder: this run omitted "
              f"`description` from the draft (parsed value preserved "
              f"in the log above). Add it via admin UI, or via a "
              f"follow-on emdash_update_content_draft call.")
        return 0
    else:
        print("Phase 4.6.2 chain validator FAILED.")
        print()
        print(f"Diagnostic:")
        print(f"  - Query judge_decisions: SELECT * FROM judge_decisions "
              f"ORDER BY timestamp DESC LIMIT 10;")
        print(f"  - If Step 2 failed: check Qwen's tool-call args against "
              f"the prompt's spec. Qwen may have paraphrased a value.")
        print(f"  - If Step 3 failed: the draft from Step 2 exists in "
              f"EmDash; clean up via admin UI or skip-on-rerun.")
        return 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: uv run python -m betty_claw.single_dossier_test "
              "<absolute_path_to_dossier.md>")
        print()
        print("Example:")
        print("  uv run python -m betty_claw.single_dossier_test \\")
        print("    /Users/betty/travelpec-com/01-source-data/research/"
              "airbnb-listings/3_Bed_PEC_Home_Loads_of_Style_12_hr_to_"
              "Sandbanks.md")
        sys.exit(2)

    sys.exit(run_single_dossier_test(sys.argv[1]))
