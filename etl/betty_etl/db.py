"""
Betty ETL database layer.

Owns the psycopg3 connection pool and the write-path for ingested
documents. Receives EmbeddedChunk objects from the pipeline and
persists them transactionally into source_documents +
source_document_chunks.

Idempotency is enforced at the schema level via the UNIQUE constraint
on source_documents.checksum_sha256. Re-ingesting an identical file
raises DuplicateDocumentError.

This module does NOT do retrieval. Vector search lives in
retrieval.py (Stage 3). db.py is strictly write-path for Stage 2.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from pgvector.psycopg import register_vector

from betty_etl.config import DB
from betty_etl.embeddings import EmbeddedChunk


# ---------- Errors ----------

class BettyDBError(Exception):
    """Base class for db.py errors."""


class DuplicateDocumentError(BettyDBError):
    """Raised when a document with this checksum_sha256 already exists."""

    def __init__(self, checksum: str, existing_id: UUID):
        self.checksum = checksum
        self.existing_id = existing_id
        super().__init__(
            f"Document with checksum {checksum[:12]}... already ingested "
            f"as {existing_id}"
        )


# ---------- Inputs ----------

@dataclass
class SourceDocumentInput:
    """All the fields needed to insert a row into source_documents."""

    workspace_id: str
    source_kind: str  # must match memory_source_kind enum
    checksum_sha256: str
    project_id: str | None = None
    title: str | None = None
    uri: str | None = None
    mime_type: str | None = None
    source_timestamp: Any = None
    summary: str | None = None
    content: str | None = None
    metadata: dict[str, Any] | None = None
    created_by: str = "system"


# ---------- Pool ----------

_pool: ConnectionPool | None = None


def _configure_connection(conn: psycopg.Connection) -> None:
    """Per-connection setup: register pgvector adapter once."""
    register_vector(conn)


def get_pool() -> ConnectionPool:
    """Lazy-init the connection pool. Singleton for the process."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=DB.dsn,
            min_size=DB.pool_min_size,
            max_size=DB.pool_max_size,
            timeout=DB.pool_timeout,
            kwargs={"row_factory": dict_row},
            configure=_configure_connection,
            open=True,
        )
        _pool.wait()
        print(f"DB pool ready: {DB.pool_min_size}-{DB.pool_max_size} conns @ {DB.host}:{DB.port}")
    return _pool


def close_pool() -> None:
    """Close the pool. Call on process shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """Yield a connection from the pool.

    psycopg3's pool.connection() context manager commits on clean
    exit and rolls back on exception.
    """
    pool = get_pool()
    with pool.connection() as conn:
        yield conn


# ---------- Hashing ----------

def compute_checksum(data: bytes) -> str:
    """SHA-256 hex digest. Use this on file bytes for idempotency keys."""
    return hashlib.sha256(data).hexdigest()


# ---------- Lookups ----------

def find_document_by_checksum(
    conn: psycopg.Connection,
    checksum_sha256: str,
) -> UUID | None:
    """Return the id of an existing document with this checksum, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM source_documents WHERE checksum_sha256 = %s",
            (checksum_sha256,),
        )
        row = cur.fetchone()
        return row["id"] if row else None


# ---------- Writes ----------

_INSERT_DOCUMENT_SQL = """
INSERT INTO source_documents (
    workspace_id, project_id, source_kind, title, uri,
    checksum_sha256, mime_type, source_timestamp, summary,
    content, metadata, created_by
)
VALUES (
    %(workspace_id)s, %(project_id)s, %(source_kind)s, %(title)s, %(uri)s,
    %(checksum_sha256)s, %(mime_type)s, %(source_timestamp)s, %(summary)s,
    %(content)s, %(metadata)s, %(created_by)s
)
RETURNING id
"""

_INSERT_CHUNK_SQL = """
INSERT INTO source_document_chunks (
    source_document_id, chunk_index, content,
    embedding, token_count, metadata
)
VALUES (%s, %s, %s, %s, %s, %s)
"""


