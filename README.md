# Swing Trader AI

An AI-powered swing trade analysis pipeline. Given a ticker and a date window, it pulls structured financial data, retrieves and reranks relevant news/analyst/sentiment chunks via RAG, reasons over the context with multiple analyst personas, synthesizes a judge verdict, validates it through guardrails, and outputs a calibrated position card.

---

## Pipeline Architecture

```
[Corpus Generator] ──► [Vector Store]
                              │
[Data Pull] ──────────────────┤
  fundamentals (EDGAR/FMP)    │
  macro (FRED)                ▼
                    [RAG Retrieval + Rerank]
                              │
                    [Prompt Composition]
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         [Bull Persona] [Bear Persona] [Macro Persona] [Technicals Persona]
              └───────────────┼───────────────┘
                              ▼
                       [Judge Synthesis]
                              │
                      [Guardrail Pass]
                              │
                    [Layer 1 Output]
                              │
                   [Layer 2: Position Sizing]
                              │
                      [Position Card]
```

**Layer 1 — Judgment**
- Deterministic data pull (no LLM): fundamentals from SEC EDGAR / FMP, macro from FRED, sector-aware indicator selection
- RAG retrieval with two-stage filter (hard ticker + date filter → semantic rerank) over a markdown corpus of news, analyst, social, and macro files
- Four parallel persona calls (bull, bear, macro, pure-technicals) + judge synthesis with forced tool-use output
- Guardrail pass: citation grounding, confidence/persona-agreement consistency, concrete invalidation condition check; up to 2 retries with failure reason appended to judge prompt

**Layer 2 — Position Sizing**
- Gate: confidence > 0.60 AND magnitude_bucket != "0-3%"
- Fractional Kelly sizing shrunk by 20-day realized volatility
- Outputs entry price, target, stop loss, hold window, position size (min 2%, max 15%)

---

## What's Built

| Component | Status |
|---|---|
| Data pull node — fundamentals (EDGAR/FMP) + macro (FRED), parallel async | Built |
| RAG retrieval node — two-stage filter, cross-encoder rerank, sentiment tagger | Built |
| Pydantic schemas — `Layer1Output`, `PositionCard`, `NumericBlock`, `QualitativeBlock`, `PersonaOutputs`, `GuardrailCheck` | Built |
| FastAPI backend — run trigger, SSE streaming, run history, eval routes | Built |
| Next.js dashboard — pipeline run form, live node timeline, run list, eval page | Built |
| Postgres eval store — schema + migrations | Built |
| LangSmith observability — `@observe_node` decorator, run metadata tagging | Built |
| Prometheus metrics — RAG empty block rate, guardrail failure rate, token cost, latency | Built |
| Eval harness — calibration curves, feature attribution, regression comparison, retrieval eval | Built |
| Corpus generator — news/analyst/social/macro pull, LLM tagging pass, backfill + live modes | Spec complete |
| Dataset pipeline — QoQ/YoY entry generation, ground truth labeling, lookahead prevention | Spec complete |
| Persona + judge nodes | Not built |
| Guardrail node | Not built |
| Layer 2 sizing node | Not built |
| Full LangGraph graph wiring | Not built |
| Self-improvement loop | Not built |

---

## Repo Layout

```
swing-trader/
├── api/
│   ├── main.py                     # FastAPI app — uvicorn entry point
│   └── routes/
│       ├── runs.py                 # POST /runs, GET /runs/{id}/stream (SSE), GET /runs
│       └── eval_routes.py          # GET /eval/* — calibration, attribution, regression
├── apps/
│   └── dashboard/                  # Next.js 14 observer dashboard
│       ├── app/
│       │   ├── page.tsx            # Run trigger form
│       │   ├── runs/
│       │   │   ├── page.tsx        # Run history list
│       │   │   └── [runId]/page.tsx # Live node timeline (SSE)
│       │   └── eval/page.tsx       # Calibration + eval metrics
│       └── components/
│           └── NodeTimeline.tsx    # Real-time node status + output viewer
├── src/swing_trader/
│   ├── state.py                    # LangGraph PipelineState TypedDict
│   ├── schemas/
│   │   └── pipeline.py             # All Pydantic data contracts
│   ├── data_pull/
│   │   ├── node.py                 # LangGraph node: fundamentals + macro in parallel
│   │   ├── fundamentals.py         # EDGAR / FMP client
│   │   ├── macro.py                # FRED client, sector-to-indicator mapping
│   │   ├── block.py                # NumericBlock renderer (no LLM)
│   │   └── cache.py                # HTTP cache layer
│   ├── rag/
│   │   ├── node.py                 # LangGraph node: RAG retrieval
│   │   └── retriever.py            # Two-stage filter + cross-encoder rerank + sentiment tagger
│   ├── db/
│   │   ├── pool.py                 # Postgres connection pool
│   │   ├── store.py                # Eval store read/write
│   │   └── ground_truth.py         # Ground truth population after hold windows close
│   ├── eval/
│   │   └── harness.py              # Calibration curves, feature attribution, regression
│   └── observability/
│       ├── decorators.py           # @observe_node — LangSmith + Prometheus timing
│       ├── langsmith_config.py     # Project + run metadata helpers
│       └── metrics.py              # Prometheus counters and histograms
├── migrations/
│   └── 001_eval_store.sql          # Eval store schema
├── datasets/
│   └── golden_set/
│       ├── template.json           # Golden set entry schema
│       └── entries/                # Curated historical entries
├── corpus/                         # RAG corpus — markdown files (gitignored)
│   ├── news/
│   ├── analyst/
│   ├── social/
│   ├── macro/
│   └── _rejected/                  # Failed quality checks — never indexed
└── docs/
    ├── PRD.md                      # Full product requirements — start here
    ├── DIRECTORY.md                # Doc map and reading order
    ├── CORPUS_GENERATOR.md         # Build and maintain the RAG knowledge base
    ├── DATA_PULL_PIPELINE.md       # Deterministic fundamentals + macro pull
    ├── RAG_PIPELINE.md             # Vector indexing + retrieval + reranking
    ├── REASONING_PIPELINE.md       # Prompt composition + personas + judge
    ├── GUARDRAIL_PIPELINE.md       # Input/runtime/output guardrails + retry
    ├── LAYER2_PIPELINE.md          # Position sizing (Kelly formula)
    ├── DATASET_PIPELINE.md         # QoQ/YoY training dataset generation
    ├── EVAL_PIPELINE.md            # Calibration, regression, self-improvement loop
    └── OBSERVABILITY_PIPELINE.md   # Tracing, metrics, eval store
```

