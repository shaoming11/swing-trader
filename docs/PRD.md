# Swing Trader AI — Product Requirements Document

## 1. Problem Statement

Stock market data is abundant but mostly irrelevant. Not all news matters, not all fundamentals move price, and not all sentiment signals are reliable. A swing trader needs a system that pulls the right context for a given ticker and time window, reasons over it with multiple perspectives, and produces a structured, actionable position card — not just a prediction.

---

## 2. Goals

- Identify high-conviction swing trade setups (1-day to 1-quarter hold windows)
- Produce calibrated confidence scores, not overconfident guesses
- Self-improve through a structured eval loop on historical quarters
- Be cost-efficient, safe, and extensible to multiple users

---

## 3. What Context Matters

### 3.1 Fundamental Factors (Company-Level)
- Earnings vs. expectations (EPS actual vs. estimate)
- Valuation & growth: P/E ratio, revenue growth, dividend payouts
- Corporate actions: M&A, product launches, executive changes, buybacks

### 3.2 Macroeconomic Factors (Systemic-Level)
- Interest rate decisions (Fed meetings, rate prints)
- Economic indicators: CPI, GDP growth, unemployment
- Geopolitics & policy: regulations, elections, trade wars, global conflicts

### 3.3 Market Sentiment (Behavioral-Level)
- Retail sentiment: Reddit, StockTwits
- Media & analyst ratings: news coverage, analyst upgrades/downgrades
- Herd mentality signals: fear/greed index, panic vs. euphoria indicators

### 3.4 Technical Factors (Market-Level)
- Trading volume and liquidity
- Price momentum and trend signals

---

## 4. Data Sources

| Data Type | Source | Notes |
|---|---|---|
| Fundamentals / filings | SEC EDGAR (free), Financial Modeling Prep, yfinance | Structured, quarterly |
| Macro (CPI, GDP, rates) | FRED API | Free, clean, authoritative |
| Price / volume / technicals | Polygon.io, Alpha Vantage, IEX Cloud | Real-time or delayed |
| News | NewsAPI, GDELT | GDELT preferred for geopolitics |
| Sentiment | StockTwits API, Reddit API | LLM-scored directly; no paid sentiment feed |

---

## 5. System Architecture

### 5.1 Pipeline Overview

```
[Data Pull] → [RAG Retrieval + Relevance Filter] → [Prompt Composition]
    → [Multi-Persona Reasoning] → [Judge Synthesis] → [Guardrail Pass]
    → [Layer 1 Output] → [Layer 2: Position Sizing] → [Position Card]
```

### 5.2 Layer 1 — Judgment Pipeline

#### Step 1: Deterministic Data Pull (no LLM)
- Pull fundamentals from SEC EDGAR / FMP: EPS actual vs. estimate, revenue growth, guidance changes, corporate actions for the window
- Pull macro from FRED for the same date range: CPI, rate decisions, unemployment — filtered to sector-relevant indicators (e.g., rate-sensitive REIT vs. semiconductor names)
- Output: a fixed structured block of hard numbers in a template — no LLM prose, no summarization

#### Step 2: RAG Retrieval + Relevance Filtering
- Chunk the markdown/text news and sentiment corpus with metadata:
  - `ticker[]` — tickers mentioned
  - `date` — publication date
  - `source_type` — `news | analyst | social`
- Embed and retrieve top-k chunks with filter: `ticker == X AND date within window`
- Rerank using a cheaper model or relevance classifier (solves "not all news is relevant")
- Run lightweight LLM sentiment scoring per chunk: `bullish | bearish | neutral` + one-line reason
- Output: condensed, deduped qualitative bullet points (not raw retrieved text)

#### Step 3: Prompt Composition
- One composed prompt = structured numeric block + condensed qualitative chunks + explicit output schema instruction
- Keep qualitative block short to avoid noisy context

#### Step 4: Multi-Persona Reasoning
- Fire parallel LLM calls with distinct system prompts:
  - Bull case analyst
  - Bear case analyst
  - Macro-only view
  - Pure-technicals view
- Each gets the same composed prompt; returns its argument

#### Step 5: Judge Synthesis
- A separate judge/synthesis call receives all persona outputs
- Forced to produce one verdict, naming which argument(s) it sides with and why
- Returns via tool-call/function-calling with strict schema (see Layer 1 Output)

