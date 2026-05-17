"""
Betty ETL orchestrator.

Single entry point: ingest_file(path) -> IngestResult.

Wraps extract → chunk → embed → ingest into one callable with
structured outcomes (no print-and-pray, no exceptions for normal
duplicate cases). Designed for three callers:

  1. CLI: `python -m betty_etl.pipeline <path>` for ad-hoc human runs
     (skip_duplicates=False so the human sees "already ingested" loud)
  2. Watcher (Stage 5): filesystem watcher calls ingest_file() on every
     new file; skip_duplicates=True so re-seeing the same file is a
     no-op, not a crash
  3. MCP tool (Stage 6+): exposed to Betty's agent layer; agent gets
     a structured IngestResult to reason about

The pipeline is mime-aware: it dispatches to the right extractor based
on file extension. Only PDFs are wired in Stage 2; emails (.eml/.emlx),
markdown, and plain text land in later stages.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from betty_etl.chunking import chunk_text
from betty_etl.db import (
    DuplicateDocumentError,
    SourceDocumentInput,
    close_pool,
    compute_checksum,
    ingest_document,
)
from betty_etl.embeddings import embed_chunks
from betty_etl.extractors.pdf import extract_pdf


# ---------- Result type ----------

IngestStatus = Literal["inserted", "skipped_duplicate", "failed"]


@dataclass
class IngestResult:
    """Structured outcome of a single ingest_file() call."""

    status: IngestStatus
    path: Path
    document_id: UUID | None = None
    chunk_count: int = 0
    duration_seconds: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        if self.status == "inserted":
            return (
                f"✓ {self.path.name}: inserted {self.chunk_count} chunks "
                f"as {self.document_id} in {self.duration_seconds:.2f}s"
            )
        elif self.status == "skipped_duplicate":
            return (
                f"⊘ {self.path.name}: already ingested as {self.document_id}"
            )
        else:
            return f"✗ {self.path.name}: {self.error}"


# ---------- Extractor dispatch ----------

def _extract(path: Path) -> tuple[str, str, dict[str, Any]]:
    """Dispatch to the right extractor based on file extension.

    Returns (full_text, mime_type, extractor_metadata).
    extractor_metadata is merged into the source_documents.metadata jsonb.
    """
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        doc = extract_pdf(path)
        return (
            doc.text_for_chunking,
            "application/pdf",
            {
                "page_count": doc.page_count,
                "pdf_title": doc.title,
                "size_bytes": doc.size_bytes,
            },
        )

    # Future extractors land here:
    # if suffix in (".eml", ".emlx"): return extract_email(path)
    # if suffix in (".md", ".markdown"): return extract_markdown(path)
    # if suffix == ".txt": return extract_text(path)

    raise ValueError(
        f"No extractor registered for extension {suffix!r}. "
        f"Supported: .pdf"
    )


# ---------- Main entry point ----------

def ingest_file(
    path: Path | str,
    *,
    workspace_id: str = "betty-dev",
    project_id: str | None = None,
    source_kind: str = "doc",
    title: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    skip_duplicates: bool = True,
) -> IngestResult:
    """Extract, chunk, embed, and ingest a single file.

    Args:
        path: Filesystem path to the file.
        workspace_id: Logical tenant boundary. Stage 2 default is
            "betty-dev". Production callers should pass their client
            namespace (e.g. "kpixies", "playce-studio").
        project_id: Optional sub-namespace within a workspace.
        source_kind: Maps to source_documents.source_kind. Common
            values: "doc", "email", "note", "file".
        title: Display name. If None, derived from filename stem.
        extra_metadata: Arbitrary jsonb metadata to merge into
            source_documents.metadata. Caller context — anything
            useful for retrieval or audit.
        skip_duplicates: If True (default), checksum collisions
            return status="skipped_duplicate" cleanly. If False,
            duplicates produce status="failed" with a duplicate
            error — useful for CLI where a human wants to know.

    Returns:
        IngestResult with status, document_id (if known), chunk_count,
        and duration.
    """
    path = Path(path).expanduser().resolve()
    start = time.monotonic()

    if not path.exists():
        return IngestResult(
            status="failed",
            path=path,
            duration_seconds=time.monotonic() - start,
            error=f"File not found: {path}",
        )

    try:
        # 1. Read bytes + compute checksum (idempotency key)
        data = path.read_bytes()
        checksum = compute_checksum(data)

        # 2. Extract text + mime + extractor metadata
        full_text, mime_type, extractor_meta = _extract(path)

        # 3. Chunk
        chunks = chunk_text(full_text)
        if not chunks:
            return IngestResult(
                status="failed",
                path=path,
                duration_seconds=time.monotonic() - start,
                error="Extraction produced no chunkable text",
            )

        # 4. Embed
        embedded = embed_chunks(chunks)

        # 5. Build SourceDocumentInput
        merged_meta = {
            **extractor_meta,
            **(extra_metadata or {}),
        }
        doc_input = SourceDocumentInput(
            workspace_id=workspace_id,
            project_id=project_id,
            source_kind=source_kind,
            checksum_sha256=checksum,
            title=title or path.stem,
            uri=str(path),
            mime_type=mime_type,
            content=full_text,
            metadata=merged_meta,
        )

        # 6. Ingest transactionally
        try:
            doc_id, chunk_count = ingest_document(doc_input, embedded)
            return IngestResult(
                status="inserted",
                path=path,
                document_id=doc_id,
                chunk_count=chunk_count,
                duration_seconds=time.monotonic() - start,
                metadata={"mime_type": mime_type, **extractor_meta},
            )
        except DuplicateDocumentError as e:
            if skip_duplicates:
                return IngestResult(
                    status="skipped_duplicate",
                    path=path,
                    document_id=e.existing_id,
                    duration_seconds=time.monotonic() - start,
                )
            return IngestResult(
                status="failed",
                path=path,
                document_id=e.existing_id,
                duration_seconds=time.monotonic() - start,
                error=f"Already ingested as {e.existing_id}",
            )

    except Exception as e:
        return IngestResult(
            status="failed",
            path=path,
            duration_seconds=time.monotonic() - start,
            error=f"{type(e).__name__}: {e}",
        )


# ---------- CLI entry point ----------

def _cli() -> int:
    """Human-facing CLI. Returns exit code.

    Usage:
        python -m betty_etl.pipeline <path> [--force] [--workspace ID]
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="betty-ingest",
        description="Ingest a file into Betty's OpenBrain.",
    )
    parser.add_argument("path", type=Path, help="File to ingest")
    parser.add_argument(
        "--workspace",
        default="betty-dev",
        help="workspace_id (default: betty-dev)",
    )
    parser.add_argument(
        "--project",
        default=None,
        help="project_id (optional)",
    )
    parser.add_argument(
        "--kind",
        default="doc",
        help="source_kind (default: doc)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Treat duplicates as errors (default: silently skip)",
    )
    args = parser.parse_args()

    result = ingest_file(
        args.path,
        workspace_id=args.workspace,
        project_id=args.project,
        source_kind=args.kind,
        skip_duplicates=not args.force,
    )
    print(result)
    close_pool()
    return 0 if result.status in ("inserted", "skipped_duplicate") else 1


