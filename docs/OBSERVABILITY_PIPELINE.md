# Observability Pipeline — Design Spec

Covers tracing, metrics, and the eval store across both the Layer 1 (judgment) and Layer 2 (position sizing) pipelines. Three independent concerns: tracing per run, aggregated metrics over time, and eval records for the self-improvement loop.

---

## 1. Architecture Overview

```
Every pipeline run
        │
        ├──→ LangSmith Trace          (per-run, full detail, automatic via LangGraph)
        │
        ├──→ Prometheus Metrics        (aggregated signals, scraped by Grafana)
        │
        └──→ Postgres Eval Store       (structured record per run, ground truth populated later)
                    │
                    └──→ Human Eval UI  (read-only dashboard over eval store + LangSmith links)
```

These three write targets are independent. A failure in one does not affect the others or the pipeline itself. Observability writes are always fire-and-forget — never block the pipeline on a metrics write.

---

## 2. LangSmith Tracing

### 2.1 Setup

LangSmith integrates with LangGraph automatically via environment variables. No manual instrumentation needed for LangGraph nodes.

```python
# .env
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY={KEY}
LANGCHAIN_PROJECT=swing-trader-{environment}   # e.g., swing-trader-prod, swing-trader-dev
```

Every LangGraph node execution, LLM call, tool call, token count, latency, input, and output is captured automatically.

### 2.2 Run Metadata

Tag every pipeline run with metadata at the graph invocation level so traces are filterable in LangSmith:

```python
from langchain_core.runnables import RunnableConfig

config = RunnableConfig(
    metadata={
        "ticker": ticker,
        "window_start": str(window_start),
        "window_end": str(window_end),
        "user_id": user_id,           # for multi-tenant filtering
        "run_type": "live | backfill | eval",
        "pipeline_version": PIPELINE_VERSION,
    },
    tags=[ticker, f"Q{quarter}", environment]
)

result = pipeline.invoke(initial_state, config=config)
```

### 2.3 What LangSmith Captures Automatically

For every LangGraph node:
- Node name, start time, end time, duration
- Input state (serialized)
- Output state (serialized)
- Any exceptions

For every LLM call within a node:
- Model name
- Prompt tokens, completion tokens, total tokens
- Estimated cost (if configured)
- Raw input messages and output
- Latency to first token and total latency

### 2.4 Manual Span Annotations

Add structured metadata to specific nodes for richer filtering. Use `get_current_run_tree()` from `langsmith`:

```python
from langsmith import get_current_run_tree

def rag_retrieval_node(state):
    run = get_current_run_tree()
    # ... retrieval logic ...
    if run:
        run.add_metadata({
            "chunks_retrieved": block.chunks_retrieved,
            "chunks_used": block.chunks_used,
            "top_reranker_score": max(i.relevance_score for i in block.items) if block.items else 0,
            "empty_block": block.chunks_used == 0
        })
    return state
```

Apply the same pattern in `guardrail_pass` to log which checks ran and which failed.

### 2.5 Trace URL Capture

After each run, capture the LangSmith trace URL and write it to the eval store:

```python
from langsmith import Client

client = Client()

def get_trace_url(run_id: str) -> str:
    run = client.read_run(run_id)
    return f"https://smith.langchain.com/public/{run.id}/r"
```

---

## 3. Metrics

### 3.1 Instrumentation

Use the `prometheus_client` library. Metrics are registered globally and exposed on a `/metrics` HTTP endpoint scraped by Prometheus.

