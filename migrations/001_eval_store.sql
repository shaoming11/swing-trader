-- Eval store schema for Swing Trader AI
-- Run this against the Postgres database before the first pipeline run.
-- Idempotent: all objects use IF NOT EXISTS or are safe to re-run.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- for gen_random_uuid()

CREATE TABLE IF NOT EXISTS pipeline_runs (

    -- ── Identity ──────────────────────────────────────────────────────────
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker              TEXT NOT NULL,
    window_start        DATE NOT NULL,
    window_end          DATE NOT NULL,
    user_id             TEXT,                       -- NULL for system/eval runs
    run_type            TEXT NOT NULL DEFAULT 'live',  -- live | backfill | eval
    pipeline_version    TEXT NOT NULL DEFAULT '0.0.0',

    -- ── Layer 1 output ─────────────────────────────────────────────────────
    direction           TEXT,           -- bullish | bearish | neutral
    magnitude_bucket    TEXT,           -- 0-3% | 3-8% | 8%+
    confidence          FLOAT,
    dominant_drivers    TEXT[],
    invalidation_condition TEXT,
    hold_window_bucket  TEXT,           -- days | weeks | quarter
    thesis              TEXT,

    -- Reasoning attribution (from Layer 1 judge)
    sided_with          TEXT[],
    sided_reasoning     TEXT,

    -- ── Layer 2 output ─────────────────────────────────────────────────────
    gate_passed         BOOLEAN,
    gate_skip_reason    TEXT,
    entry_price         FLOAT,
    target_price        FLOAT,
    stop_loss           TEXT,
    hold_window_start   DATE,
    hold_window_end     DATE,
    position_size_pct   FLOAT,

    -- Sizing audit
    kelly_full          FLOAT,
    kelly_fractional    FLOAT,
    volatility_used     FLOAT,

    -- ── Ground truth (populated after hold_window_end closes) ─────────────
    actual_price_at_entry   FLOAT,
    actual_price_at_exit    FLOAT,
    actual_direction        TEXT,
    actual_magnitude_pct    FLOAT,
    hit                     BOOLEAN,        -- direction + magnitude bucket matched
    price_target_error_pct  FLOAT,          -- (predicted_mid - actual) / actual * 100
    timing_result           TEXT,           -- early | on-time | late | never
    ground_truth_populated  BOOLEAN NOT NULL DEFAULT FALSE,

    -- ── Debug: inputs ──────────────────────────────────────────────────────
    numeric_block_text      TEXT,
    data_gaps               TEXT[],
    sources_used            TEXT[],
    rag_chunks_retrieved    INT,
    rag_chunks_used         INT,
    rag_top_chunks          JSONB,  -- [{content, score, source_type, date}]

    -- ── Debug: reasoning ───────────────────────────────────────────────────
    persona_bull_output     TEXT,
    persona_bear_output     TEXT,
    persona_macro_output    TEXT,
    persona_technicals_output TEXT,
    judge_reasoning         TEXT,

    -- ── Debug: guardrails ──────────────────────────────────────────────────
    guardrail_checks        JSONB,  -- [{name, passed, reason}]
    guardrail_retries       INT NOT NULL DEFAULT 0,
    pipeline_cancelled      BOOLEAN NOT NULL DEFAULT FALSE,
    cancellation_reason     TEXT,
    warnings                TEXT[],

    -- ── Trace ──────────────────────────────────────────────────────────────
    langsmith_trace_url     TEXT,

    -- ── Timestamps ─────────────────────────────────────────────────────────
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);

-- ── Indexes ────────────────────────────────────────────────────────────────────

-- Primary access pattern: look up runs by ticker + window
CREATE INDEX IF NOT EXISTS idx_runs_ticker_window
    ON pipeline_runs (ticker, window_start, window_end);

-- Multi-tenant: look up all runs for a user
CREATE INDEX IF NOT EXISTS idx_runs_user
    ON pipeline_runs (user_id)
    WHERE user_id IS NOT NULL;

-- Calibration curve queries filter by confidence
CREATE INDEX IF NOT EXISTS idx_runs_confidence
    ON pipeline_runs (confidence)
    WHERE confidence IS NOT NULL;

-- Self-improvement loop: find unresolved ground truth records
CREATE INDEX IF NOT EXISTS idx_runs_ground_truth_pending
    ON pipeline_runs (hold_window_end, gate_passed)
    WHERE ground_truth_populated = FALSE
      AND pipeline_cancelled = FALSE;

-- General time-series access
CREATE INDEX IF NOT EXISTS idx_runs_created
    ON pipeline_runs (created_at DESC);

-- Pipeline version comparison (regression checks)
CREATE INDEX IF NOT EXISTS idx_runs_version
    ON pipeline_runs (pipeline_version, created_at DESC);

-- ── Ticker metadata table (used for sector-level feature attribution) ──────────

CREATE TABLE IF NOT EXISTS ticker_metadata (
    ticker      TEXT PRIMARY KEY,
    company     TEXT,
    sector      TEXT,
    industry    TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Views ──────────────────────────────────────────────────────────────────────

-- Calibration curve view
CREATE OR REPLACE VIEW calibration_curve AS
SELECT
    CASE
        WHEN confidence >= 0.85 THEN '90%+'
        WHEN confidence >= 0.65 THEN '70-85%'
        WHEN confidence >= 0.45 THEN '50-65%'
        ELSE '<50%'
    END AS confidence_bucket,
    COUNT(*) AS total,
    SUM(hit::int) AS correct,
    ROUND(AVG(hit::int) * 100, 1) AS actual_hit_rate_pct
FROM pipeline_runs
WHERE ground_truth_populated = TRUE
  AND pipeline_cancelled = FALSE
  AND hit IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC;

-- Feature attribution view (which drivers correlate with misses)
CREATE OR REPLACE VIEW feature_attribution AS
SELECT
    unnest(dominant_drivers) AS driver,
    COUNT(*) AS total_calls,
    SUM((NOT hit)::int) AS misses,
    ROUND(AVG((NOT hit)::int) * 100, 1) AS miss_rate_pct
FROM pipeline_runs
WHERE ground_truth_populated = TRUE
  AND pipeline_cancelled = FALSE
  AND hit IS NOT NULL
GROUP BY 1
ORDER BY miss_rate_pct DESC;

-- Pipeline version regression view
CREATE OR REPLACE VIEW pipeline_regression AS
SELECT
    pipeline_version,
    COUNT(*) AS runs,
    ROUND(AVG(hit::int) * 100, 1) AS hit_rate_pct,
    ROUND(AVG(confidence) * 100, 1) AS avg_confidence_pct,
    ROUND(AVG(guardrail_retries), 2) AS avg_retries,
    ROUND(AVG(CASE WHEN pipeline_cancelled THEN 1.0 ELSE 0.0 END) * 100, 1) AS cancellation_rate_pct
FROM pipeline_runs
WHERE ground_truth_populated = TRUE
GROUP BY 1
ORDER BY 1 DESC;
