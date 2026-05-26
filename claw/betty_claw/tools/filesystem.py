"""
Filesystem tools for Betty's Phase 4.6 actor.

Three atomic tools wrap the host filesystem under a fixed allow-list of
roots: read_file, list_directory, write_file. Per Phase 4.4 Q1 Decision A,
each tool declares one constant risk_class (the registry handles that),
and the bounds on what paths the actor can touch are STRUCTURAL — the
validators reject any path that doesn't resolve under an allowed root,
regardless of what Qwen emits.

Allowed roots (via env at module load):

  BETTY_SITE_DIR  — read + list + write. The Astro project working tree
                    (default: ~/Projects/emdash/travelpec-site). git_*
                    tools also operate on this directory.

  BETTY_DOCS_DIR  — read + list only (NOT writable from these tools).
                    Site docs/voice/research dossiers on Google Drive
                    (default: ~/My Drive/Betty/emdash-sites/travelpec.com-v3).

Both are resolved (symlinks followed) at module load. A path that doesn't
resolve under one of these roots raises ValueError at validate-time, before
any file I/O.

write_file uses the atomic_io.atomic_write pattern (tmpfile + fsync +
os.replace) so the Judge / a concurrent reader never observes a partial
write. read_file and list_directory have no side effects.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from betty_claw.atomic_io import atomic_write_json
from betty_claw.contracts import ToolResult


# ---------------------------------------------------------------------------
# Allow-list configuration
# ---------------------------------------------------------------------------

def _resolve_root(env_var: str, default: str) -> Path:
    """Resolve an allow-list root from env, expanding ~ and following symlinks.

    Symlink resolution at module load is deliberate — it locks the
    physical path the validator compares against. If a later symlink
    rewrite changed the target, the validator would still enforce the
    original root. To re-bind, restart the process.
    """
    raw = os.environ.get(env_var, default)
    return Path(os.path.expanduser(raw)).resolve()


BETTY_SITE_DIR: Path = _resolve_root(
    "BETTY_SITE_DIR",
    "~/Projects/emdash/travelpec-site",
)

BETTY_DOCS_DIR: Path = _resolve_root(
    "BETTY_DOCS_DIR",
    "~/My Drive/Betty/emdash-sites/travelpec.com-v3",
)

READ_ROOTS: tuple[Path, ...] = (BETTY_SITE_DIR, BETTY_DOCS_DIR)
WRITE_ROOTS: tuple[Path, ...] = (BETTY_SITE_DIR,)


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

def _validate_path_under(path_str: str, allowed_roots: Iterable[Path]) -> Path:
    """Resolve `path_str` and assert it lives under one of `allowed_roots`.

    Raises ValueError on:
      - non-string input
      - empty string
      - path that resolves outside every allowed root
        (path traversal via .. is naturally caught because resolve()
        normalizes the path before comparison)

    The path is NOT required to exist — that's a separate concern handled
    by the calling tool (read_file errors on missing file; write_file
    creates parent dirs).

    Returns the resolved absolute Path.
    """
    if not isinstance(path_str, str):
        raise ValueError(
            f"path must be str, got {type(path_str).__name__}"
        )
    if not path_str.strip():
        raise ValueError("path must be non-empty")

    candidate = Path(os.path.expanduser(path_str)).resolve()

    for root in allowed_roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue

    roots_repr = ", ".join(str(r) for r in allowed_roots)
    raise ValueError(
        f"Path {candidate} is not under any allowed root. "
        f"Allowed roots: [{roots_repr}]"
    )


# ---------------------------------------------------------------------------
# Tool: read_file
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read the contents of a text file under one of Betty's allowed "
            "roots (Astro project tree or site docs/research/voice). "
            "Returns the file content as a string. Does not modify any state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-prefixed path. Must resolve under "
                        "BETTY_SITE_DIR or BETTY_DOCS_DIR; otherwise rejected."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

# Soft cap on read size. Large files (>5 MB) are almost certainly not what
# Betty wants to dump into the actor context — the LLM context budget would
# get blown. Caller can override via internal API if needed; the schema
# doesn't expose a Qwen-controllable limit because that would be a footgun.
_READ_FILE_MAX_BYTES = 5 * 1024 * 1024


def _validate_read_file_args(args: dict) -> Path:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    if set(args.keys()) != {"path"}:
        raise ValueError(
            f"read_file expects exactly {{'path'}}; got {sorted(args.keys())}"
        )
    return _validate_path_under(args["path"], READ_ROOTS)


def read_file(args: dict) -> ToolResult:
    """Read a text file from the allow-listed roots. risk_class=read_only."""
    path = _validate_read_file_args(args)

    if not path.exists():
        raise ValueError(f"File does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"Path is not a regular file: {path}")

    size = path.stat().st_size
    if size > _READ_FILE_MAX_BYTES:
        raise ValueError(
            f"File too large: {size} bytes (cap {_READ_FILE_MAX_BYTES}). "
            f"Use list_directory + targeted reads instead."
        )

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"File is not valid UTF-8: {path} ({e}). "
            f"Betty's file tools handle text only."
        ) from e

    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="read_file",
        status="executed",
        payload={
            "path": str(path),
            "content": content,
            "size_bytes": size,
            "summary": (
                f"Read {size} bytes from {path.name} "
                f"({len(content.splitlines())} lines)."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Tool: list_directory
# ---------------------------------------------------------------------------

LIST_DIRECTORY_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": (
            "List the immediate children (files + subdirectories) of a "
            "directory under one of Betty's allowed roots. Does NOT recurse. "
            "Returns entries sorted alphabetically with a `kind` flag "
            "(`file` or `dir`) and size in bytes for files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-prefixed path. Must resolve under "
                        "BETTY_SITE_DIR or BETTY_DOCS_DIR; otherwise rejected."
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}


def _validate_list_directory_args(args: dict) -> Path:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    if set(args.keys()) != {"path"}:
        raise ValueError(
            f"list_directory expects exactly {{'path'}}; got {sorted(args.keys())}"
        )
    return _validate_path_under(args["path"], READ_ROOTS)


def list_directory(args: dict) -> ToolResult:
    """List immediate children of a directory. risk_class=read_only."""
    path = _validate_list_directory_args(args)

    if not path.exists():
        raise ValueError(f"Directory does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    entries = []
    for child in sorted(path.iterdir(), key=lambda p: p.name):
        if child.is_dir():
            entries.append({"name": child.name, "kind": "dir"})
        elif child.is_file():
            entries.append({
                "name": child.name,
                "kind": "file",
                "size_bytes": child.stat().st_size,
            })
        # Symlinks, sockets, etc. are skipped — Betty doesn't need to
        # reason about them and surfacing them could confuse Qwen.

    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="list_directory",
        status="executed",
        payload={
            "path": str(path),
            "entries": entries,
            "summary": (
                f"{len(entries)} entries in {path.name} "
                f"({sum(1 for e in entries if e['kind'] == 'dir')} dirs, "
                f"{sum(1 for e in entries if e['kind'] == 'file')} files)."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Tool: write_file
# ---------------------------------------------------------------------------

WRITE_FILE_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Atomically write text content to a file under BETTY_SITE_DIR. "
            "Creates parent directories if needed. Overwrites existing "
            "files; does NOT append. The write is atomic (tmpfile + fsync + "
            "os.replace) so a concurrent reader cannot observe a partial "
            "write. Use for Astro source, layouts, components, and content "
            "collection files. NOT for arbitrary paths outside the site dir."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute or ~-prefixed path. Must resolve under "
                        "BETTY_SITE_DIR; reads of docs/research go through "
                        "read_file, not write_file."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full file content (UTF-8 text).",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}


def _validate_write_file_args(args: dict) -> tuple[Path, str]:
    if not isinstance(args, dict):
        raise ValueError(f"args must be dict, got {type(args).__name__}")
    if set(args.keys()) != {"path", "content"}:
        raise ValueError(
            f"write_file expects exactly {{'path','content'}}; "
            f"got {sorted(args.keys())}"
        )
    path = _validate_path_under(args["path"], WRITE_ROOTS)
    content = args["content"]
    if not isinstance(content, str):
        raise ValueError(
            f"content must be str, got {type(content).__name__}"
        )
    return path, content


def write_file(args: dict) -> ToolResult:
    """Atomically write text content to a file. risk_class=reversible_write."""
    path, content = _validate_write_file_args(args)

    # Ensure parent directories exist. parents=True for nested new dirs;
    # exist_ok=True so we don't error if the parent already exists.
    path.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via the shared utility. atomic_write_json writes JSON,
    # but we need raw text. Use the underlying atomic-rename pattern by
    # writing the text through a tmpfile + os.replace.
    tmp_path = path.with_suffix(path.suffix + ".tmp." + uuid.uuid4().hex[:8])
    try:
        tmp_path.write_text(content, encoding="utf-8")
        # fsync the tmpfile so its data is on disk before the rename.
        with open(tmp_path, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup if the rename failed mid-flight.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    size = path.stat().st_size
    return ToolResult(
        call_id=str(uuid.uuid4()),
        tool_name="write_file",
        status="executed",
        payload={
            "path": str(path),
            "bytes_written": size,
            "summary": (
                f"Wrote {size} bytes to {path.relative_to(BETTY_SITE_DIR)} "
                f"({len(content.splitlines())} lines)."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Exercise the three filesystem tools against a temp scratch tree.

    Creates a temporary dir under BETTY_SITE_DIR (so the write-root check
    passes), runs read/list/write through their happy paths and failure
    modes, asserts the contract holds, and cleans up.

    No external dependencies; cost zero.
    """
    import shutil

    print("Phase 4.6 filesystem tools self-test\n")
    print(f"  BETTY_SITE_DIR = {BETTY_SITE_DIR}")
    print(f"  BETTY_DOCS_DIR = {BETTY_DOCS_DIR}\n")

    # Scratch dir under BETTY_SITE_DIR. If BETTY_SITE_DIR doesn't exist
    # on this machine (e.g., a CI run without travelpec-site cloned),
    # the self-test exits cleanly with a skip message.
    if not BETTY_SITE_DIR.exists():
        print(f"  [skip] BETTY_SITE_DIR does not exist: {BETTY_SITE_DIR}")
        print(f"  Set BETTY_SITE_DIR env var or create the path to run "
              f"the full self-test.")
        return

    scratch = BETTY_SITE_DIR / ".betty-fs-selftest"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir()

    try:
        # ---- write_file happy path ----
        target = scratch / "subdir" / "hello.txt"
        result = write_file({
            "path": str(target),
            "content": "hello betty\nline two\n",
        })
        assert result.status == "executed"
        assert result.tool_name == "write_file"
        assert Path(result.payload["path"]) == target
        assert target.read_text() == "hello betty\nline two\n"
        print(f"  [ok] write_file wrote {result.payload['bytes_written']} bytes")

        # ---- read_file happy path ----
        result = read_file({"path": str(target)})
        assert result.status == "executed"
        assert result.payload["content"] == "hello betty\nline two\n"
        assert result.payload["size_bytes"] == len("hello betty\nline two\n")
        print(f"  [ok] read_file returned {result.payload['size_bytes']} bytes")

        # ---- list_directory happy path ----
        result = list_directory({"path": str(scratch / "subdir")})
        assert result.status == "executed"
        names = [e["name"] for e in result.payload["entries"]]
        assert "hello.txt" in names
        print(f"  [ok] list_directory found {len(names)} entries")

        # ---- path traversal blocked ----
        try:
            read_file({"path": "/etc/passwd"})
        except ValueError as e:
            assert "not under any allowed root" in str(e)
            print(f"  [ok] read_file rejected /etc/passwd")
        else:
            raise AssertionError("read_file should have rejected /etc/passwd")

        # ---- write_file rejects paths in BETTY_DOCS_DIR ----
        if BETTY_DOCS_DIR.exists():
            try:
                write_file({
                    "path": str(BETTY_DOCS_DIR / "should-not-write.txt"),
                    "content": "should not happen",
                })
            except ValueError as e:
                assert "not under any allowed root" in str(e)
                print(f"  [ok] write_file rejected BETTY_DOCS_DIR target")
            else:
                raise AssertionError(
                    "write_file should have rejected BETTY_DOCS_DIR target "
                    "— only BETTY_SITE_DIR is writable"
                )

        # ---- read_file rejects missing file ----
        try:
            read_file({"path": str(scratch / "does-not-exist.txt")})
        except ValueError as e:
            assert "does not exist" in str(e)
            print(f"  [ok] read_file rejected missing file")
        else:
            raise AssertionError("read_file should reject missing file")

        # ---- write_file rejects non-string content ----
        try:
            write_file({"path": str(target), "content": 12345})
        except ValueError as e:
            assert "content must be str" in str(e)
            print(f"  [ok] write_file rejected non-string content")
        else:
            raise AssertionError("write_file should reject non-string content")

        # ---- write_file rejects extra keys ----
        try:
            write_file({
                "path": str(target),
                "content": "x",
                "encoding": "utf-8",
            })
        except ValueError as e:
            assert "expects exactly" in str(e)
            print(f"  [ok] write_file rejected extra keys (strict schema)")
        else:
            raise AssertionError("write_file should reject extra keys")

        print("\nfilesystem.py self-test PASSED")

    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    _self_test()