```python
from prometheus_client import Counter, Histogram, Gauge, start_http_server

# Pipeline-level
PIPELINE_RUNS_TOTAL = Counter(
    "pipeline_runs_total",
    "Total pipeline runs",
    ["ticker", "run_type", "outcome"]   # outcome: completed | cancelled | failed
)

PIPELINE_DURATION_SECONDS = Histogram(
    "pipeline_duration_seconds",
    "End-to-end pipeline duration",
    ["run_type"],
    buckets=[5, 10, 30, 60, 120, 300]
)

# Node-level latency
NODE_DURATION_SECONDS = Histogram(
    "node_duration_seconds",
    "Per-node execution duration",
    ["node_name"],
    buckets=[0.5, 1, 2, 5, 10, 30]
)

# LLM cost
LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Total tokens used",
    ["node_name", "model", "token_type"]   # token_type: input | output
)

LLM_COST_USD_TOTAL = Counter(
    "llm_cost_usd_total",
    "Estimated LLM cost in USD",
    ["node_name", "model"]
)

# RAG
RAG_CHUNKS_RETRIEVED = Histogram(
    "rag_chunks_retrieved",
    "Chunks retrieved before rerank",
    buckets=[0, 5, 10, 20, 50]
)

RAG_CHUNKS_USED = Histogram(
    "rag_chunks_used",
    "Chunks used after rerank and dedup",
    buckets=[0, 2, 5, 10]
)

RAG_EMPTY_BLOCK_TOTAL = Counter(
    "rag_empty_block_total",
    "Times retrieval returned no usable chunks",
    ["ticker"]
)

# Guardrails
GUARDRAIL_CHECKS_TOTAL = Counter(
    "guardrail_checks_total",
    "Guardrail check executions",
    ["check_name", "result"]   # result: pass | fail
)

GUARDRAIL_RETRIES_TOTAL = Counter(
    "guardrail_retries_total",
    "Judge call retries triggered by guardrail failures",
    ["reason"]
)

PIPELINE_CANCELLATIONS_TOTAL = Counter(
    "pipeline_cancellations_total",
    "Pipeline runs cancelled after guardrail exhaustion",
    ["reason"]
)

# Layer 1 outputs
CONFIDENCE_DISTRIBUTION = Histogram(
    "layer1_confidence",
    "Distribution of Layer 1 confidence scores",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
)

DIRECTION_TOTAL = Counter(
    "layer1_direction_total",
    "Layer 1 direction outputs",
    ["direction"]   # bullish | bearish | neutral
)

# Layer 2 gate
LAYER2_GATE_TOTAL = Counter(
    "layer2_gate_total",
    "Layer 2 gate decisions",
    ["decision"]   # acted | skipped
)
```

### 3.2 Instrumentation Hooks

Wrap each LangGraph node with a timing decorator that fires metrics automatically:

```python
import time
from functools import wraps

def observe_node(node_name: str):
    def decorator(fn):
        @wraps(fn)
        def wrapper(state):
            start = time.time()
            try:
                result = fn(state)
                NODE_DURATION_SECONDS.labels(node_name=node_name).observe(time.time() - start)
                return result
            except Exception as e:
                NODE_DURATION_SECONDS.labels(node_name=node_name).observe(time.time() - start)
                raise
        return wrapper
    return decorator

# Usage
@observe_node("rag_retrieval")
def rag_retrieval_node(state): ...

@observe_node("judge_synthesis")
def judge_synthesis_node(state): ...
```

For LLM token metrics, extract token counts from the LangChain callback or from the response object directly after each call:

```python
def record_llm_usage(node_name: str, model: str, response):
    usage = response.usage_metadata
    LLM_TOKENS_TOTAL.labels(node_name=node_name, model=model, token_type="input").inc(usage.input_tokens)
    LLM_TOKENS_TOTAL.labels(node_name=node_name, model=model, token_type="output").inc(usage.output_tokens)
    cost = estimate_cost(model, usage.input_tokens, usage.output_tokens)
    LLM_COST_USD_TOTAL.labels(node_name=node_name, model=model).inc(cost)
```

### 3.3 Cost Estimation Table

```python
MODEL_COST_PER_1K_TOKENS = {
    "claude-opus-4-6":    {"input": 0.015, "output": 0.075},
    "claude-sonnet-4-6":  {"input": 0.003, "output": 0.015},
    "claude-haiku-4-5":   {"input": 0.00025, "output": 0.00125},
}

def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = MODEL_COST_PER_1K_TOKENS.get(model, {"input": 0, "output": 0})
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])
```

### 3.4 Grafana Dashboard Panels

