"""
Betty ETL PDF extractor.

Wraps pypdf to extract text and metadata from local PDFs. Returns a
structured PDFDocument with normalized text, page markers preserved
in the full-content blob, plus file-level metadata for source_documents.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pypdf


# ---------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------
@dataclass
class PDFDocument:
    """Result of extracting one PDF."""

    path: Path
    checksum_sha256: str
    page_count: int
    size_bytes: int
    mtime: datetime
    title: str
    pages: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        """All pages joined with page markers."""
        parts = []
        for i, page_text in enumerate(self.pages):
            parts.append(f"[page {i + 1}]")
            parts.append(page_text)
        return "\n\n".join(parts)

    @property
    def text_for_chunking(self) -> str:
        """All pages joined WITHOUT page markers, ready for chunking."""
        return "\n\n".join(self.pages)


# ---------------------------------------------------------------------
# Whitespace normalization
# ---------------------------------------------------------------------
_WHITESPACE_RUN = re.compile(r"[ \t]+")
_NEWLINE_RUN = re.compile(r"\n{3,}")
_HYPHEN_BREAK = re.compile(r"(\w+)-\n(\w+)")


def normalize_page_text(text: str) -> str:
    """Apply baseline whitespace cleanup to extracted PDF text."""
    # Fix hyphen-broken words at line ends: "exam-\nple" -> "example"
    text = _HYPHEN_BREAK.sub(r"\1\2", text)
    # Collapse runs of spaces/tabs to single space
    text = _WHITESPACE_RUN.sub(" ", text)
    # Collapse 3+ newlines to 2 (preserve paragraph breaks)
    text = _NEWLINE_RUN.sub("\n\n", text)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------
def compute_sha256(path: Path) -> str:
    """Stream-compute a file's SHA-256 hash."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------
# Main extractor
# ---------------------------------------------------------------------
def extract_pdf(path: Path) -> PDFDocument:
    """
    Extract one PDF into a PDFDocument.

    Raises FileNotFoundError if path doesn't exist, pypdf.errors.PdfError
    if the file isn't a valid PDF.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")

    stat = path.stat()
    checksum = compute_sha256(path)

    reader = pypdf.PdfReader(path)

    # Title: prefer PDF metadata title, fall back to filename stem
    pdf_meta = reader.metadata or {}
    title = (pdf_meta.get("/Title") or "").strip() or path.stem

    # Extract and normalize each page
    pages = []
    for page in reader.pages:
        raw = page.extract_text() or ""
        pages.append(normalize_page_text(raw))

    return PDFDocument(
        path=path,
        checksum_sha256=checksum,
        page_count=len(reader.pages),
        size_bytes=stat.st_size,
        mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        title=title,
        pages=pages,
    )


# ---------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------
def _self_test() -> None:
    """Extract the Stage 2 test PDF and print a summary."""
    from betty_etl.config import TEST_DATA_DIR

    test_pdf = TEST_DATA_DIR / "attention-is-all-you-need.pdf"
    print(f"Extracting: {test_pdf}")

    doc = extract_pdf(test_pdf)

    print(f"  Title:           {doc.title!r}")
    print(f"  Path:            {doc.path}")
    print(f"  Checksum:        {doc.checksum_sha256[:16]}... (truncated)")
    print(f"  Size:            {doc.size_bytes:,} bytes")
    print(f"  mtime:           {doc.mtime.isoformat()}")
    print(f"  Pages:           {doc.page_count}")
    print(f"  Total chars:     {len(doc.text_for_chunking):,}")
    print(f"  First 200 chars of page 1:")
    print(f"    {doc.pages[0][:200]!r}")
    print(f"  Last 200 chars of last page:")
    print(f"    {doc.pages[-1][-200:]!r}")
    print("PDF extractor self-test passed.")


if __name__ == "__main__":
    _self_test()
