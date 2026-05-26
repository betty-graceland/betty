"""
Git tools for Betty's Phase 4.6 actor.

Four atomic tools wrap `git` operations against BETTY_SITE_DIR (the
travelpec.com Astro project working tree by default):

  - git_status  (read_only)         — git status --porcelain
  - git_diff    (read_only)         — git diff [path] [--staged]
  - git_commit_all (reversible_write) — git add -A && git commit -m ...
  - git_push    (external_side_effect) — git push origin HEAD:vic-overnight

Per Phase 4.4 Hard Rule 3 (BRIEF, non-negotiable): Betty MUST NOT push
to `main`. Hard Rule encoded structurally here: git_push hard-codes the
remote branch as `vic-overnight`. The tool's schema does NOT expose a
branch parameter, so Qwen cannot redirect the push. Peter merges to
main manually after reviewing what landed on vic-overnight.

All git invocations use subprocess.run with shell=False and a fixed
argv list — no string interpolation into a shell, no `git -c …` chaining.
The commit message is passed through `-m <message>` argv, so shell
metacharacters in the message are inert.

Each tool's validator rejects extra arguments (strict schema match) so
schema/validator drift is loud, not silent.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from betty_claw.contracts import ToolResult
from betty_claw.tools.filesystem import BETTY_SITE_DIR


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The branch Betty pushes to. Hard-coded per Hard Rule 3.
VIC_OVERNIGHT_BRANCH = "vic-overnight"

# Subprocess timeout. git_push to GitHub typically completes in <5s; git
# status/diff in <1s. 60s headroom handles slow remote auth or large diffs
# without hanging the actor loop.
_GIT_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Shared subprocess helper
# ---------------------------------------------------------------------------

def _run_git(argv: list[str], cwd: Path = BETTY_SITE_DIR) -> subprocess.CompletedProcess:
    """Run `git <argv>` in `cwd`, capturing stdout/stderr.

    Returns the CompletedProcess regardless of returncode — callers
    decide whether non-zero is a failure for their semantics. Raises
    only on subprocess infrastructure failures (timeout, missing git,
    cwd doesn't exist).
    """
    if not cwd.exists():
        raise ValueError(
            f"Git working directory does not exist: {cwd}. "
            f"Check BETTY_SITE_DIR env var or clone the project."
        )
    if not (cwd / ".git").exists():
        raise ValueError(
            f"Not a git repository: {cwd} has no .git/ directory."
        )

    try:
        return subprocess.run(
            ["git", *argv],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,  # don't raise on non-zero; caller inspects
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError(
            f"git {argv[0]} timed out after {_GIT_TIMEOUT_SECONDS}s. "
            f"Network issue or hung process."
        ) from e
    except FileNotFoundError as e:
        raise ValueError("`git` binary not found on PATH.") from e


def _current_head_sha(cwd: Path = BETTY_SITE_DIR) -> str:
    """Return the current HEAD SHA (short form). Used for commit confirmation."""
    proc = _run_git(["rev-parse", "--short", "HEAD"], cwd=cwd)
    if proc.returncode != 0:
        return "(unknown)"
    return proc.stdout.strip()


def _current_branch(cwd: Path = BETTY_SITE_DIR) -> str:
    """Return the current local branch name, or '(detached)' if not on a branch."""
    proc = _run_git(["symbolic-ref", "--short", "-q", "HEAD"], cwd=cwd)
    if proc.returncode != 0:
        return "(detached)"
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# Tool: git_status
# ---------------------------------------------------------------------------

GIT_STATUS_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "git_status",
        "description": (
            "Show the working-tree status of the Astro project repo: which "
            "files are modified, staged, untracked, or deleted. Uses "
            "`git status --porcelain` for machine-readable output. Returns "
            "empty entries list when the working tree is clean."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def _validate_git_status_args(args: dict) -> None:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    if args:
        raise ValueError(
            f"git_status takes no arguments; got {sorted(args.keys())}"
        )


def git_status(args: dict) -> ToolResult:
    """Show working-tree status. risk_class=read_only."""
    _validate_git_status_args(args)

    proc = _run_git(["status", "--porcelain", "-b"])
    if proc.returncode != 0:
        raise ValueError(
            f"git status failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )

    lines = proc.stdout.splitlines()
    branch_line = lines[0] if lines and lines[0].startswith("## ") else None
    entry_lines = lines[1:] if branch_line else lines

    entries = []
    for line in entry_lines:
        if len(line) < 3:
            continue
        # Porcelain format: XY <path> where X is staged, Y is unstaged.
        code = line[:2]
        path = line[3:]
        entries.append({"code": code, "path": path})

    branch_name = _current_branch()
    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="git_status",
        status="executed",
        payload={
            "branch": branch_name,
            "branch_line": branch_line,
            "entries": entries,
            "clean": len(entries) == 0,
            "summary": (
                f"On branch {branch_name}; "
                f"{'clean working tree' if not entries else f'{len(entries)} changed entries'}."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Tool: git_diff
# ---------------------------------------------------------------------------

GIT_DIFF_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "git_diff",
        "description": (
            "Show pending changes in the Astro project repo. By default "
            "shows unstaged changes against the working tree. Optionally "
            "filter to a specific path, or show staged changes against HEAD."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Optional path (relative to repo root) to scope "
                        "the diff. Omit to diff the entire working tree."
                    ),
                },
                "staged": {
                    "type": "boolean",
                    "description": (
                        "If true, show staged changes (git diff --staged). "
                        "If false or omitted, show unstaged changes."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}

# Diff output can be large (multi-MB) on a fresh build. Cap to keep the
# actor context manageable. If a real diff exceeds this, Betty should
# either commit smaller batches or scope to specific paths.
_GIT_DIFF_MAX_CHARS = 64 * 1024


def _validate_git_diff_args(args: dict) -> tuple[str | None, bool]:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    extra = set(args.keys()) - {"path", "staged"}
    if extra:
        raise ValueError(f"git_diff received unknown keys: {sorted(extra)}")

    path = args.get("path")
    if path is not None and not isinstance(path, str):
        raise ValueError(f"path must be str, got {type(path).__name__}")

    staged = args.get("staged", False)
    if not isinstance(staged, bool):
        raise ValueError(f"staged must be bool, got {type(staged).__name__}")

    return path, staged


def git_diff(args: dict) -> ToolResult:
    """Show diff. risk_class=read_only."""
    path, staged = _validate_git_diff_args(args)

    argv = ["diff"]
    if staged:
        argv.append("--staged")
    if path:
        argv.extend(["--", path])

    proc = _run_git(argv)
    if proc.returncode != 0:
        raise ValueError(
            f"git diff failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )

    diff_text = proc.stdout
    truncated = False
    if len(diff_text) > _GIT_DIFF_MAX_CHARS:
        diff_text = diff_text[:_GIT_DIFF_MAX_CHARS]
        truncated = True

    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="git_diff",
        status="executed",
        payload={
            "path": path,
            "staged": staged,
            "diff": diff_text,
            "truncated": truncated,
            "summary": (
                f"Diff ({'staged' if staged else 'unstaged'}"
                f"{f' for {path}' if path else ''}): "
                f"{len(diff_text)} chars"
                f"{' (truncated)' if truncated else ''}."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Tool: git_commit_all
# ---------------------------------------------------------------------------

GIT_COMMIT_ALL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "git_commit_all",
        "description": (
            "Stage all changes in the working tree and create a commit. "
            "Equivalent to `git add -A && git commit -m <message>`. The "
            "commit lands on whatever branch is currently checked out. "
            "Note: Betty must NOT be on the `main` branch when committing "
            "(per Hard Rule 3 from the project BRIEF). The tool errors "
            "if current branch is `main`."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Commit message. Must be non-empty. Multi-line "
                        "messages are supported."
                    ),
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    },
}


def _validate_git_commit_all_args(args: dict) -> str:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    if set(args.keys()) != {"message"}:
        raise ValueError(
            f"git_commit_all expects exactly {{'message'}}; "
            f"got {sorted(args.keys())}"
        )
    message = args["message"]
    if not isinstance(message, str):
        raise ValueError(f"message must be str, got {type(message).__name__}")
    if not message.strip():
        raise ValueError("message must be non-empty")
    return message


def git_commit_all(args: dict) -> ToolResult:
    """Stage all + commit. risk_class=reversible_write."""
    message = _validate_git_commit_all_args(args)

    # Hard Rule 3 enforcement: refuse to commit on `main`.
    current = _current_branch()
    if current == "main":
        raise ValueError(
            "Refusing to commit on `main`. Betty's commits must land on a "
            "non-main branch (typically `vic-overnight`) per Hard Rule 3 "
            "from the project BRIEF. Check out the target branch first."
        )

    # Stage everything.
    proc = _run_git(["add", "-A"])
    if proc.returncode != 0:
        raise ValueError(
            f"git add -A failed (rc={proc.returncode}): {proc.stderr[:500]}"
        )

    # Commit. If there's nothing to commit, `git commit` exits with code 1
    # and a "nothing to commit" message — treat that as a soft no-op
    # rather than an error, since it's a legitimate state.
    proc = _run_git(["commit", "-m", message])
    if proc.returncode != 0:
        combined = (proc.stdout + proc.stderr).lower()
        if "nothing to commit" in combined or "no changes added" in combined:
            return ToolResult(
                call_id=str(uuid.uuid4()),
                tool_name="git_commit_all",
                status="executed",
                payload={
                    "branch": current,
                    "commit_sha": _current_head_sha(),
                    "nothing_to_commit": True,
                    "summary": "Nothing to commit; working tree was clean.",
                },
            )
        raise ValueError(
            f"git commit failed (rc={proc.returncode}): "
            f"{proc.stderr[:500] or proc.stdout[:500]}"
        )

    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="git_commit_all",
        status="executed",
        payload={
            "branch": current,
            "commit_sha": _current_head_sha(),
            "nothing_to_commit": False,
            "summary": (
                f"Committed to {current} at {_current_head_sha()}: "
                f"{message.splitlines()[0][:80]}"
            ),
        },
    )


# ---------------------------------------------------------------------------
# Tool: git_push
# ---------------------------------------------------------------------------

GIT_PUSH_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "git_push",
        "description": (
            "Push the current HEAD to the remote `vic-overnight` branch on "
            "origin. The remote branch is hard-coded per Hard Rule 3 of the "
            "project BRIEF — Betty never pushes to `main`. Equivalent to "
            "`git push origin HEAD:vic-overnight`. After the push, "
            "Cloudflare's CI/CD picks up the new commit on vic-overnight "
            "and deploys to a preview/staging environment. Peter merges to "
            "`main` manually after reviewing."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def _validate_git_push_args(args: dict) -> None:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    if args:
        raise ValueError(
            f"git_push takes no arguments; got {sorted(args.keys())}. "
            f"The destination branch is hard-coded to vic-overnight."
        )


def git_push(args: dict) -> ToolResult:
    """Push HEAD to origin/vic-overnight. risk_class=external_side_effect."""
    _validate_git_push_args(args)

    head_sha = _current_head_sha()
    refspec = f"HEAD:{VIC_OVERNIGHT_BRANCH}"

    proc = _run_git(["push", "origin", refspec])
    if proc.returncode != 0:
        raise ValueError(
            f"git push failed (rc={proc.returncode}): "
            f"{proc.stderr[:1000] or proc.stdout[:1000]}"
        )

    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="git_push",
        status="executed",
        payload={
            "remote": "origin",
            "branch": VIC_OVERNIGHT_BRANCH,
            "head_sha": head_sha,
            "stderr": proc.stderr.strip(),  # git push reports progress on stderr
            "summary": (
                f"Pushed {head_sha} to origin/{VIC_OVERNIGHT_BRANCH}."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Exercise the four git tools against BETTY_SITE_DIR.

    This self-test does NOT push to a real remote. It validates the
    schema/validator path on git_push (rejects extra args, accepts {}),
    runs git_status and git_diff against the real working tree, and runs
    a no-op commit (which is gracefully treated as a soft no-op).

    Cost: zero. No state mutation if working tree is clean.
    """
    print("Phase 4.6 git_ops tools self-test\n")
    print(f"  BETTY_SITE_DIR = {BETTY_SITE_DIR}\n")

    if not BETTY_SITE_DIR.exists() or not (BETTY_SITE_DIR / ".git").exists():
        print(f"  [skip] BETTY_SITE_DIR is not a git repository.")
        return

    # ---- git_status ----
    result = git_status({})
    assert result.status == "executed"
    print(f"  [ok] git_status: branch={result.payload['branch']!r}, "
          f"clean={result.payload['clean']}, "
          f"{len(result.payload['entries'])} entries")

    # ---- git_diff (unstaged, whole tree) ----
    result = git_diff({})
    assert result.status == "executed"
    print(f"  [ok] git_diff (unstaged): {len(result.payload['diff'])} chars, "
          f"truncated={result.payload['truncated']}")

    # ---- git_diff (staged) ----
    result = git_diff({"staged": True})
    assert result.status == "executed"
    print(f"  [ok] git_diff (staged): {len(result.payload['diff'])} chars")

    # ---- git_commit_all: no-op path (clean tree → soft no-op) ----
    # This only exercises the validation + branch-check + no-op path.
    # A real commit test would need to dirty the working tree, which we
    # don't want to do casually in the user's checkout.
    current_branch = _current_branch()
    if current_branch == "main":
        print(f"  [skip] Current branch is `main`; git_commit_all would "
              f"correctly refuse. Checkout a non-main branch to test "
              f"the commit path.")
    else:
        result = git_commit_all({"message": "selftest noop"})
        assert result.status == "executed"
        if result.payload["nothing_to_commit"]:
            print(f"  [ok] git_commit_all on clean tree → soft no-op as expected")
        else:
            print(f"  [warn] git_commit_all actually committed: "
                  f"{result.payload['commit_sha']}. "
                  f"Working tree was not clean.")

    # ---- Validator: git_status with extra args ----
    try:
        git_status({"branch": "main"})
    except ValueError as e:
        assert "takes no arguments" in str(e)
        print(f"  [ok] git_status rejected extra args")
    else:
        raise AssertionError("git_status should reject extra args")

    # ---- Validator: git_push with extra args ----
    try:
        git_push({"branch": "main"})
    except ValueError as e:
        assert "takes no arguments" in str(e)
        print(f"  [ok] git_push rejected branch override (hard-coded to "
              f"vic-overnight)")
    else:
        raise AssertionError("git_push should reject extra args")

    # ---- Validator: git_commit_all empty message ----
    try:
        git_commit_all({"message": ""})
    except ValueError as e:
        assert "non-empty" in str(e)
        print(f"  [ok] git_commit_all rejected empty message")
    else:
        raise AssertionError("git_commit_all should reject empty message")

    # ---- Hard Rule 3: git_commit_all refuses on main ----
    if _current_branch() == "main":
        try:
            git_commit_all({"message": "selftest on main should fail"})
        except ValueError as e:
            assert "main" in str(e).lower()
            print(f"  [ok] git_commit_all refused on main branch (Hard Rule 3)")
        else:
            raise AssertionError(
                "git_commit_all on main should fail per Hard Rule 3"
            )

    print("\ngit_ops.py self-test PASSED "
          "(no real commit or push; full chain test happens in dry-run)")


if __name__ == "__main__":
    _self_test()
