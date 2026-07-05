"""
betty_handoff_report.py — Betty worker→judge handoff writer (Contract v1).

Deterministic executor code. No literal IDs/paths/tokens are ever chosen by a
model; this module just serializes what the pipeline already produced. It writes
a contract-compliant JSON run report + a markdown render, then publishes a small
ready-file LAST, into the shared handoff directory (a Google Drive for Desktop
mounted folder on Betty's Mac). The judge (Claude) reads these via the Drive API,
verifies the report sha256 against the ready-file, and only then judges.

Integration: call write_run_report(...) at the end of process_pending_stays.py,
after the batch completes. Build one item per dossier with build_item().

Stdlib only — no third-party deps.
"""

from __future__ import annotations
import hashlib
import json
import os
import tempfile
import datetime
from pathlib import Path

SCHEMA_VERSION = "1.0"


# ---------- hashing / time ----------

def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def input_set_hash(sources: list[tuple[str, str]]) -> str:
    """Deterministic hash of the frozen input set, for the replay gate.
    sources: list of (source_filename, source_text). Order-independent."""
    parts = sorted(f"{name}:{sha256_text(text)}" for name, text in sources)
    return sha256_text("\n".join(parts))


# ---------- builders ----------

def build_item(*, source_filename: str, source_text: str, draft_id: str, title: str,
               draft_body: str, validator_result: dict, editorial_score: float | None = None,
               draft_url: str | None = None, error_code: str | None = None,
               error_message: str | None = None) -> dict:
    """One contract-compliant item. Evidence is the embedded draft_body, hashed.
    validator_result and editorial_score go in worker_assertions — NOT judge inputs."""
    item = {
        "source_filename": source_filename,
        "source_hash": sha256_text(source_text),
        "draft_id": draft_id,
        "title": title,
        "evidence_mode": "embedded",
        "draft_body": draft_body,
        "draft_snapshot_ref": None,
        "draft_hash": sha256_text(draft_body),
        "worker_assertions": {"validator_result": validator_result},
        "error_code": error_code,
        "error_message": error_message,
    }
    if draft_url is not None:
        item["draft_url"] = draft_url
    if editorial_score is not None:
        item["worker_assertions"]["editorial_score"] = editorial_score
    return item


def build_report(*, run_id: str, attempt: int, pipeline_version: str, site: str,
                 input_set_hash: str, started_at: str, completed_at: str,
                 batch_limit: int, items: list[dict], status: str = "complete") -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "schema_compat": "backward-compatible",
        "run_id": run_id,
        "attempt": attempt,
        "pipeline_version": pipeline_version,
        "site": site,
        "input_set_hash": input_set_hash,
        "started_at": started_at,
        "completed_at": completed_at,
        "status": status,
        "batch_limit": batch_limit,
        "items": items,
    }


# ---------- validation (mirrors Contract v1; raises ValueError) ----------

_REQUIRED_TOP = {"schema_version", "run_id", "attempt", "pipeline_version", "site",
                 "input_set_hash", "started_at", "completed_at", "status",
                 "batch_limit", "items"}
_REQUIRED_ITEM = {"source_filename", "source_hash", "draft_id", "title",
                  "evidence_mode", "draft_hash", "worker_assertions"}


def validate_report(report: dict) -> None:
    missing = _REQUIRED_TOP - report.keys()
    if missing:
        raise ValueError(f"report missing required fields: {sorted(missing)}")
    if report["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {report['schema_version']!r}")
    if not isinstance(report["items"], list) or not report["items"]:
        raise ValueError("items must be a non-empty list")
    seen: set[str] = set()
    for i, item in enumerate(report["items"]):
        m = _REQUIRED_ITEM - item.keys()
        if m:
            raise ValueError(f"item {i} missing required fields: {sorted(m)}")
        if "validator_result" not in item["worker_assertions"]:
            raise ValueError(f"item {i} worker_assertions missing validator_result")
        did = item["draft_id"]
        if did in seen:
            raise ValueError(f"duplicate draft_id: {did}")
        seen.add(did)
        mode = item["evidence_mode"]
        if mode == "embedded":
            if item.get("draft_body") is None:
                raise ValueError(f"item {i} embedded but missing draft_body")
            if item["draft_hash"] != sha256_text(item["draft_body"]):
                raise ValueError(f"item {i} draft_hash does not match draft_body")
        elif mode == "snapshot":
            ref = item.get("draft_snapshot_ref")
            if not ref or "sha256" not in ref:
                raise ValueError(f"item {i} snapshot but missing snapshot_ref.sha256")
        else:
            raise ValueError(f"item {i} invalid evidence_mode: {mode!r}")


# ---------- atomic write + ready-file ----------

def _atomic_write(directory: Path, name: str, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, directory / name)  # atomic on a local filesystem
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def write_run_report(report: dict, handoff_dir: str) -> dict:
    """Validate -> write report + markdown -> publish ready-file LAST.
    handoff_dir is the local Drive-synced folder on Betty's Mac.
    Returns the ready-file dict. Raises ValueError if the report is non-compliant."""
    validate_report(report)
    d = Path(handoff_dir)
    d.mkdir(parents=True, exist_ok=True)

    base = f"run-{report['run_id']}-{report['attempt']}"
    report_name = f"{base}.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    report_sha = _sha256_bytes(payload)

    _atomic_write(d, report_name, payload)
    _atomic_write(d, f"{base}.md", render_markdown(report).encode("utf-8"))

    ready = {
        "schema_version": SCHEMA_VERSION,
        "run_id": report["run_id"],
        "attempt": report["attempt"],
        "report_filename": report_name,
        "report_sha256": report_sha,
        "record_count": len(report["items"]),
        "created_at": utc_now(),
    }
    ready_bytes = json.dumps(ready, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    _atomic_write(d, f"{base}.ready.json", ready_bytes)  # published LAST
    return ready


def render_markdown(report: dict) -> str:
    lines = [
        f"# Betty run report — {report['run_id']} (attempt {report['attempt']})",
        "",
        f"- site: {report['site']}",
        f"- pipeline_version: {report['pipeline_version']}",
        f"- completed_at: {report['completed_at']}",
        f"- items: {len(report['items'])}",
        "",
    ]
    for it in report["items"]:
        wa = it.get("worker_assertions", {})
        vr = wa.get("validator_result", {})
        lines += [
            f"## {it['title']}",
            f"- source: {it['source_filename']}",
            f"- draft_id: {it['draft_id']}",
            f"- worker validator pass: {vr.get('pass')}",
            f"- worker editorial_score (telemetry only): {wa.get('editorial_score')}",
            "",
        ]
    return "\n".join(lines)