---

## Quick Start

### 1. Python pipeline + API

```bash
# Install Python dependencies
pip install -e .

# Copy and fill in API keys
cp .env.example .env

# Start the FastAPI backend
uvicorn api.main:app --reload --port 8000
```

### 2. Dashboard

```bash
cd apps/dashboard
npm install
npm run dev          # http://localhost:3000
```

### 3. Trigger a run (CLI)

```bash
# Live run
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","window_start":"2024-01-01","window_end":"2024-03-31","run_type":"live"}'

# Then stream node events
curl -N http://localhost:8000/runs/{run_id}/stream
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key — persona + judge calls |
| `LANGSMITH_API_KEY` | LangSmith tracing |
| `LANGSMITH_PROJECT` | LangSmith project name (default: `swing-trader`) |
| `FMP_API_KEY` | Financial Modeling Prep — fundamentals, analyst ratings |
| `FRED_API_KEY` | FRED — macro data (CPI, rates, GDP, yield curve) |
| `POLYGON_API_KEY` | Polygon.io — price/volume, historical closes |
| `NEWS_API_KEY` | NewsAPI — news corpus backfill |
| `DATABASE_URL` | Postgres connection string for eval store |
| `METRICS_PORT` | Prometheus metrics port (default: `9090`) |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/runs` | Trigger a pipeline run. Returns `run_id` immediately (202). |
| `GET` | `/runs/{run_id}/stream` | SSE stream of node events for a live run. |
| `GET` | `/runs/{run_id}` | Fetch a completed run from the eval store. |
| `GET` | `/runs` | List past runs. Query params: `ticker`, `run_type`, `limit`. |
| `GET` | `/eval/calibration` | Calibration curve by confidence bucket. |
| `GET` | `/eval/attribution` | Feature attribution — which driver type drives misses. |
| `GET` | `/health` | Health check. |

### SSE Event Types

```jsonc
{"event": "node_start",     "node": "data_pull",    "ts": 1234567890.0}
{"event": "node_done",      "node": "data_pull",    "elapsed_s": 1.23, "output": {...}}
{"event": "pipeline_done",  "run_id": "...",        "warnings": [...]}
{"event": "error",          "message": "...",       "ts": 1234567890.0}
```

Node order: `data_pull` → `rag_retrieval` → `persona_bull` / `persona_bear` / `persona_macro` / `persona_technicals` (parallel) → `judge` → `guardrails` → `layer2`

---

## RAG Corpus

The corpus is a flat set of markdown files under `corpus/` with YAML frontmatter:

```yaml
---
date: 2024-01-15
tickers: [AAPL]
source_type: news          # news | analyst | social | macro
source: NewsAPI            # NewsAPI | GDELT | FMP | StockTwits | Reddit | FRED
relevance_tags: [earnings, guidance]
sentiment_label: bullish   # added by LLM tagging pass (Claude Haiku)
sentiment_reason: "beat on EPS and raised guidance"
---

# Headline

Body text...
```

File naming: `corpus/{source_type}/{YYYY-MM-DD}_{TICKER}_{slug}.md`

**Retrieval pipeline (four stages):**
1. Hard metadata filter — ticker + date range (eliminates most irrelevance before any model runs)
2. Embedding search — top-50 candidates (`text-embedding-3-small`)
3. Cross-encoder rerank — top-10 by relevance score
4. Sentiment tag pass — labels each chunk bullish/bearish/neutral

Output capped at 1,500 tokens rendered as condensed bullets grouped by source type. If all top-10 chunks score below the reranker threshold, returns an empty block rather than injecting noise.

**Retrieval targets:** Recall@10 > 0.80, Precision@10 > 0.60, MRR > 0.70

