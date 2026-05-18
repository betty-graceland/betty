"""
draft_email tool: proposes an email send without actually sending.

CONTRACT
========
draft_email validates its arguments BEFORE generating a call_id or writing
any file. If validation fails, ValueError is raised and the proposals
directory is unchanged. This means the proposals directory answers the
question "did this tool actually attempt to run?" purely from filesystem
state — a UUID on disk means a validated call happened.

Validation is strict: required keys must be present, must be non-empty
strings, and no extra keys are permitted. Silent coercion is forbidden;
if Qwen emits {"to": ["x@y.com"]} instead of {"to": "x@y.com"}, we reject
loudly rather than guessing.

This tool does NOT send email. It writes a proposal JSON file to
~/code/betty/claw/proposals/<call_id>.json and returns a ToolResult with
status="proposed". The Judge (Phase 4.3) will consume the proposal file
and decide whether to approve execution.

PROPOSAL JSON SHAPE
===================
{
  "schema_version": 1,
  "call_id": "<uuid4>",
  "tool_name": "draft_email",
  "proposed_at": "<iso8601-utc>",
  "arguments": {"to": "...", "subject": "...", "body": "..."}
}

schema_version starts at 1. When Phase 4.3 adds a verdict block (or any
field changes), bump the version and handle migration in the Judge reader.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

from betty_claw.types import ToolCall, ToolResult

# Proposals directory. Resolved relative to this file so the path is stable
# regardless of the CWD the actor runs from.
#
# Path math: this file is at <repo>/claw/betty_claw/tools/draft_email.py
#   parents[0] = tools/
#   parents[1] = betty_claw/
#   parents[2] = claw/
#   parents[3] = <repo root>
# So parents[3] / "claw" / "proposals" = <repo>/claw/proposals/
_PROPOSALS_DIR = Path(__file__).resolve().parents[3] / "claw" / "proposals"

# Schema version for the proposal JSON shape. Bump when fields change.
_SCHEMA_VERSION = 1

# Required argument keys. Strict: no extra keys permitted.
_REQUIRED_KEYS = frozenset({"to", "subject", "body"})


def _validate_arguments(args: dict) -> Tuple[str, str, str]:
    """
    Validate the raw argument dict from Qwen's tool-call output.

    Raises ValueError on any of:
      - Missing required key
      - Extra unknown key
      - Non-string value
      - Empty-string value (after strip)

    Returns the validated (to, subject, body) tuple. Validation happens
    BEFORE any call_id is generated or any file is written.
    """
    if not isinstance(args, dict):
        raise ValueError(
            f"draft_email arguments must be a dict, got {type(args).__name__}"
        )

    provided_keys = set(args.keys())
    missing = _REQUIRED_KEYS - provided_keys
    if missing:
        raise ValueError(
            f"draft_email missing required keys: {sorted(missing)}"
        )

    extra = provided_keys - _REQUIRED_KEYS
    if extra:
        raise ValueError(
            f"draft_email received unknown keys: {sorted(extra)}. "
            f"Allowed keys: {sorted(_REQUIRED_KEYS)}"
        )

    for key in _REQUIRED_KEYS:
        value = args[key]
        if not isinstance(value, str):
            raise ValueError(
                f"draft_email argument {key!r} must be str, "
                f"got {type(value).__name__}: {value!r}"
            )
        if not value.strip():
            raise ValueError(
                f"draft_email argument {key!r} must be non-empty"
            )

    return args["to"], args["subject"], args["body"]


def _write_proposal_atomic(path: Path, payload: dict) -> None:
    """
    Write proposal JSON atomically: write to <path>.tmp, fsync, then
    os.replace.

    os.replace is atomic on POSIX (and on Windows for same-filesystem
    renames). fsync before rename guards against the rare crash window
    where the rename completes but the data hasn't hit the platter,
    leaving a file that exists but is empty.

    This guarantees the Judge in Phase 4.3 cannot observe a partial-write
    state.
    """
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def draft_email(args: dict) -> ToolResult:
    """
    Propose an email send. Does NOT send.

    Validates args, generates a UUID4 call_id, writes a proposal JSON file
    to the proposals directory, and returns ToolResult(status="proposed").

    Raises ValueError if args fail validation. In the validation-failure
    case, no call_id is generated and no file is written.
    """
    to, subject, body = _validate_arguments(args)

    # Validation passed. Now we can mint a call_id and commit to disk.
    call_id = str(uuid.uuid4())
    proposed_at = datetime.now(timezone.utc).isoformat()

    proposal_path = _PROPOSALS_DIR / f"{call_id}.json"

    # Ensure the proposals directory exists. parents=True is defensive
    # against fresh clones where .gitkeep might be missing.
    _PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": _SCHEMA_VERSION,
        "call_id": call_id,
        "tool_name": "draft_email",
        "proposed_at": proposed_at,
        "arguments": {
            "to": to,
            "subject": subject,
            "body": body,
        },
    }

    _write_proposal_atomic(proposal_path, payload)

    return ToolResult(
        call_id=call_id,
        tool_name="draft_email",
        status="proposed",
        payload={"proposal_path": str(proposal_path.resolve())},
    )


def _self_test() -> None:
    """
    Live self-test. Exercises the happy path and all four validation
    failure modes. Snapshots the proposals directory before and after to
    verify the "validate before UUID, validate before disk" contract:
    exactly one new file should appear, corresponding to the happy path.

    Cleans up any created proposal files even on assertion failure.
    """
    created_paths: list[Path] = []

    # Snapshot the proposals directory BEFORE any calls. The contract
    # under test: validation-failure paths must not write files, so the
    # post-snapshot minus the pre-snapshot must equal exactly the
    # happy-path file.
    _PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    before = set(_PROPOSALS_DIR.glob("*.json"))

    try:
        # --- Happy path ---
        result = draft_email({
            "to": "test@example.com",
            "subject": "Self-test subject",
            "body": "Self-test body content.",
        })

        assert result.status == "proposed", (
            f"expected status='proposed', got {result.status!r}"
        )
        assert result.tool_name == "draft_email"
        assert result.call_id, "call_id should be non-empty"

        proposal_path = Path(result.payload["proposal_path"])
        created_paths.append(proposal_path)

        assert proposal_path.is_absolute(), (
            f"proposal_path must be absolute, got {proposal_path}"
        )
        assert proposal_path.exists(), (
            f"proposal file missing at {proposal_path}"
        )

        with open(proposal_path, "r", encoding="utf-8") as f:
            written = json.load(f)

        assert written["schema_version"] == _SCHEMA_VERSION
        assert written["call_id"] == result.call_id
        assert written["tool_name"] == "draft_email"
        assert written["arguments"]["to"] == "test@example.com"
        assert written["arguments"]["subject"] == "Self-test subject"
        assert written["arguments"]["body"] == "Self-test body content."
        assert "proposed_at" in written

        # --- Validation: missing key ---
        try:
            draft_email({"to": "x@y.com", "subject": "no body"})
        except ValueError as e:
            assert "missing required keys" in str(e), (
                f"unexpected error message: {e}"
            )
        else:
            raise AssertionError("expected ValueError for missing key")

        # --- Validation: extra key ---
        try:
            draft_email({
                "to": "x@y.com",
                "subject": "s",
                "body": "b",
                "cc": "extra@y.com",
            })
        except ValueError as e:
            assert "unknown keys" in str(e), (
                f"unexpected error message: {e}"
            )
        else:
            raise AssertionError("expected ValueError for extra key")

        # --- Validation: wrong type (list instead of str) ---
        try:
            draft_email({
                "to": ["x@y.com"],
                "subject": "s",
                "body": "b",
            })
        except ValueError as e:
            assert "must be str" in str(e), (
                f"unexpected error message: {e}"
            )
        else:
            raise AssertionError("expected ValueError for list-typed 'to'")

        # --- Validation: empty string ---
        try:
            draft_email({"to": "x@y.com", "subject": "", "body": "b"})
        except ValueError as e:
            assert "non-empty" in str(e), (
                f"unexpected error message: {e}"
            )
        else:
            raise AssertionError("expected ValueError for empty subject")

        # --- Contract verification: snapshot diff ---
        # The "validate before UUID, validate before disk" contract says
        # validation failures leave the proposals directory untouched.
        # After one happy path and four validation failures, exactly one
        # new file should exist, and it must be the happy-path file.
        after = set(_PROPOSALS_DIR.glob("*.json"))
        new_files = after - before
        assert len(new_files) == 1, (
            f"expected exactly 1 new proposal file (validation failures "
            f"must not write); got {len(new_files)}: {new_files}"
        )
        assert proposal_path in new_files, (
            f"happy-path file {proposal_path} not found in new files "
            f"{new_files}"
        )

        # Also confirm no atomic-write tempfiles leaked.
        tmp_leftovers = list(_PROPOSALS_DIR.glob("*.tmp"))
        assert not tmp_leftovers, (
            f"atomic-write tempfiles leaked: {tmp_leftovers}"
        )

        print("draft_email self-test: PASS")
        print(f"  happy-path call_id: {result.call_id}")
        print(f"  proposal_path: {proposal_path}")
        print(f"  validation failures verified: 4 (no files written)")

    finally:
        # Clean up any proposal files this test created. Validation-failure
        # paths shouldn't have created files (that's the contract being
        # tested), but if the contract was broken, the snapshot assertion
        # above already caught it — cleanup here just leaves the directory
        # tidy for the next run.
        for p in created_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                print(f"  warning: cleanup failed for {p}: {e}")


if __name__ == "__main__":
    _self_test()
