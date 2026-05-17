"""
Betty ETL retrieval layer.

The read-path counterpart to db.py / pipeline.py. Embeds a query
string with Nomic's search_query prefix, runs a cosine-distance
search against source_document_chunks via pgvector's <=> operator
(matching the vector_cosine_ops HNSW index in 001_init.sql), and
returns hydrated RetrievalHit objects with chunk content, parent
document metadata, and similarity scores.

Workspace/project filters are pushed down into the SQL WHERE clause
so the HNSW index lookup happens within the tenant boundary, not
after. This matters for multi-tenant correctness once Betty handles
multiple clients (kpixies, playce-studio, etc.) — never leak
cross-tenant results into the top-K.

This module does NOT mutate state. It is pure read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from psycopg import sql

from betty_etl.db import get_conn
from betty_etl.embeddings import embed_query


# ---------- Result type ----------

@dataclass
class RetrievalHit:
    """A single retrieved chunk with parent document context.

    Similarity is the cosine similarity (1.0 = identical, 0.0 =
    orthogonal). We compute it as `1 - distance` from pgvector's
    <=> operator, which returns cosine distance.

    start_char and end_char are extracted from the chunk's metadata
    jsonb (they are NOT top-level columns in the live schema).
    """

    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    content: str
    similarity: float

    # Parent document context
    document_title: str | None
    document_uri: str
    source_kind: str
    workspace_id: str

    # Char offsets from metadata jsonb (may be None if chunk was
    # written by a producer that didn't populate them)
    start_char: int | None
    end_char: int | None

    # Full chunk metadata for callers that want it
    chunk_metadata: dict[str, Any] = field(default_factory=dict)
    document_metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        title = self.document_title or self.document_uri
        preview = self.content[:80].replace("\n", " ")
        return (
            f"[{self.similarity:.3f}] {title} "
            f"(chunk {self.chunk_index}): {preview}..."
        )


# ---------- Core retrieve function ----------

# Pre-built SQL with named placeholders. Workspace and project filters
# are added dynamically because psycopg3 doesn't compile "WHERE TRUE
# AND col = NULL" the way we'd want — we build the WHERE clause to
# match exactly what the planner can push into the HNSW lookup.

_BASE_SELECT = """
SELECT
    c.id              AS chunk_id,
    c.source_document_id AS document_id,
    c.chunk_index,
    c.content,
    c.metadata        AS chunk_metadata,
    1 - (c.embedding <=> %(query_vec)s::vector) AS similarity,
    d.title           AS document_title,
    d.uri             AS document_uri,
    d.source_kind,
    d.workspace_id,
    d.metadata        AS document_metadata
