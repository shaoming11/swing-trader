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
- Guardrail pass: confidence floor, invalidation quality, data gap threshold, persona agreement consistency, neutral-high-confidence check; up to 2 retries with failure reason appended to judge prompt

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
| Persona reasoning node — 4 parallel LLM calls (bull, bear, macro, technicals) | Built |
| Judge synthesis node — 70B model synthesizes personas into Layer1Output | Built |
| Guardrail node — 5 checks, retry loop, pipeline cancellation on exhaustion | Built |
| Pydantic schemas — `Layer1Output`, `PositionCard`, `NumericBlock`, `QualitativeBlock`, `PersonaOutputs`, `GuardrailCheck` | Built |
| FastAPI backend — run trigger, SSE streaming, run history, eval routes, API key auth, rate limiting | Built |
| Next.js dashboard — pipeline run form, live node timeline, run list, eval page | Built |
| Postgres eval store — schema + migrations | Built |
| LangSmith observability — `@observe_node` decorator, run metadata tagging | Built |
| Prometheus metrics — RAG empty block rate, guardrail failure rate, token cost, latency | Built |
| Eval harness — calibration curves, feature attribution, regression comparison, retrieval eval | Built |
| Corpus generator — news/analyst/social/macro pull, LLM tagging pass, backfill + live modes | Built |
| CI/CD — GitHub Actions (lint, test, security audit, Docker build) | Built |
| Docker — multi-stage Dockerfile, production docker-compose | Built |
| Self-improvement loop — iterative eval + prompt tuning via SSE | Built |
| Dataset pipeline — QoQ/YoY entry generation, ground truth labeling, lookahead prevention | Spec complete |
| Layer 2 sizing node | Not built |
| Full LangGraph graph wiring | Not built |

---

## Repo Layout

```
swing-trader/
├── api/
│   ├── main.py                     # FastAPI app — CORS, auth, rate limiting, health checks
│   ├── middleware.py               # API key auth + sliding-window rate limiter
│   └── routes/
│       ├── runs.py                 # POST /runs, GET /runs/{id}/stream (SSE), GET /runs
│       ├── eval_routes.py          # GET /eval/* — calibration, attribution, regression
│       └── corpus.py               # POST /corpus/backfill, POST /corpus/index, GET /corpus/status
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
│   ├── clients.py                  # LLM/embed client factory (Groq + Jina AI)
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
│   │   ├── retriever.py            # Two-stage filter + cross-encoder rerank + sentiment tagger
│   │   ├── indexer.py              # Corpus → vector store ingestion
│   │   └── store.py                # Vector store abstraction (Chroma / Qdrant)
│   ├── reasoning/
│   │   ├── node.py                 # Persona reasoning (4 parallel LLM calls)
│   │   ├── judge.py                # Judge synthesis (70B model)
│   │   ├── guardrails.py           # 5 guardrail checks + retry loop
│   │   └── prompts.py              # System/user prompt templates
│   ├── corpus/
│   │   ├── generator.py            # Backfill + live corpus generation
│   │   ├── sources.py              # Polygon, GDELT, FMP, FRED, StockTwits pullers
│   │   ├── writer.py               # Markdown writer with dedup + quality check
│   │   └── tagger.py               # LLM batch tagging pass
│   ├── db/
│   │   ├── pool.py                 # Postgres connection pool (asyncpg)
│   │   ├── store.py                # Eval store read/write
│   │   └── ground_truth.py         # Ground truth population after hold windows close
│   ├── eval/
│   │   ├── harness.py              # Calibration curves, feature attribution, regression
│   │   ├── self_improve.py         # Iterative eval + prompt tuning loop
│   │   ├── metrics.py              # Eval scoring functions
│   │   └── benchmarks.py           # RAG retrieval benchmarks
│   └── observability/
│       ├── decorators.py           # @observe_node — LangSmith + Prometheus timing
│       ├── langsmith_config.py     # Project + run metadata helpers
│       └── metrics.py              # Prometheus counters and histograms
├── migrations/
│   └── 001_eval_store.sql          # Eval store schema (idempotent)
├── .github/
│   └── workflows/
│       └── ci.yml                  # Lint + test + security audit + Docker build
├── Dockerfile                      # Multi-stage build, non-root user
├── docker-compose.yml              # API + Postgres + Prometheus + Grafana
├── .env.example                    # All config vars documented
├── corpus/                         # RAG corpus — markdown files (gitignored)
└── docs/                           # Deep-dive design docs
```

---

## Quick Start

### 1. Python pipeline + API

```bash
# Install Python dependencies
pip install -e ".[dev]"

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

### 3. Docker (full stack)

```bash
# Start everything: API + Postgres + Prometheus + Grafana
docker compose up --build

# API at http://localhost:8000
# Dashboard: run separately (cd apps/dashboard && npm run dev)
# Grafana at http://localhost:3000 (admin / admin)
```

### 4. Trigger a run

```bash
# Via API (add -H "X-API-Key: yourkey" if API_KEYS is set)
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"ticker":"AAPL","window_start":"2024-01-01","window_end":"2024-03-31"}'

# Then stream node events
curl -N http://localhost:8000/runs/{run_id}/stream

# Via CLI
python main.py AAPL 2024-01-01 2024-03-31