#### Step 6: Guardrail Pass
Validate before accepting as Layer 1 output:
- `dominant_drivers` must appear in the input (no hallucinated citations)
- Confidence must be internally consistent with persona agreement (50/50 bull/bear split → confidence cannot be 90%)
- `invalidation_condition` must reference a concrete, checkable number or event — not vague language
- Fail → retry judge call with specific inconsistency flagged, or drop to a lower-confidence default

#### Layer 1 Output Schema
```json
{
  "direction": "bullish | bearish | neutral",
  "magnitude_bucket": "0-3% | 3-8% | 8%+",
  "confidence": 0.0,
  "dominant_drivers": ["fundamental | macro | sentiment | technical"],
  "invalidation_condition": "string — concrete and checkable",
  "hold_window_bucket": "days | weeks | quarter",
  "thesis": "string — natural language, kept for trade journal"
}
```

---

### 5.3 Layer 2 — Position Sizing Pipeline

#### Gate
- Only act if `confidence > threshold` AND `magnitude_bucket != negligible`

#### Sizing
- Fractional Kelly-style formula: `f(confidence, volatility)`
- Confidence and position size are NOT a straight line — high volatility names get shrunk even at high confidence

#### Position Card Output
```json
{
  "entry_price": "current price or entry zone",
  "target_price": "entry × magnitude_bucket applied",
  "stop_loss": "invalidation_condition translated to price level or event trigger",
  "hold_window": "date range from hold_window_bucket",
  "position_size": "sizing formula output",
  "thesis": "from Layer 1"
}
```

---

## 6. RAG Corpus Organization

### 6.1 Corpus Structure (Markdown Files)

```
corpus/
  news/
    {YYYY-MM-DD}_{ticker}_{source}.md
  analyst/
    {YYYY-MM-DD}_{ticker}_{firm}.md
  social/
    {YYYY-MM-DD}_{ticker}_{platform}.md
  macro/
    {YYYY-MM-DD}_{indicator}.md   # e.g., CPI, FOMC, GDP
```

### 6.2 Required Frontmatter per File
```yaml
---
date: YYYY-MM-DD
tickers: [AAPL, MSFT]
source_type: news | analyst | social | macro
source: NewsAPI | GDELT | StockTwits | FRED
relevance_tags: [earnings, macro, sentiment, technical]
---
```

### 6.3 Chunking Strategy
- Chunk size: ~300–500 tokens
- Overlap: ~50 tokens
- Metadata filters applied before embedding retrieval: `ticker` + `date_range`
- Reranker (lightweight cross-encoder or LLM call) reduces top-50 to top-10 before composition

### 6.4 Relevance Filtering Approach
Do NOT rely on embedding similarity alone. Apply a two-stage filter:
1. Hard filter: `ticker in chunk.tickers AND chunk.date within [window_start, window_end]`
2. Soft rerank: semantic relevance to the specific event/thesis being analyzed

---

## 7. Eval Framework

### 7.1 Eval Hierarchy

| Layer | Type | Description |
|---|---|---|
| 1 | Golden set | Curated past quarters with known outcomes; primary ground truth |
| 2 | Regression tests | Re-run after every prompt or rule change to catch backsliding |
| 3 | Adversarial | Feed deliberately misleading news; confirm agent does not overreact |
| 4 | LLM-as-judge | Evaluate reasoning quality on each call |
| 5 | Human eval | Periodic spot-check; not primary signal |

### 7.2 Key Eval Metrics

**Certainty eval:** System must identify at least 3 positions per quarter with `confidence >= 0.90`

**Calibration curve:** Bucket predictions by stated confidence (90%, 70%, 50%) and measure actual hit rate per bucket. If "90% confident" calls land at 60%, that is the direct "be less certain" signal.

**Price target error:** `(predicted − actual) / actual` — tracked by sector and volatility regime

**Timing error:** Did price hit target before or after the hold window? Informs whether to shrink or extend hold periods.

**Feature attribution on misses:** For wrong calls, which input bucket (fundamental / macro / sentiment / technical) correlates most with the error → tells you what to reweight in prompt composition.

### 7.3 Self-Improvement Loop