def insert_source_document(
    conn: psycopg.Connection,
    doc: SourceDocumentInput,
) -> UUID:
    """Insert one row into source_documents and return its id."""
    params = {
        "workspace_id": doc.workspace_id,
        "project_id": doc.project_id,
        "source_kind": doc.source_kind,
        "title": doc.title,
        "uri": doc.uri,
        "checksum_sha256": doc.checksum_sha256,
        "mime_type": doc.mime_type,
        "source_timestamp": doc.source_timestamp,
        "summary": doc.summary,
        "content": doc.content,
        "metadata": Jsonb(doc.metadata or {}),
        "created_by": doc.created_by,
    }

    try:
        with conn.cursor() as cur:
            cur.execute(_INSERT_DOCUMENT_SQL, params)
            row = cur.fetchone()
            return row["id"]
    except psycopg.errors.UniqueViolation as e:
        conn.rollback()
        existing_id = find_document_by_checksum(conn, doc.checksum_sha256)
        if existing_id is None:
            raise BettyDBError(
                f"UniqueViolation on checksum but row not found: {e}"
            ) from e
        raise DuplicateDocumentError(doc.checksum_sha256, existing_id) from e


def insert_chunks(
    conn: psycopg.Connection,
    source_document_id: UUID,
    embedded_chunks: list[EmbeddedChunk],
) -> int:
    """Batch-insert embedded chunks for a document. Returns count inserted."""
    if not embedded_chunks:
        return 0

    rows = [
        (
            source_document_id,
            ec.chunk.index,
            ec.chunk.content,
            ec.embedding,
            ec.token_count,
            Jsonb({
                "start_char": ec.chunk.start_char,
                "end_char": ec.chunk.end_char,
                "char_length": ec.chunk.char_length,
                "model_name": ec.model_name,
                "embedding_dim": ec.embedding_dim,
            }),
        )
        for ec in embedded_chunks
    ]

    with conn.cursor() as cur:
        cur.executemany(_INSERT_CHUNK_SQL, rows)
        return len(rows)


def ingest_document(
    doc: SourceDocumentInput,
    embedded_chunks: list[EmbeddedChunk],
) -> tuple[UUID, int]:
    """Atomically insert a document + all its chunks."""
    with get_conn() as conn:
        doc_id = insert_source_document(conn, doc)
        chunk_count = insert_chunks(conn, doc_id, embedded_chunks)
        return doc_id, chunk_count


# ---------- Health check ----------

def ping() -> dict[str, Any]:
    """Verify the pool is alive and pgvector is registered."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version() AS pg_version")
            pg = cur.fetchone()
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vec = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM source_documents")
            docs = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM source_document_chunks")
            chunks = cur.fetchone()
    return {
        "pg_version": pg["pg_version"].split(",")[0],
        "pgvector_version": vec["extversion"] if vec else None,
        "source_documents_count": docs["n"],
        "source_document_chunks_count": chunks["n"],
    }


# ---------- Self-test ----------

def _self_test() -> None:
    """Full pipeline smoke test: extract → chunk → embed → ingest → verify."""
    from datetime import datetime, timezone
    from betty_etl.chunking import chunk_text
    from betty_etl.config import TEST_DATA_DIR
    from betty_etl.embeddings import embed_chunks
    from betty_etl.extractors.pdf import extract_pdf

    test_pdf = TEST_DATA_DIR / "attention-is-all-you-need.pdf"
    print(f"End-to-end ingest: {test_pdf}")

    health = ping()
    print(f"  DB health: {health}")

    pdf_bytes = test_pdf.read_bytes()
    checksum = compute_checksum(pdf_bytes)
    print(f"  Checksum: {checksum[:16]}...")

    doc = extract_pdf(test_pdf)
    chunks = chunk_text(doc.text_for_chunking)
    embedded = embed_chunks(chunks)
    print(f"  Chunks embedded: {len(embedded)}")

    doc_input = SourceDocumentInput(
        workspace_id="betty-dev",
        source_kind="doc",
        checksum_sha256=checksum,
        title="Attention Is All You Need",
        uri=str(test_pdf),
        mime_type="application/pdf",
        source_timestamp=datetime.now(timezone.utc),
        content=doc.text_for_chunking,
        metadata={"test_run": True, "stage": 2},
    )

    try:
        doc_id, chunk_count = ingest_document(doc_input, embedded)
        print(f"  Inserted document {doc_id}")
        print(f"  Inserted {chunk_count} chunks")
    except DuplicateDocumentError as e:
        print(f"  Already ingested: {e.existing_id} (idempotency working)")

    health_after = ping()
    print(f"  DB after: {health_after}")
    print("  Self-test complete.")

    close_pool()


if __name__ == "__main__":
    _self_test()
