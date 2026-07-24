-- Migration 001: pipeline_runs eval store
-- Run once: psql $DATABASE_URL -f db/migrations/001_pipeline_runs.sql

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id              UUID PRIMARY KEY,
    ticker              TEXT NOT NULL,
    window_start        DATE NOT NULL,
    window_end          DATE NOT NULL,
    user_id             TEXT,
    run_type            TEXT NOT NULL DEFAULT 'live',
    pipeline_version    TEXT NOT NULL DEFAULT '0.0.0',
    completed_at        TIMESTAMPTZ,

    -- Layer 1
    direction           TEXT,
    magnitude_bucket    TEXT,
    confidence          FLOAT,
    dominant_drivers    TEXT[],
    invalidation_condition TEXT,
    hold_window_bucket  TEXT,
    thesis              TEXT,
    sided_with          TEXT[],
    sided_reasoning     TEXT,

    -- Layer 2
    gate_passed         BOOLEAN,
    gate_skip_reason    TEXT,
    entry_price         FLOAT,
    target_price        FLOAT,
    stop_loss           TEXT,
    hold_window_start   DATE,
    hold_window_end     DATE,
    position_size_pct   FLOAT,
    kelly_full          FLOAT,
    kelly_fractional    FLOAT,
    volatility_used     FLOAT,

    -- Debug: inputs
    numeric_block_text  TEXT,
    data_gaps           TEXT[],
    sources_used        TEXT[],
    rag_chunks_retrieved INT DEFAULT 0,
    rag_chunks_used     INT DEFAULT 0,
    rag_top_chunks      JSONB DEFAULT '[]',

    -- Debug: reasoning
    persona_bull_output     TEXT,
    persona_bear_output     TEXT,
    persona_macro_output    TEXT,
    persona_technicals_output TEXT,
    judge_reasoning         TEXT,

    -- Debug: guardrails
    guardrail_checks        JSONB DEFAULT '[]',
    guardrail_retries       INT DEFAULT 0,
    pipeline_cancelled      BOOLEAN DEFAULT FALSE,
    cancellation_reason     TEXT,
    warnings                TEXT[],

    langsmith_trace_url     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_ticker        ON pipeline_runs (ticker);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_completed_at  ON pipeline_runs (completed_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline_version ON pipeline_runs (pipeline_version);

-- Ground truth (populated later by outcome tracking)
ALTER TABLE pipeline_runs
    ADD COLUMN IF NOT EXISTS actual_direction       TEXT,
    ADD COLUMN IF NOT EXISTS actual_magnitude_pct   FLOAT,
    ADD COLUMN IF NOT EXISTS hit                    BOOLEAN,
    ADD COLUMN IF NOT EXISTS price_target_error_pct FLOAT,
    ADD COLUMN IF NOT EXISTS timing_result          TEXT;
