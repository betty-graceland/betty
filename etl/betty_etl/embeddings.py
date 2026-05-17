"""
Betty ETL embeddings.

Wraps sentence-transformers to batch-embed Chunk objects into
768-dim float32 vectors using nomic-ai/nomic-embed-text-v1.5.

Nomic-specific requirements honored here:
  - trust_remote_code=True is required on model load
  - document chunks MUST be encoded with prompt_name="search_document"
  - matching query-side prompt (used in retrieval.py later) is
    "search_query" — DO NOT mix these up or retrieval quality
    will collapse silently

Vectors are L2-normalized so cosine similarity == dot product,
matching the vector_ip_ops HNSW index in 001_init.sql.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from betty_etl.chunking import Chunk
from betty_etl.config import EMBED

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


@dataclass
class EmbeddedChunk:
    """A Chunk plus its embedding vector, token count, and the model that produced it."""

    chunk: Chunk
    embedding: list[float]
    model_name: str
    embedding_dim: int
    token_count: int

    @property
    def index(self) -> int:
        return self.chunk.index

    @property
    def content(self) -> str:
        return self.chunk.content


@lru_cache(maxsize=1)
def _load_model(model_name: str, device: str) -> "SentenceTransformer":
    """Load and cache the embedding model.

    Nomic requires trust_remote_code=True. First load downloads
    ~550MB to ~/.cache/huggingface and warms the MPS device.
    """
    from sentence_transformers import SentenceTransformer

    print(f"Loading embedding model: {model_name} on {device}")
    model = SentenceTransformer(
        model_name,
        device=device,
        trust_remote_code=True,
    )
    model.max_seq_length = EMBED.max_seq_length
    # Nomic v1.5 ships with an empty prompts dict — register task
    # prefixes so prompt_name="search_document"/"search_query" work.
    model.prompts = {
        "search_document": "search_document: ",
        "search_query": "search_query: ",
    }
    return model


def embed_chunks(
    chunks: list[Chunk],
    model_name: str | None = None,
    device: str | None = None,
    batch_size: int | None = None,
    normalize: bool | None = None,
) -> list[EmbeddedChunk]:
    """Embed a list of chunks in batches.

    Uses Nomic's "search_document" prompt prefix as required for
    document-side embeddings. Returns EmbeddedChunk objects in input
    order.
    """
    if not chunks:
        return []

    name = model_name if model_name is not None else EMBED.model_name
    dev = device if device is not None else EMBED.device
    batch = batch_size if batch_size is not None else EMBED.batch_size
    norm = normalize if normalize is not None else EMBED.normalize

    model = _load_model(name, dev)

    texts = [c.content for c in chunks]
    vectors = model.encode(
        texts,
        batch_size=batch,
        normalize_embeddings=norm,
        prompt_name=EMBED.document_prompt_name,  # Nomic: "search_document"
        show_progress_bar=len(texts) > 32,
        convert_to_numpy=True,
    )

    # Token counts come from the same tokenizer the model used.
    # Cheap second pass — no re-encoding, just tokenization.
    token_counts = [
        len(model.tokenizer.encode(t, add_special_tokens=True))
        for t in texts
    ]

    dim = vectors.shape[1]
    if dim != EMBED.expected_dim:
        raise ValueError(
            f"Model {name} produced dim {dim}, "
            f"but EMBED.expected_dim is {EMBED.expected_dim}. "
            f"Schema's vector(N) column must match — this is a migration."
        )

    return [
        EmbeddedChunk(
            chunk=chunk,
            embedding=vec.astype("float32").tolist(),
            model_name=name,
            embedding_dim=dim,
            token_count=tc,
        )
        for chunk, vec, tc in zip(chunks, vectors, token_counts)
    ]


def embed_query(
    query: str,
    model_name: str | None = None,
    device: str | None = None,
    normalize: bool | None = None,
) -> list[float]:
    """Embed a single query string with Nomic's "search_query" prefix.

    Mirrors embed_chunks() but for the read-path: uses the
    "search_query" prompt (NOT "search_document") so the embedding
    lives in the correct subspace for retrieval against
    document-side embeddings. Mixing prefixes silently collapses
    retrieval quality, so this is the only sanctioned way to embed
    a query.

    Returns a 768-dim float32 vector as a Python list, ready to pass
    to pgvector via psycopg.
    """
    if not query or not query.strip():
        raise ValueError("query must be non-empty")

    name = model_name if model_name is not None else EMBED.model_name
    dev = device if device is not None else EMBED.device
    norm = normalize if normalize is not None else EMBED.normalize

    model = _load_model(name, dev)

    vector = model.encode(
        query,
        normalize_embeddings=norm,
        prompt_name="search_query",  # Nomic query-side prefix
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    if vector.shape[0] != EMBED.expected_dim:
        raise ValueError(
            f"Query embedding dim {vector.shape[0]} != "
            f"expected {EMBED.expected_dim}"
        )

    return vector.astype("float32").tolist()


def _self_test() -> None:
    """Extract → chunk → embed the Stage 2 test PDF and print a summary."""
    import math
    from betty_etl.config import TEST_DATA_DIR
    from betty_etl.chunking import chunk_text
    from betty_etl.extractors.pdf import extract_pdf

    test_pdf = TEST_DATA_DIR / "attention-is-all-you-need.pdf"
    print(f"Extracting, chunking, and embedding: {test_pdf}")

    doc = extract_pdf(test_pdf)
    chunks = chunk_text(doc.text_for_chunking)
    embedded = embed_chunks(chunks)

    first = embedded[0]
    print(f"  Chunks embedded: {len(embedded)}")
    print(f"  Model:           {first.model_name}")
    print(f"  Dimension:       {first.embedding_dim}")
    print(f"  First 5 dims:    {[round(v, 4) for v in first.embedding[:5]]}")

    norm = math.sqrt(sum(v * v for v in first.embedding))
    print(f"  L2 norm (chunk 0): {norm:.6f}  (should be ~1.0 if normalized)")
    print("  Self-test complete.")


if __name__ == "__main__":
    _self_test()