```
1. Train on one quarter + year-over-year set
2. Make predictions
3. See results → analyze diffs:
   - Where was the model wrong?
   - Was confidence too high or too low?
   - Was the hold window too short or too long?
   - Which input type (macro vs. sentiment) drove errors?
4. Update:
   - Prompt rules and system prompts
   - Relevance filter weights
   - Rewrite markdown corpus files where context was misleading
5. Move to next quarter
6. Run regression suite to confirm no backsliding
```

---

## 8. Testing Dataset

### 8.1 Structure
- Compare quarterly reports (quarter-over-quarter) and year-over-year reports
- Each dataset entry contains:
  - Input: fundamentals, macro data, news/sentiment chunks for the window
  - Ground truth: actual price movement in the hold window after the quarter
  - Labels: magnitude bucket, direction, dominant driver (manually labeled)

### 8.2 Time Ranges
- From the prior year (year-over-year comparisons)
- From the most recent quarter (quarter-over-quarter comparisons)

### 8.3 Goal
Feed the model the correct context + financial data + reasoning process for historical periods, then validate that it accurately predicts the stock price direction and magnitude bucket. This validates that the pipeline produces reliable signals before live deployment.

---

## 9. Orchestration & Implementation

### 9.1 Orchestration Tool
**LangGraph** — manages the pipeline as a stateful graph with conditional edges for retry, fallback, and guardrail failure handling.

### 9.2 Structured Outputs
- All LLM calls that produce structured data must use function-calling / tool-call mode
- Validate every output against a Pydantic (Python) schema
- Malformed output → retry with the specific validation error appended to the prompt (max 2 retries, then graceful fallback)

### 9.3 Agent Guardrails

| Stage | Guardrail |
|---|---|
| Input | Deterministic filters: prompt injection detection, ticker validation, date range sanity check |
| Runtime | Validate retrieved chunks are within scope before adding to context |
| Output | Schema validation, internal consistency check (confidence vs. persona agreement), citation grounding check |
| Pipeline | Cancel entire pipeline if Layer 1 guardrail fails after retries; do not pass garbage to Layer 2 |

### 9.4 Model Routing
- Deterministic data pull: no model
- Relevance reranker / sentiment tagger: small/cheap model (SLM) or cross-encoder
- Persona reasoning calls: mid-tier model (e.g., Claude Sonnet or equivalent)
- Judge synthesis: highest-capability model (e.g., Claude Opus or equivalent)
- Guardrail checks: rule-based + lightweight model

### 9.5 Cost Controls
- Hard token budgets per pipeline run
- Reranker runs before expensive reasoning calls to minimize context size
- SLMs for classification/tagging; expensive models only for reasoning and judgment
- Per-user cost tracking for multi-tenant isolation

---

## 10. Multi-Tenancy

- Each user gets isolated:
  - Corpus (their own news/sentiment files)
  - API key context
  - Eval history and calibration data
  - Cost tracking
- No cross-user data leakage at retrieval or reasoning stages
- Role-based access: users only access their own pipeline runs and position cards

---

## 11. Production Stack

| Component | Responsibility |
|---|---|
| Serving | API layer exposing pipeline trigger, status, and position card retrieval |
| Orchestration | LangGraph stateful graph, hosted or self-managed |
| Vector store | Embeddings + metadata filters for RAG retrieval |
| Data ingestion | Scheduled pulls from FRED, EDGAR, Polygon, NewsAPI/GDELT |
| Eval runner | Batch job per prompt/rule change, reports regression deltas |
| CI/CD | Run regression eval suite on every prompt change; block deploys on calibration regression |

---

## 12. Observability

Observability spans three layers: tracing (per-run, end-to-end), metrics (aggregated health signals), and the eval store (self-improvement loop input). These are independent concerns and should be built independently.

### 12.1 Tracing

**Tool: LangSmith** — integrates natively with LangGraph. Every node execution, LLM call, token count, latency, input, and output is captured automatically with minimal instrumentation. Non-LLM steps (EDGAR pull, FRED pull, vector store query) should be wrapped in LangGraph nodes so they appear in the same trace.

What is captured per pipeline run:

| Node | What to log |
|---|---|
| `data_pull` | Ticker, date window, sources hit, structured block output, latency |
| `rag_retrieval` | Chunks retrieved count, chunks post-rerank count, top chunk scores, filter applied |
| `prompt_composition` | Final prompt token count, qualitative block size |
| `persona_[bull\|bear\|macro\|technicals]` | Tokens in/out, latency, full output (stored for judge input and feature attribution) |
| `judge_synthesis` | Tokens in/out, latency, raw output before schema validation |
| `guardrail_pass` | Each check name, pass/fail, failure reason, retry count |
| `layer2_sizing` | Gate decision (acted/skipped), sizing inputs (confidence, volatility), position card output |