# ---------- Self-test ----------

def _self_test() -> None:
    """Run the full pipeline against the Stage 2 test PDF."""
    from betty_etl.config import TEST_DATA_DIR

    test_pdf = TEST_DATA_DIR / "attention-is-all-you-need.pdf"
    print(f"Pipeline self-test: {test_pdf}")

    # First run: either inserts (clean DB) or skips (already there)
    result = ingest_file(
        test_pdf,
        workspace_id="betty-dev",
        source_kind="doc",
        title="Attention Is All You Need",
        extra_metadata={"test_run": True, "stage": "2-pipeline"},
    )
    print(f"  Run 1: {result}")
    assert result.status in ("inserted", "skipped_duplicate"), (
        f"Unexpected status: {result.status}"
    )

    # Second run: must skip cleanly
    result2 = ingest_file(
        test_pdf,
        workspace_id="betty-dev",
        source_kind="doc",
    )
    print(f"  Run 2: {result2}")
    assert result2.status == "skipped_duplicate", (
        f"Second run should skip, got {result2.status}"
    )

    # --force semantics: same call but skip_duplicates=False
    result3 = ingest_file(
        test_pdf,
        workspace_id="betty-dev",
        skip_duplicates=False,
    )
    print(f"  Run 3 (--force): {result3}")
    assert result3.status == "failed", (
        f"Forced duplicate should fail, got {result3.status}"
    )

    # Failure path: nonexistent file
    result4 = ingest_file("/tmp/does-not-exist-betty.pdf")
    print(f"  Run 4 (missing file): {result4}")
    assert result4.status == "failed"

    print("  Pipeline self-test complete.")
    close_pool()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        _self_test()
    else:
        sys.exit(_cli())
