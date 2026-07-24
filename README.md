# Swing Trader AI

An AI-powered swing trade analysis pipeline. Given a ticker and a date window, it pulls structured financial data, retrieves and reranks relevant news/analyst/sentiment chunks via RAG, reasons over the context with multiple analyst personas, synthesizes a judge verdict, validates it through guardrails, and outputs a calibrated position card.

---

## Pipeline Architecture

```
[Data Pull] → [RAG Retrieval + Relevance Filter] → [Prompt Composition]
    → [Multi-Persona Reasoning] → [Judge Synthesis] → [Guardrail Pass]
    → [Layer 1 Output] → [Layer 2: Position Sizing] → [Position Card]
```

**Layer 1 — Judgment**
- Deterministic data pull (no LLM): fundamentals from SEC EDGAR / FMP, macro from FRED
- RAG retrieval with two-stage filter (hard ticker+date filter → semantic rerank) over a markdown corpus of news, analyst, social, and macro files
- Four parallel persona calls (bull, bear, macro, technicals) + judge synthesis
- Guardrail pass: citation grounding, confidence/persona-agreement consistency, concrete invalidation condition check

**Layer 2 — Position Sizing**
- Confidence + magnitude gate before acting
- Fractional Kelly sizing shrunk by volatility
- Outputs entry price, target, stop loss, hold window, position size

---

## What's Built

| Track | Component | Status |
|---|---|---|
| A1 | RAG retrieval node, two-stage filter, sentiment tagger | Built |
| A2 | Fundamentals pull (EDGAR/FMP), macro pull (FRED), numeric block renderer | Built |
| B1 | All Pydantic schemas (`Layer1Output`, `PositionCard`, `NumericBlock`, `QualitativeBlock`, `PersonaOutputs`, `GuardrailCheck`) | Built |
| B2 | Postgres eval store schema + migrations | Built |
| B3 | LangSmith observability, node decorators, Prometheus metrics | Built |
| C2 | Eval harness: calibration curves, feature attribution, regression comparison, retrieval eval | Built |

Remaining: prompt composition, persona + judge nodes, guardrail node, Layer 2 sizing node, full LangGraph graph wiring, self-improvement loop, multi-tenancy.

---

## Quick Start

```bash
# Install dependencies
pip install -e .

# Copy and fill in your API keys
cp .env.example .env

# Run the pipeline through the data pull + RAG stages
python main.py AAPL 2024-01-01 2024-03-31

# Backfill / eval modes
python main.py TSLA 2024-06-01 2024-09-30 --run-type backfill
python main.py MSFT 2024-01-01 2024-03-31 --run-type eval

# Skip Prometheus metrics server
python main.py AAPL 2024-01-01 2024-03-31 --no-metrics
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key for persona + judge calls |
| `LANGSMITH_API_KEY` | LangSmith tracing |
| `LANGSMITH_PROJECT` | LangSmith project name (default: `swing-trader`) |
| `FMP_API_KEY` | Financial Modeling Prep — fundamentals |
| `FRED_API_KEY` | FRED — macro data (CPI, rates, GDP) |
| `POLYGON_API_KEY` | Polygon.io — price/volume |
| `NEWS_API_KEY` | NewsAPI — news corpus |
| `DATABASE_URL` | Postgres connection string for eval store |
| `METRICS_PORT` | Prometheus metrics port (default: `9090`) |

---

## Project Structure

```
swing-trader/
├── main.py                          # Pipeline entry point
├── src/swing_trader/
│   ├── state.py                     # LangGraph PipelineState TypedDict
│   ├── schemas/
│   │   └── pipeline.py              # All Pydantic data contracts
│   ├── data_pull/
│   │   ├── node.py                  # LangGraph node: fundamentals + macro in parallel
│   │   ├── fundamentals.py          # EDGAR / FMP client
│   │   ├── macro.py                 # FRED client
│   │   ├── block.py                 # Numeric block renderer (no LLM)
│   │   └── cache.py                 # HTTP cache layer
│   ├── rag/
│   │   ├── node.py                  # LangGraph node: RAG retrieval
│   │   └── retriever.py             # Two-stage filter + reranker + sentiment tagger
│   ├── db/
│   │   ├── pool.py                  # Postgres connection pool
│   │   ├── store.py                 # Eval store write/read path
│   │   └── ground_truth.py          # Ground truth population helpers
│   ├── eval/
│   │   └── harness.py               # Calibration curves, feature attribution, regression
│   └── observability/
│       ├── decorators.py            # @observe_node LangSmith decorator
│       ├── langsmith_config.py      # LangSmith project + metadata helpers
│       └── metrics.py               # Prometheus counters and histograms
├── migrations/
│   └── 001_eval_store.sql           # Eval store schema
├── datasets/
│   └── golden_set/
│       ├── README.md
│       ├── template.json            # Golden set entry template
│       └── entries/                 # Curated historical entries (gitkeep)
├── corpus/                          # RAG corpus (markdown files, gitignored)
│   ├── news/
│   ├── analyst/
│   ├── social/
│   └── macro/
└── docs/
    ├── PRD.md                       # Full product requirements
    ├── IMPLEMENTATION_PLAN.md       # Parallel build tracks
    └── *.md                         # Per-pipeline deep dives
```

---

## RAG Corpus

The corpus is a flat set of markdown files under `corpus/` with YAML frontmatter:

```yaml
---
date: 2024-01-15
tickers: [AAPL]
source_type: news        # news | analyst | social | macro
source: NewsAPI
relevance_tags: [earnings, guidance]
---
```

Naming convention: `{YYYY-MM-DD}_{TICKER}_{source}.md`

Files are chunked at 300–500 tokens with 50-token overlap, embedded, and stored in the vector store with ticker + date metadata for hard filtering before semantic rerank.

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
print(feature_attribution(records))  # which driver type drives the most misses
```

---

## Output Schemas

**Layer 1 Output**
```json
{
  "direction": "bullish | bearish | neutral",
  "magnitude_bucket": "0-3% | 3-8% | 8%+",
  "confidence": 0.0,
  "dominant_drivers": ["fundamental | macro | sentiment | technical"],
  "invalidation_condition": "concrete, checkable condition",
  "hold_window_bucket": "days | weeks | quarter",
  "thesis": "natural language, stored in trade journal"
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

## Observability

- **LangSmith** — full trace per pipeline run: every node's inputs, outputs, token counts, latency
- **Prometheus** — scraped at `:9090/metrics`: RAG chunks retrieved/used, empty block rate, guardrail failure rate, pipeline cancellation rate
- **Eval store** — Postgres table of every completed Layer 1 output; ground truth populated after the hold window closes; feeds calibration curves and the self-improvement loop

---

## Docs

- [`docs/PRD.md`](docs/PRD.md) — full product requirements and design decisions
- [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) — parallel build tracks and critical path
- [`docs/RAG_PIPELINE.md`](docs/RAG_PIPELINE.md) — RAG retrieval deep dive
- [`docs/DATA_PULL_PIPELINE.md`](docs/DATA_PULL_PIPELINE.md) — data pull deep dive
- [`docs/EVAL_PIPELINE.md`](docs/EVAL_PIPELINE.md) — eval framework deep dive
- [`docs/OBSERVABILITY_PIPELINE.md`](docs/OBSERVABILITY_PIPELINE.md) — observability design