# Batch run (all tickers in watchlist.yaml)
python main.py --batch
```

---

## Environment Variables

See [`.env.example`](.env.example) for all variables. Key ones:

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | Yes | Groq API key for all LLM inference |
| `JINA_API_KEY` | Yes | Jina AI key for embeddings |
| `DATABASE_URL` | Recommended | Postgres connection string for eval store |
| `FMP_API_KEY` | Recommended | Financial Modeling Prep — fundamentals, analyst ratings |
| `FRED_API_KEY` | Recommended | FRED — macro data (CPI, rates, GDP, yield curve) |
| `POLYGON_API_KEY` | Recommended | Polygon.io — news articles for corpus |
| `API_KEYS` | Production | Comma-separated API keys for auth (empty = disabled) |
| `CORS_ORIGINS` | Production | Comma-separated allowed origins (empty = localhost only) |
| `RATE_LIMIT_PER_MINUTE` | Optional | Per-IP rate limit (default: 30) |

---

## API Reference

All endpoints require `X-API-Key` header when `API_KEYS` env var is set.

| Method | Path | Description |
|---|---|---|
| `POST` | `/runs` | Trigger a pipeline run. Returns `run_id` immediately (202). |
| `GET` | `/runs/{run_id}/stream` | SSE stream of node events for a live run. |
| `GET` | `/runs/{run_id}` | Fetch a completed run from the eval store. |
| `GET` | `/runs` | List past runs. Query params: `ticker`, `run_type`, `limit`. |
| `GET` | `/eval/calibration` | Calibration curve by confidence bucket. |
| `GET` | `/eval/attribution` | Feature attribution — which driver type drives misses. |
| `GET` | `/eval/regression` | Version-over-version hit rate comparison. |
| `POST` | `/eval/self-improve` | Trigger self-improvement loop (SSE stream). |
| `POST` | `/corpus/backfill` | Trigger corpus backfill for tickers (SSE stream). |
| `POST` | `/corpus/index` | Run RAG indexer on unindexed corpus files. |
| `GET` | `/corpus/status` | Corpus file counts and vector store stats. |
| `GET` | `/health` | Health check — verifies DB + vector store connectivity. |

### SSE Event Types

```jsonc
{"event": "node_start",     "node": "data_pull",    "ts": 1234567890.0}
{"event": "node_done",      "node": "data_pull",    "elapsed_s": 1.23, "output": {...}}
{"event": "pipeline_done",  "run_id": "...",        "warnings": [...]}
{"event": "error",          "message": "...",       "ts": 1234567890.0}
```

Node order: `data_pull` → `rag_retrieval` → `persona_bull` / `persona_bear` / `persona_macro` / `persona_technicals` (parallel) → `judge` → `guardrails` → `layer2`

---

## Guardrails

The guardrail node runs 5 checks on the judge's Layer 1 output:

| Check | Description |
|---|---|
| `confidence_floor` | Confidence must be >= 0.40 (configurable) |
| `invalidation_quality` | Invalidation condition must reference a specific price, %, or event |
| `data_gap_threshold` | Pipeline must not have > 4 data gaps |
| `persona_agreement` | High confidence requires minimum persona agreement |
| `neutral_high_confidence` | Neutral direction with > 0.80 confidence is contradictory |

On failure, the judge is retried up to 2 times with failure reasons injected into the prompt. If all retries exhaust, the pipeline is cancelled.

---

## Security

- **API key auth** — `X-API-Key` header validated against `API_KEYS` env var. Disabled when unset (dev mode).
- **Rate limiting** — sliding-window per-IP, configurable via `RATE_LIMIT_PER_MINUTE` (default: 30/min). Returns 429 with `Retry-After` header.
- **CORS** — restricted to `CORS_ORIGINS` env var. Defaults to `localhost:3000` only.
- **Non-root Docker** — production container runs as `appuser`.
- **Startup validation** — fails fast if required API keys are missing.
- **No secrets in code** — all credentials via env vars, `.env` gitignored.

---

## Deployment

### Railway / Render / Fly.io

1. Push the repo
2. Set env vars from `.env.example` in the platform dashboard
3. The Dockerfile is auto-detected and built

### Docker Compose (self-hosted)

```bash
# Set production passwords
export POSTGRES_PASSWORD=<strong-password>
export GRAFANA_PASSWORD=<strong-password>
export API_KEYS=<your-api-key>
export CORS_ORIGINS=https://yourdomain.com

docker compose up -d
```

---

## RAG Corpus

The corpus is a flat set of markdown files under `corpus/` with YAML frontmatter:

```yaml
---
date: 2024-01-15
tickers: [AAPL]
source_type: news          # news | analyst | social | macro
source: Polygon            # Polygon | GDELT | FMP | StockTwits | FRED
relevance_tags: [earnings, guidance]
sentiment_label: bullish   # added by LLM tagging pass
sentiment_reason: "beat on EPS and raised guidance"
---

# Headline

Body text...
```

File naming: `corpus/{source_type}/{YYYY-MM-DD}_{TICKER}_{slug}.md`

**Retrieval pipeline (four stages):**
1. Hard metadata filter — ticker + date range
2. Embedding search — top-50 candidates (Jina `jina-embeddings-v2-base-en`)
3. Cross-encoder rerank — top-10 by relevance score
4. Sentiment tag pass — labels each chunk bullish/bearish/neutral

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
  "thesis": "..."
}
```

---

## Model Routing

| Stage | Model | Provider |
|---|---|---|
| Sentiment tagging | `llama-3.2-3b-preview` | Groq (free) |
| Persona calls (bull, bear, macro, technicals) | `llama-3.1-8b-instant` | Groq (free) |
| Judge synthesis | `llama-3.3-70b-versatile` | Groq (free) |
| Embeddings | `jina-embeddings-v2-base-en` | Jina AI (free) |

---

## CI/CD

GitHub Actions runs on every push and PR to `main`:

- **Lint** — `ruff check` + `ruff format --check`
- **Test** — `pytest` with Postgres service container
- **Security** — `pip-audit` dependency vulnerability scan
- **Docker** — build verification (runs after lint + test pass)

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
