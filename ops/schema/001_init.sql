-- =====================================================================
-- Betty OpenBrain — Migration 001: Substrate Initialization
-- =====================================================================
-- Establishes the minimum schema for Stage 1: ingest documents, chunk
-- them, embed chunks with nomic-embed-text (768-dim), store, and query
-- via cosine similarity. No memory/review/judge tables yet — those
-- belong to later stages.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- Extensions
-- ---------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- ---------------------------------------------------------------------
-- Migration tracking
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version      TEXT PRIMARY KEY,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description  TEXT
);

-- ---------------------------------------------------------------------
-- source_documents: one row per ingested artifact (email, PDF, file)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_documents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      TEXT NOT NULL DEFAULT 'betty',
    project_id        TEXT,
    source_kind       TEXT NOT NULL,           -- 'email' | 'pdf' | 'file' | 'note'
    title             TEXT,
    uri               TEXT NOT NULL,            -- filesystem path or message-id
    checksum_sha256   TEXT NOT NULL,            -- idempotency key
    mime_type         TEXT,
    source_timestamp  TIMESTAMPTZ,              -- when the artifact itself was created
    imported_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    summary           TEXT,
    content           TEXT,                     -- full normalized text
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by        TEXT NOT NULL DEFAULT 'system',
    CONSTRAINT source_documents_checksum_unique UNIQUE (checksum_sha256)
);

CREATE INDEX IF NOT EXISTS idx_source_documents_workspace
    ON source_documents (workspace_id);
CREATE INDEX IF NOT EXISTS idx_source_documents_kind
    ON source_documents (source_kind);
CREATE INDEX IF NOT EXISTS idx_source_documents_imported
    ON source_documents (imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_documents_metadata
    ON source_documents USING GIN (metadata);

-- ---------------------------------------------------------------------
-- source_document_chunks: many per parent, each with its own embedding
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_document_chunks (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_document_id  UUID NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    chunk_index         INTEGER NOT NULL,
    content             TEXT NOT NULL,
    embedding           VECTOR(768),              -- nomic-embed-text dimension
    token_count         INTEGER,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chunks_unique_per_doc UNIQUE (source_document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_document
    ON source_document_chunks (source_document_id);

-- HNSW index for cosine similarity search on embeddings.
-- m=16, ef_construction=64 are pgvector's recommended defaults for
-- moderate-scale corpora. Tune if recall/latency demand it later.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
    ON source_document_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- ---------------------------------------------------------------------
-- Record the migration
-- ---------------------------------------------------------------------
INSERT INTO schema_migrations (version, description)
VALUES ('001', 'Initialize source_documents and source_document_chunks with pgvector HNSW')
ON CONFLICT (version) DO NOTHING;

COMMIT;