---

## Output Schemas

**Layer 1 Output**
```json
{
  "direction": "bullish | bearish | neutral",
  "magnitude_bucket": "0-3% | 3-8% | 8%+",
  "confidence": 0.72,
  "dominant_drivers": ["fundamental", "macro"],
  "invalidation_condition": "Close below $178 or CPI print > 3.5%",
  "hold_window_bucket": "weeks",
  "thesis": "..."
}
```

**Position Card (Layer 2)**
```json
{
  "gate_passed": true,
  "entry_price": 185.50,
  "target_price": 200.00,
  "stop_loss": "Close below $178 or CPI surprise > 0.4%",
  "hold_window_start": "2024-01-15",
  "hold_window_end": "2024-03-31",
  "position_size_pct": 0.05,
  "kelly_full": 0.18,
  "kelly_fractional": 0.045,
  "volatility_used": 0.28,
  "thesis": "..."
}
```

---

## Eval Harness

```python
from swing_trader.eval.harness import (
    load_golden_set,
    calibration_curve,
    feature_attribution,
    regression_comparison,
)

records = load_golden_set("datasets/golden_set/entries/")
curve = calibration_curve(records)
print(curve.to_dict())               # hit rate per confidence bucket
print(curve.corrective_actions())    # "model is OVERCONFIDENT in 90% bucket"
print(feature_attribution(records))  # which driver type correlates with misses
```

Five-tier eval hierarchy:
1. **Golden set** — minimum 50 curated entries, frozen inputs, manually labeled dominant driver
2. **Regression suite** — runs on every prompt/model/parameter change; blocks deploy if hit rate drops > 5pp
3. **Adversarial scenarios** — conflicting signals, misleading headlines, empty RAG block, stale data
4. **LLM-as-judge** — scores 1–5 per dimension: evidence grounding, driver attribution, confidence calibration, invalidation quality, thesis clarity
5. **Human spot-check** — 10–20 entries per session via eval dashboard

Full regression run over 50 golden entries costs ~$5.

---

## Observability

- **LangSmith** — full trace per pipeline run: every node's inputs, outputs, token counts, latency. Trace URL captured and stored in eval store.
- **Prometheus** — scraped at `:9090/metrics`: pipeline runs, node latency, token cost, RAG empty block rate, guardrail failure rate, confidence distribution, gate pass rate.
- **Eval store** — Postgres table of every completed run (50+ columns including all persona outputs, guardrail checks, RAG chunks). Ground truth auto-populated after hold windows close. Feeds calibration curves and the self-improvement loop.

Grafana alert thresholds: > $0.50/run, guardrail failure rate > 10%.

---

## Dataset Pipeline

Generates QoQ (quarter-over-quarter) and YoY (year-over-year) training entries from historical API data. Each entry is a frozen snapshot of all inputs for a given ticker/quarter plus the known ground truth outcome.

- **Lookahead bias prevention** — strict date filter on every RAG chunk; fundamentals use the report immediately preceding the window
- **Ground truth** — price-based labels automated via Polygon; dominant driver labeled via Claude Haiku batch pass or manual curation
- **Data quality score** (0.0–1.0) — entries below 0.70 excluded from golden set and regression suite
- **Dataset splits by time** — train (pre-2023), validation (2023), test (2024+); never random splits
- **Storage** — one JSON file per entry under `datasets/v{version}/entries/`; JSONL split files for streaming

---

## Model Routing

| Stage | Model |
|---|---|
| Persona calls (bull, bear, macro, technicals) | `claude-sonnet-4-6` |
| Judge synthesis | `claude-opus-4-6` |
| LLM tagging pass (corpus generator) | `claude-haiku-4-5-20251001` |
| Dominant driver labeling (dataset pipeline) | `claude-haiku-4-5-20251001` |

---

## Docs

Read in this order:

1. [`docs/PRD.md`](docs/PRD.md) — architecture, goals, build order
2. [`docs/CORPUS_GENERATOR.md`](docs/CORPUS_GENERATOR.md) — how the knowledge base is built
3. [`docs/DATA_PULL_PIPELINE.md`](docs/DATA_PULL_PIPELINE.md) — fundamentals + macro pull
4. [`docs/RAG_PIPELINE.md`](docs/RAG_PIPELINE.md) — vector indexing + retrieval + reranking
5. [`docs/REASONING_PIPELINE.md`](docs/REASONING_PIPELINE.md) — prompt composition + personas + judge
6. [`docs/GUARDRAIL_PIPELINE.md`](docs/GUARDRAIL_PIPELINE.md) — guardrails + retry + cancellation
7. [`docs/LAYER2_PIPELINE.md`](docs/LAYER2_PIPELINE.md) — position sizing (Kelly formula)
8. [`docs/DATASET_PIPELINE.md`](docs/DATASET_PIPELINE.md) — historical training dataset generation
9. [`docs/EVAL_PIPELINE.md`](docs/EVAL_PIPELINE.md) — calibration, regression, self-improvement loop
10. [`docs/OBSERVABILITY_PIPELINE.md`](docs/OBSERVABILITY_PIPELINE.md) — tracing, metrics, eval store