FROM source_document_chunks c
JOIN source_documents d ON d.id = c.source_document_id
WHERE c.embedding IS NOT NULL
"""

_ORDER_AND_LIMIT = """
ORDER BY c.embedding <=> %(query_vec)s::vector
LIMIT %(limit)s
"""


def retrieve(
    query: str,
    *,
    workspace_id: str | None = None,
    project_id: str | None = None,
    source_kind: str | None = None,
    limit: int = 10,
    min_similarity: float = 0.5,
) -> list[RetrievalHit]:
    """Retrieve top-K chunks matching a query, ranked by cosine similarity.

    Args:
        query: Natural language query string. Will be embedded with
            Nomic's "search_query" prefix automatically.
        workspace_id: Restrict to a single workspace. None = search
            across all workspaces (typically only useful for admin
            queries; production callers should always pass this).
        project_id: Restrict to a project within the workspace.
        source_kind: Restrict to a source kind ("doc", "email", etc).
        limit: Max chunks to return. The HNSW index is queried for
            roughly this many candidates, then filtered by similarity.
        min_similarity: Drop hits below this cosine similarity. With
            normalized Nomic vectors, 0.5 filters obvious noise;
            0.6+ is more strict. Tune per query type as you observe
            real-world scores.

    Returns:
        List of RetrievalHit, sorted by descending similarity.
        Empty list if no chunks meet the threshold.
    """
    if not query or not query.strip():
        raise ValueError("query must be non-empty")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    if not (0.0 <= min_similarity <= 1.0):
        raise ValueError("min_similarity must be in [0.0, 1.0]")

    # 1. Embed the query (search_query prefix applied automatically)
    query_vec = embed_query(query)

    # 2. Build SQL with optional workspace/project/kind filters pushed
    #    into the WHERE clause so the planner can use them alongside
    #    the HNSW index.
    filter_clauses = []
    params: dict[str, Any] = {
        "query_vec": query_vec,
        # Fetch extra candidates so post-filter by min_similarity still
        # returns `limit` results in the common case.
        "limit": limit * 3,
    }
    if workspace_id is not None:
        filter_clauses.append("d.workspace_id = %(workspace_id)s")
        params["workspace_id"] = workspace_id
    if project_id is not None:
        filter_clauses.append("d.project_id = %(project_id)s")
        params["project_id"] = project_id
    if source_kind is not None:
        filter_clauses.append("d.source_kind = %(source_kind)s")
        params["source_kind"] = source_kind

    where_extension = ""
    if filter_clauses:
        where_extension = " AND " + " AND ".join(filter_clauses)

    full_sql = _BASE_SELECT + where_extension + _ORDER_AND_LIMIT

    # 3. Execute and hydrate
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(full_sql, params)
            rows = cur.fetchall()

    # 4. Post-filter by min_similarity and convert to RetrievalHit
    hits: list[RetrievalHit] = []
    for row in rows:
        sim = float(row["similarity"])
        if sim < min_similarity:
            continue

        chunk_meta = row["chunk_metadata"] or {}
        doc_meta = row["document_metadata"] or {}

        hits.append(RetrievalHit(
            chunk_id=row["chunk_id"],
            document_id=row["document_id"],
            chunk_index=row["chunk_index"],
            content=row["content"],
            similarity=sim,
            document_title=row["document_title"],
            document_uri=row["document_uri"],
            source_kind=row["source_kind"],
            workspace_id=row["workspace_id"],
            start_char=chunk_meta.get("start_char"),
            end_char=chunk_meta.get("end_char"),
            chunk_metadata=chunk_meta,
            document_metadata=doc_meta,
        ))

        if len(hits) >= limit:
            break

    return hits


# ---------- Self-test ----------

def _self_test() -> None:
    """Prove the read-path works against the ingested Attention paper.

    Runs three queries chosen to exercise different match qualities:
      1. Exact-concept query ("self-attention mechanism") — should
         return tight, high-similarity hits from the methodology section
      2. Adjacent-concept query ("multi-head attention") — should also
         score high, possibly overlapping the first query's hits
      3. Loosely-related query ("how do transformers handle long
         sequences") — natural-language phrasing, looser scores
    """
    from betty_etl.db import close_pool

    queries = [
        "self-attention mechanism",
        "multi-head attention",
        "how do transformers handle long sequences",
    ]

    for query in queries:
        print(f"\nQuery: {query!r}")
        hits = retrieve(
            query,
            workspace_id="betty-dev",
            limit=3,
            min_similarity=0.4,  # Lower than default so we see borderline scores
        )
        if not hits:
            print("  No hits above threshold.")
            continue

        for i, hit in enumerate(hits):
            preview = hit.content[:120].replace("\n", " ")
            offset_info = (
                f" [chars {hit.start_char}-{hit.end_char}]"
                if hit.start_char is not None
                else ""
            )
            print(
                f"  #{i+1} sim={hit.similarity:.4f}  "
                f"chunk_idx={hit.chunk_index}{offset_info}"
            )
            print(f"      {preview}...")

    # Sanity assertion: the highest-ranked hit for our most specific
    # query should be well above the noise floor.
    print("\nFinal sanity check: top hit for 'self-attention mechanism'")
    top_hits = retrieve(
        "self-attention mechanism",
        workspace_id="betty-dev",
        limit=1,
    )
    assert len(top_hits) == 1, "Expected at least one hit"
    assert top_hits[0].similarity > 0.55, (
        f"Top similarity {top_hits[0].similarity:.4f} is suspiciously low — "
        f"either retrieval is broken or the document isn't ingested"
    )
    print(f"  ✓ Top hit similarity {top_hits[0].similarity:.4f} > 0.55")
    print("\nRetrieval self-test complete.")

    close_pool()


if __name__ == "__main__":
    _self_test()