Retry events and pipeline cancellations are recorded automatically as LangGraph conditional edge traversals.

### 12.2 Metrics

Track aggregated signals in a time-series store (Grafana + Postgres for self-hosted; Datadog for multi-tenant production):

| Metric | Signal |
|---|---|
| Cost per pipeline run (by ticker, by step) | Cost control and budget enforcement |
| p50/p95 latency per node | Identifies the bottleneck step |
| Guardrail failure rate by check type | Detects prompt degradation over time |
| Confidence distribution over time | Detects overconfidence drift |
| Retry rate per step | Signals model or schema instability |
| RAG chunks retrieved vs. post-rerank | Measures retrieval efficiency |
| Pipeline cancellation rate | Overall system health |
| Gate pass rate (Layer 2) | Signals whether threshold needs tuning |

### 12.3 Eval Store

Every completed Layer 1 output is written to a Postgres table. This is the primary input for the self-improvement loop, calibration curves, and feature attribution on misses.

```sql
run_id              UUID PRIMARY KEY,
ticker              TEXT,
date_window_start   DATE,
date_window_end     DATE,
direction           TEXT,
magnitude_bucket    TEXT,
confidence          FLOAT,
dominant_drivers    TEXT[],
invalidation_condition TEXT,
hold_window_bucket  TEXT,
thesis              TEXT,
-- ground truth (populated after hold window closes)
actual_direction    TEXT,
actual_magnitude    FLOAT,
hit                 BOOLEAN,
-- debug inputs
input_snapshot      JSONB,   -- the full composed prompt inputs
persona_outputs     JSONB,   -- all four persona responses
judge_reasoning     TEXT,
guardrail_checks    JSONB,   -- each check name + result
-- Layer 2
gate_passed         BOOLEAN,
position_card       JSONB,
-- meta
created_at          TIMESTAMPTZ,
langsmith_trace_url TEXT      -- link to full trace for drill-down
```

### 12.4 Human Eval UI

The human eval view is a read-only dashboard over the eval store plus LangSmith trace links. You do not need to build a custom trace viewer — LangSmith handles that. Build only:

- Table of recent runs: ticker, stated confidence, direction, hit/miss (once ground truth is populated)
- Calibration chart: stated confidence bucket (90% / 70% / 50%) vs. actual hit rate per bucket
- Miss drill-down: click a run → opens LangSmith trace + shows persona outputs side by side
- Feature attribution summary: which input bucket (fundamental / macro / sentiment / technical) correlates most with wrong calls, per sector

### 12.5 Recommended Stack by Phase

| Phase | Tracing | Metrics | Eval Store |
|---|---|---|---|
| Development | LangSmith | LangSmith dashboard | Postgres (local) |
| Early production | LangSmith | Grafana + Postgres | Postgres |
| Multi-tenant production | LangSmith | Datadog | Postgres (per-tenant schema) |

LangSmith alone covers 80% of needs in development. Add Postgres eval store when the self-improvement loop starts. Add Grafana/Datadog when going multi-tenant.

---

## 13. Build Order (Recommended)

1. **RAG retrieval + relevance filter** — build and eval in isolation before wiring to reasoning
2. **Deterministic data pull** — structured numeric block from EDGAR + FRED
3. **Prompt composition** — combine numeric block + RAG chunks
4. **Multi-persona reasoning + judge** — wire parallel calls and synthesis
5. **Layer 1 guardrail pass** — schema validation + internal consistency
6. **Layer 2 position sizing** — gate + Kelly sizing + position card
7. **Observability** — LangSmith tracing + eval store schema + basic metrics
8. **Eval loop** — golden set + calibration tracking + regression suite
9. **Self-improvement loop** — automated prompt tuning and corpus rewriting
10. **Multi-tenancy + production hardening**

---

## 14. Open Questions

- How to generate the initial corpus (bootstrapping the markdown files for RAG)?
- What reranker model fits the latency and cost budget?
- Where does the vector store live (self-hosted vs. managed)?
- What is the action confidence threshold for Layer 2 gating?
- How often does the self-improvement loop run (per quarter, per month)?
