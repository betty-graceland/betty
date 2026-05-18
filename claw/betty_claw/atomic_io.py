"""Atomic JSON file writes.

Phase 4.3 introduces this module as a shared utility because spend_ledger.py
is the second site in the codebase needing atomic writes. The first site,
claw/betty_claw/tools/draft_email.py, has its own inline implementation
from Phase 4.2 and will be migrated to use this module in a follow-up.

The pattern is the standard POSIX-safe atomic replace:
  1. Write JSON to a temp file in the same directory as the target.
  2. fsync the temp file's data to disk.
  3. os.replace temp -> target. On POSIX, this is atomic: readers see
     either the old file or the new file, never a partial write.

Same-directory tmpfile matters because os.replace across filesystems is
not guaranteed atomic. The temp file must live on the same filesystem
as the destination.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, data: Any) -> None:
    """Write `data` as JSON to `path` atomically.

    Raises:
        OSError: if the parent directory does not exist or is not writable.
        TypeError: if `data` is not JSON-serializable.
    """
    path = Path(path)
    parent = path.parent

    if not parent.is_dir():
        raise OSError(f"Parent directory does not exist: {parent}")

    fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=parent,
    )
    tmp_path = Path(tmp_path_str)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup of the temp file if anything went wrong
        # before the replace, including KeyboardInterrupt and SystemExit.
        # After a successful replace, tmp_path no longer exists, so
        # unlink would raise FileNotFoundError.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


if __name__ == "__main__":
    import shutil
    import sys

    test_dir = Path("/tmp/betty_atomic_io_selftest")
    if test_dir.exists():
        shutil.rmtree(test_dir)
    test_dir.mkdir()

    target = test_dir / "test.json"

    # 1. Basic write.
    atomic_write_json(target, {"hello": "world", "n": 42})
    assert target.exists(), "target file not created"
    loaded = json.loads(target.read_text())
    assert loaded == {"hello": "world", "n": 42}, f"round-trip mismatch: {loaded}"
    print("  [ok] basic write round-trips")

    # 2. Overwrite leaves no temp files behind.
    atomic_write_json(target, {"hello": "again"})
    leftover = [p for p in test_dir.iterdir() if p.name.startswith(".test.json.")]
    assert not leftover, f"temp files left behind: {leftover}"
    print("  [ok] no temp files left after successful write")

    # 3. Missing parent directory raises OSError.
    bad_path = test_dir / "nonexistent_subdir" / "file.json"
    try:
        atomic_write_json(bad_path, {"x": 1})
    except OSError as e:
        print(f"  [ok] missing parent raises OSError: {e}")
    else:
        print("  [FAIL] missing parent did not raise")
        sys.exit(1)

    # 4. Non-serializable data raises TypeError and leaves no temp file.
    class NotJsonable:
        pass

    try:
        atomic_write_json(target, {"bad": NotJsonable()})
    except TypeError:
        leftover = [p for p in test_dir.iterdir() if p.name.startswith(".test.json.")]
        assert not leftover, f"temp files left after failed serialization: {leftover}"
        print("  [ok] non-serializable raises TypeError, cleans up temp file")
    else:
        print("  [FAIL] non-serializable did not raise")
        sys.exit(1)

    # 5. Original file survives a failed write attempt.
    loaded = json.loads(target.read_text())
    assert loaded == {"hello": "again"}, f"original file corrupted: {loaded}"
    print("  [ok] original file intact after failed write")

    shutil.rmtree(test_dir)
    print("\natomic_io.py self-test PASSED")