| Panel | Query | Alert Threshold |
|---|---|---|
| Pipeline runs/hour | `rate(pipeline_runs_total[1h])` | — |
| Cost per run (USD) | `rate(llm_cost_usd_total[1h]) / rate(pipeline_runs_total[1h])` | > $0.50/run |
| p95 pipeline latency | `histogram_quantile(0.95, pipeline_duration_seconds)` | > 120s |
| p95 node latency by node | `histogram_quantile(0.95, node_duration_seconds)` | — |
| Guardrail failure rate | `rate(guardrail_checks_total{result="fail"}[1h]) / rate(guardrail_checks_total[1h])` | > 10% |
| Pipeline cancellation rate | `rate(pipeline_cancellations_total[1h]) / rate(pipeline_runs_total[1h])` | > 5% |
| RAG empty block rate | `rate(rag_empty_block_total[1h]) / rate(pipeline_runs_total[1h])` | > 15% |
| Confidence distribution | `histogram_quantile` buckets over `layer1_confidence` | — |
| Gate pass rate | `layer2_gate_total{decision="acted"} / layer2_gate_total` | — |

---

## 4. Eval Store

### 4.1 Schema

```sql
CREATE TABLE pipeline_runs (
    -- Identity
    run_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticker              TEXT NOT NULL,
    window_start        DATE NOT NULL,
    window_end          DATE NOT NULL,
    user_id             TEXT,                          -- null for system/eval runs
    run_type            TEXT NOT NULL,                 -- live | backfill | eval
    pipeline_version    TEXT NOT NULL,

    -- Layer 1 output
    direction           TEXT,                          -- bullish | bearish | neutral
    magnitude_bucket    TEXT,                          -- 0-3% | 3-8% | 8%+
    confidence          FLOAT,
    dominant_drivers    TEXT[],
    invalidation_condition TEXT,
    hold_window_bucket  TEXT,                          -- days | weeks | quarter
    thesis              TEXT,

    -- Layer 2 output
    gate_passed         BOOLEAN,
    gate_skip_reason    TEXT,                          -- if gate_passed = false
    entry_price         FLOAT,
    target_price        FLOAT,
    stop_loss           TEXT,
    hold_window_start   DATE,
    hold_window_end     DATE,
    position_size_pct   FLOAT,

    -- Ground truth (populated after hold_window_end)
    actual_price_at_entry   FLOAT,
    actual_price_at_exit    FLOAT,
    actual_direction        TEXT,
    actual_magnitude_pct    FLOAT,
    hit                     BOOLEAN,                  -- did direction + magnitude bucket match?
    price_target_error_pct  FLOAT,                    -- (predicted - actual) / actual * 100
    timing_result           TEXT,                     -- early | on-time | late | never
    ground_truth_populated  BOOLEAN DEFAULT FALSE,

    -- Debug: inputs
    numeric_block_text      TEXT,                     -- the full structured numeric block
    data_gaps               TEXT[],
    sources_used            TEXT[],
    rag_chunks_retrieved    INT,
    rag_chunks_used         INT,
    rag_top_chunks          JSONB,                    -- [{content, score, source_type, date}]

    -- Debug: reasoning
    persona_bull_output     TEXT,
    persona_bear_output     TEXT,
    persona_macro_output    TEXT,
    persona_technicals_output TEXT,
    judge_reasoning         TEXT,

    -- Debug: guardrails
    guardrail_checks        JSONB,                    -- [{name, passed, reason}]
    guardrail_retries       INT DEFAULT 0,
    pipeline_cancelled      BOOLEAN DEFAULT FALSE,
    cancellation_reason     TEXT,
    warnings                TEXT[],

    -- Trace link
    langsmith_trace_url     TEXT,

    -- Timestamps
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    completed_at            TIMESTAMPTZ
);

-- Indexes for common query patterns
CREATE INDEX idx_runs_ticker_window ON pipeline_runs (ticker, window_start, window_end);
CREATE INDEX idx_runs_user ON pipeline_runs (user_id);
CREATE INDEX idx_runs_confidence ON pipeline_runs (confidence);
CREATE INDEX idx_runs_ground_truth ON pipeline_runs (ground_truth_populated, hit);
CREATE INDEX idx_runs_created ON pipeline_runs (created_at DESC);
```

### 4.2 Writing a Run Record

Write to the eval store at pipeline completion, not per-node. Use a single upsert so re-runs on the same ticker+window update the existing record.

