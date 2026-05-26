-- =====================================================================
-- Betty OpenBrain — Migration 002: judge_decisions audit trail
-- =====================================================================
-- Phase 4.5 minimum audit trail per Phase 4.4 Q6 (locked 2026-05-24).
-- One row per envelope evaluated by the actor, regardless of whether the
-- Judge round-tripped to Anthropic or the read-only path skipped the Judge.
--
-- Purpose:
--   - Reviewing what Betty did overnight resolves to a single query against
--     this table (SELECT ... FROM judge_decisions WHERE timestamp > 'last
--     night'). No log file scraping.
--   - Verdict trail across read_only Judge-skip, reversible_write Judge gate,
--     and external_side_effect Judge gate is uniform — one schema covers all
--     three risk classes per Q1 Decision A.
--   - envelope_json captures the full OB1 envelope for replay/debug.
--
-- Non-goals for Phase 4.5:
--   - Cross-table joins to source_documents or memories. Phase 4.5 keeps
--     envelope_json self-contained; future phases can ETL into normalized
--     tables if needed.
--   - Operator UI integration. Phase 4.7 is deferred until after the
--     travelpec.com win.
--   - authorization_refs / evidence_refs / expected_consequence enforcement.
--     These ride inside envelope_json as forward-compat content; semantic
--     validation comes when the OB1 envelope expands beyond the Phase 4.5
--     minimum (risk_class + authorization_refs only).
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS judge_decisions (
    id                BIGSERIAL PRIMARY KEY,
    timestamp         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    call_id           TEXT NOT NULL,             -- UUID4 from ToolCall.call_id
    tool_name         TEXT NOT NULL,             -- denormalized for cheap filtering
    risk_class        TEXT NOT NULL CHECK (
        risk_class IN ('read_only', 'reversible_write', 'external_side_effect', 'high_risk')
    ),
    envelope_json     JSONB NOT NULL,            -- full Envelope shape for replay
    verdict           TEXT NOT NULL CHECK (
        verdict IN ('APPROVE', 'REJECT', 'SKIP_READ_ONLY')
    ),
    cost_usd          NUMERIC(10, 6) NOT NULL DEFAULT 0,  -- 0 for SKIP_READ_ONLY and circuit-breaker rejects
    reasoning         TEXT,                      -- Judge's reasoning string; NULL for SKIP_READ_ONLY
    executed_at       TIMESTAMPTZ,               -- when the tool callable actually ran
    execution_result  JSONB                      -- ToolResult.payload (proposal_path for write, data for read)
);

-- Audit query patterns:
--   "What happened overnight?" — by timestamp DESC
--   "All rejections for a tool" — by tool_name + verdict
--   "How much have we spent on Judge calls today?" — sum cost_usd by date_trunc
CREATE INDEX IF NOT EXISTS idx_judge_decisions_timestamp
    ON judge_decisions (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_judge_decisions_tool_name
    ON judge_decisions (tool_name);
CREATE INDEX IF NOT EXISTS idx_judge_decisions_verdict
    ON judge_decisions (verdict);
CREATE INDEX IF NOT EXISTS idx_judge_decisions_call_id
    ON judge_decisions (call_id);

-- Record the migration.
INSERT INTO schema_migrations (version, description)
VALUES ('002', 'Add judge_decisions audit trail for Phase 4.5 envelope minimum')
ON CONFLICT (version) DO NOTHING;

COMMIT;