```python
async def write_eval_record(state: PipelineState, trace_url: str):
    record = {
        "run_id": state.run_id,
        "ticker": state.ticker,
        "window_start": state.window_start,
        "window_end": state.window_end,
        "user_id": state.user_id,
        "run_type": state.run_type,
        "pipeline_version": PIPELINE_VERSION,

        # Layer 1
        "direction": state.layer1_output.direction if state.layer1_output else None,
        "magnitude_bucket": state.layer1_output.magnitude_bucket if state.layer1_output else None,
        "confidence": state.layer1_output.confidence if state.layer1_output else None,
        "dominant_drivers": state.layer1_output.dominant_drivers if state.layer1_output else None,
        "invalidation_condition": state.layer1_output.invalidation_condition if state.layer1_output else None,
        "hold_window_bucket": state.layer1_output.hold_window_bucket if state.layer1_output else None,
        "thesis": state.layer1_output.thesis if state.layer1_output else None,

        # Layer 2
        "gate_passed": state.layer2_output.gate_passed if state.layer2_output else None,
        "entry_price": state.layer2_output.entry_price if state.layer2_output else None,
        "target_price": state.layer2_output.target_price if state.layer2_output else None,

        # Inputs
        "numeric_block_text": state.numeric_block.rendered_text if state.numeric_block else None,
        "data_gaps": state.numeric_block.data_gaps if state.numeric_block else [],
        "rag_chunks_retrieved": state.qualitative_block.chunks_retrieved if state.qualitative_block else 0,
        "rag_chunks_used": state.qualitative_block.chunks_used if state.qualitative_block else 0,

        # Reasoning
        "persona_bull_output": state.persona_outputs.get("bull"),
        "persona_bear_output": state.persona_outputs.get("bear"),
        "persona_macro_output": state.persona_outputs.get("macro"),
        "persona_technicals_output": state.persona_outputs.get("technicals"),
        "judge_reasoning": state.judge_reasoning,

        # Guardrails
        "guardrail_checks": state.guardrail_checks,
        "guardrail_retries": state.guardrail_retries,
        "pipeline_cancelled": state.pipeline_cancelled,
        "cancellation_reason": state.cancellation_reason,
        "warnings": state.warnings,

        "langsmith_trace_url": trace_url,
        "completed_at": datetime.utcnow()
    }

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO pipeline_runs ({columns})
            VALUES ({placeholders})
            ON CONFLICT (run_id) DO UPDATE SET {updates}
        """, record)
```

### 4.3 Populating Ground Truth

Ground truth is populated by a separate daily job that runs after each hold window closes:

```python
async def populate_ground_truth():
    """
    For all runs where ground_truth_populated = false
    and hold_window_end <= today, fetch actual price and compute metrics.
    """
    async with db_pool.acquire() as conn:
        pending = await conn.fetch("""
            SELECT run_id, ticker, hold_window_end, entry_price, target_price,
                   direction, magnitude_bucket, hold_window_start
            FROM pipeline_runs
            WHERE ground_truth_populated = false
              AND gate_passed = true
              AND hold_window_end <= CURRENT_DATE
              AND pipeline_cancelled = false
        """)

    for run in pending:
        actual_exit_price = await fetch_price(run["ticker"], run["hold_window_end"])
        actual_entry_price = await fetch_price(run["ticker"], run["hold_window_start"])

        actual_magnitude = (actual_exit_price - actual_entry_price) / actual_entry_price * 100
        actual_direction = "bullish" if actual_magnitude > 1 else "bearish" if actual_magnitude < -1 else "neutral"

        predicted_magnitude_mid = MAGNITUDE_BUCKET_MIDPOINTS[run["magnitude_bucket"]]
        price_target_error = ((predicted_magnitude_mid - actual_magnitude) / abs(actual_magnitude)) * 100 if actual_magnitude != 0 else None

        hit = (actual_direction == run["direction"]) and magnitude_bucket_matches(actual_magnitude, run["magnitude_bucket"])

        await conn.execute("""
            UPDATE pipeline_runs SET
                actual_price_at_entry   = $1,
                actual_price_at_exit    = $2,
                actual_direction        = $3,
                actual_magnitude_pct    = $4,
                hit                     = $5,
                price_target_error_pct  = $6,
                ground_truth_populated  = true
            WHERE run_id = $7
        """, actual_entry_price, actual_exit_price, actual_direction,
             actual_magnitude, hit, price_target_error, run["run_id"])
```

---

## 5. Eval Queries

Standard queries used by the self-improvement loop and human eval UI.

### Calibration curve
```sql
SELECT
    CASE
        WHEN confidence >= 0.85 THEN '90%'
        WHEN confidence >= 0.65 THEN '70%'
        WHEN confidence >= 0.45 THEN '50%'
        ELSE '<50%'
    END AS confidence_bucket,
    COUNT(*) AS total,
    SUM(hit::int) AS correct,
    ROUND(AVG(hit::int) * 100, 1) AS actual_hit_rate_pct
FROM pipeline_runs
WHERE ground_truth_populated = true
  AND pipeline_cancelled = false
GROUP BY 1
ORDER BY 1 DESC;
```

### Feature attribution on misses
```sql
SELECT
    unnest(dominant_drivers) AS driver,
    COUNT(*) AS total_calls,
    SUM((NOT hit)::int) AS misses,
    ROUND(AVG((NOT hit)::int) * 100, 1) AS miss_rate_pct
FROM pipeline_runs
WHERE ground_truth_populated = true
  AND pipeline_cancelled = false
GROUP BY 1
ORDER BY miss_rate_pct DESC;
```

### Price target error by sector (requires sector column — join with a ticker metadata table)
```sql
SELECT
    ticker,
    AVG(ABS(price_target_error_pct)) AS mean_abs_error,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ABS(price_target_error_pct)) AS median_abs_error
FROM pipeline_runs
WHERE ground_truth_populated = true
  AND price_target_error_pct IS NOT NULL
GROUP BY ticker
ORDER BY mean_abs_error DESC;
```

### Timing analysis
```sql
SELECT
    timing_result,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM pipeline_runs
WHERE ground_truth_populated = true
  AND gate_passed = true
GROUP BY 1;
```

### Regression check (compare pipeline versions)
```sql
SELECT
    pipeline_version,
    COUNT(*) AS runs,
    ROUND(AVG(hit::int) * 100, 1) AS hit_rate_pct,
    ROUND(AVG(confidence) * 100, 1) AS avg_confidence,
    ROUND(AVG(guardrail_retries), 2) AS avg_retries
FROM pipeline_runs
WHERE ground_truth_populated = true
GROUP BY 1
ORDER BY 1 DESC;
```

---

## 6. Human Eval UI

Read-only. Built over the eval store with links to LangSmith for trace drill-down. No custom trace viewer needed.

### 6.1 Views to Build

**Run list view**
- Columns: ticker, date window, confidence, direction, magnitude bucket, hit (checkmark/cross once ground truth available), created at
- Filters: ticker, run type, date range, hit/miss, gate passed
- Click a row → run detail view

**Run detail view**
- Numeric block (structured data fed to the model)
- Four persona outputs side by side
- Judge reasoning
- Guardrail checks table (name, pass/fail, reason)
- Layer 2 position card
- Ground truth (actual direction, actual magnitude, hit/miss)
- Link: "View full trace in LangSmith" → `langsmith_trace_url`

**Calibration chart**
- Bar chart: confidence bucket (x-axis) vs. actual hit rate (y-axis)
- Overlay: diagonal line showing perfect calibration
- Data: calibration query above

**Miss analysis view**
- Feature attribution table (driver → miss rate)
- Filter by time range and ticker
- Data: feature attribution query above

**Regression dashboard**
- Table comparing hit rate, avg confidence, avg retries by `pipeline_version`
- Used to confirm no backsliding after prompt changes

### 6.2 Stack

- Backend: FastAPI serving the eval queries as JSON endpoints
- Frontend: simple React or Next.js table + chart views; use Recharts for the calibration chart
- Auth: same auth as the main app; users see only their own `user_id` records
- LangSmith links: open in new tab, no embedding needed

---

## 7. Implementation Notes

- **Prometheus:** `prometheus_client` Python library; expose metrics on port 9090 via `start_http_server(9090)`
- **Postgres:** `asyncpg` for async DB writes; use a connection pool (`asyncpg.create_pool`)
- **LangSmith:** `langsmith` Python SDK; `LANGCHAIN_TRACING_V2=true` is all that is needed for automatic tracing
- **Observability writes are non-blocking:** wrap all metric increments and DB writes in `asyncio.shield` or a background task — never await them in the hot path
- **Environment separation:** use separate LangSmith projects (`swing-trader-dev`, `swing-trader-prod`) and separate Postgres schemas (`dev`, `prod`) to prevent eval data contamination
- **PII:** `user_id` in the eval store should be an opaque internal ID, not an email or name
