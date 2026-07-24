# Documentation Directory

This file maps every spec document in this repository, explains what it covers, and references the relevant section of the PRD. Read `PRD.md` first — all other files are detailed implementations of sections within it.

---

## PRD.md — Product Requirements Document

The source of truth. Defines the problem, the full pipeline architecture, data sources, eval framework, and build order. Every other file in this directory is a deep-dive into one section of the PRD.

Start here before reading anything else.

---

## Pipeline Files

### CORPUS_GENERATOR.md
**What it covers:** Generating and maintaining the markdown files that feed the RAG pipeline.

**PRD reference:** Section 6 (RAG Corpus Organization), Section 13 Open Questions (how to bootstrap the corpus)

**Key decisions documented:**
- File naming convention and required frontmatter schema per file
- Pull logic for each source: NewsAPI, GDELT, FMP analyst ratings, StockTwits, Reddit, FRED
- Optional LLM tagging pass (cheap model, batched) that adds `sentiment_label` and `relevance_tags` to frontmatter — this is what makes the retrieval filter effective
- Two run modes: **backfill** (historical, idempotent) and **live** (daily cron)
- Deduplication and quality rules — bad files go to `corpus/_rejected/`, never deleted, never indexed

**Inputs:** ticker list, date range, source config
**Outputs:** markdown files under `corpus/{news,analyst,social,macro}/`

---

### DATA_PULL_PIPELINE.md
**What it covers:** The deterministic, no-LLM data pull that produces the structured numeric block — the "hard numbers" half of the composed prompt.

**PRD reference:** Section 5.2 Step 1 (Deterministic Data Pull)

**Key decisions documented:**
- Full field list for fundamentals (EPS, revenue, P/E, corporate actions) with exact FMP and EDGAR API calls
- Sector-to-indicator mapping for FRED macro pull — a REIT gets mortgage rates, a semiconductor gets yield curve data; sector resolved from FMP profile
- FOMC event detection as a separate pull (not a FRED series)
- Fixed plain-text template for the numeric block — not JSON, not prose, not summarized by an LLM
- Data gap handling: missing fields are stated explicitly in a NOTES section rather than silently omitted
- Caching strategy: historical data cached permanently; current quarter at 24h TTL
- LangGraph node runs fundamentals and macro pulls in parallel via `asyncio.gather`; partial failures degrade gracefully

**Inputs:** ticker, window_start, window_end
**Outputs:** `NumericBlock` (rendered text + metadata)

---

### RAG_PIPELINE.md
**What it covers:** Indexing the corpus into a vector store and retrieving relevant chunks for a given ticker and date window.

**PRD reference:** Section 5.2 Step 2 (RAG Retrieval + Relevance Filtering), Section 6 (RAG Corpus Organization)

**Key decisions documented:**
- Chunking: 400 tokens, 50 overlap, sentence-aware splitter; frontmatter stored as metadata, not embedded
- Embedding model: `text-embedding-3-small` by default; vector store record schema
- Vector store recommendation: Qdrant for production (first-class metadata filtering), Chroma for local dev
- Four-stage retrieval: hard metadata filter → embedding search (top-50) → cross-encoder rerank (top-10) → sentiment tag pass
- Hard filter is the primary solution to "not all news is relevant" — most irrelevance eliminated before any model runs
- If all top-10 chunks score below the reranker threshold, returns empty block rather than injecting noise
- Output: `QualitativeBlock` rendered as condensed bullets grouped by source type, capped at 1,500 tokens
- Retrieval eval metrics: Recall@10 > 0.80, Precision@10 > 0.60, MRR > 0.70
- Incremental indexing: only new files on each corpus generator run; soft-delete for rejected files

**Inputs:** ticker, window_start, window_end, optional thesis_hint
**Outputs:** `QualitativeBlock` (formatted qualitative context for prompt)

---

### REASONING_PIPELINE.md
**What it covers:** Prompt composition, multi-persona reasoning, and judge synthesis — Steps 3–5 of the Layer 1 judgment pipeline.

**PRD reference:** Section 5.2 Steps 3–5, Section 9.4 (Model Routing)

**Key decisions documented:**
- Prompt structure: numeric block + qualitative block + output instruction, with token budgets per section (800 / 1,500 / 300)
- Four persona system prompts written out in full: bull, bear, macro-only, pure-technicals — each constrained to its perspective, cannot invent facts
- Persona calls fire concurrently via `asyncio.gather`; a failed persona is logged but does not cancel the pipeline
- Judge system prompt forces the judge to name which personas it sides with and why
- Judge uses `tool_choice={"type":"tool","name":"submit_verdict"}` — cannot respond in prose
- `Layer1Output` Pydantic schema with field-level validators (at least one driver, invalidation not vague)
- `sided_with` and `sided_reasoning` fields stored in eval store for feature attribution analysis
- Model routing: personas → `claude-sonnet-4-6`; judge → `claude-opus-4-6`

**Inputs:** `NumericBlock`, `QualitativeBlock`
**Outputs:** `Layer1Output` (direction, magnitude_bucket, confidence, dominant_drivers, invalidation_condition, hold_window_bucket, thesis)

---

### GUARDRAIL_PIPELINE.md
**What it covers:** All guardrail stages: input validation, runtime checks, output consistency checks, retry logic, and pipeline cancellation.

**PRD reference:** Section 9.3 (Agent Guardrails), Section 5.2 Step 6 (Guardrail Pass)

**Key decisions documented:**
- **Input guardrails:** ticker format + existence check, date range validation, prompt injection detection (regex patterns), per-user rate limiting
- **Runtime guardrails:** chunk scope re-validation after retrieval (defends against vector store filter bugs), context size enforcement before LLM calls
- **Output guardrails (four checks):**
  1. Pydantic schema validation
  2. Citation grounding — `dominant_drivers` must appear in the actual input data
  3. Confidence consistency — confidence capped by persona agreement ratio (50/50 split → max 0.65 confidence)
  4. Invalidation concreteness — must reference a price level, percentage, or named event; vague phrases rejected
- Retry logic: max 2 retries; each retry appends the specific failure reason to the judge prompt
- Decision table: which failures trigger retry vs. cancel vs. degraded run
- Pipeline cancellation: cancellation record written to eval store; Layer 2 never receives control
- All `guardrail_checks` entries logged to observability and eval store

**Inputs:** `PipelineState` at each stage
**Outputs:** Modified `PipelineState` with guardrail check results, retry notes, or cancellation flag

---

### LAYER2_PIPELINE.md
**What it covers:** The position sizing pipeline — gate check, fractional Kelly sizing, and position card generation. Fully deterministic, no LLM.

**PRD reference:** Section 5.3 (Layer 2 — Position Sizing Pipeline)

**Key decisions documented:**
- Gate conditions: confidence > 0.60 AND magnitude_bucket != "0-3%"
- Volatility input: 20-day realized volatility annualized from Polygon daily closes
- Fractional Kelly formula with worked example: `f_kelly = f_full × 0.25 × vol_scalar`
- Confidence and position size are NOT a straight line — high-vol names shrunk even at high confidence
- Position size constraints: min 2%, max 15%, max 60% total portfolio exposure
- Target price: magnitude bucket midpoint applied to entry (e.g., "3-8%" → 5.5% applied)
- Stop-loss translation: price-based (`$142`) vs. event-based (`if CPI exceeds 3.5%`) invalidation conditions handled separately
- Hold window: bucket mapped to day range, midpoint used as actual date
- `PositionCard` Pydantic schema with all sizing audit fields (`kelly_full`, `kelly_fractional`, `volatility_used`)

**Inputs:** `Layer1Output`, ticker, window_start
**Outputs:** `PositionCard` (entry, target, stop, hold window, position size, thesis)

---

### EVAL_PIPELINE.md
**What it covers:** The evaluation framework and self-improvement loop — how the system measures calibration, catches regressions, and updates itself between quarters.

**PRD reference:** Section 7 (Eval Framework), Section 7.3 (Self-Improvement Loop)

**Key decisions documented:**
- Five-tier eval hierarchy: golden set → regression suite → adversarial → LLM-as-judge → human spot-check
- Golden set structure: minimum 50 entries, frozen inputs, manually labeled dominant driver, lookahead verified
- Regression suite: triggered by any prompt/model/parameter change; blocks deploy if hit rate drops > 5pp or cancellation rate rises > 10pp
- Adversarial scenarios: conflicting signals, misleading headlines, social pump, macro noise during earnings, empty RAG block, stale data
- LLM-as-judge scoring (1–5 per dimension): evidence grounding, driver attribution, confidence calibration, invalidation quality, thesis clarity
- Calibration curve: requires minimum 10 samples per confidence bucket; interpretation table with corrective actions
- Feature attribution: which input driver correlates with misses → maps directly to prompt weight adjustments
- Self-improvement loop: processes one quarter at a time, generates improvement actions, applies them as prompt/corpus/parameter commits, runs regression before advancing
- Prompt versioning: all prompts stored as text files in `prompts/`; every change committed and version-tagged
- Cost estimate: ~$5 per full regression run over 50 golden entries

**Inputs:** Eval store records, golden set, prompt version
**Outputs:** Calibration reports, regression reports, prompt/corpus/parameter updates

---

### DATASET_PIPELINE.md
**What it covers:** Generating the QoQ and YoY training and testing datasets from historical API data.

**PRD reference:** Section 8 (Testing Dataset)

**Key decisions documented:**
- Two entry types: QoQ (adjacent quarters) and YoY (same quarter across two years); same schema for both
- Window definition: earnings release date → next earnings release date (capped at 90 days) — uses fiscal calendar, not calendar quarters
- Lookahead bias prevention: strict date filter on every RAG chunk; fundamentals use report immediately preceding the window, not one released inside it
- Ground truth labeling: price-based labels are automated (Polygon historical close); dominant driver labeling via LLM pass (Haiku, batched) or manual curation
- Data quality score (0.0–1.0): entries below 0.70 excluded from golden set
- Dataset splits by time: train (pre-2023), validation (2023), test (2024+) — never random splits
- Edge cases: halted stocks, acquisitions, splits — all documented with specific handling
- Storage: one JSON file per entry under `datasets/v{version}/entries/`; JSONL split files for streaming
- Dataset versioning: major for structural changes, minor for additive changes

**Inputs:** ticker list, quarter range, source APIs
**Outputs:** Dataset entries under `datasets/`; manifest and split files

---

### OBSERVABILITY_PIPELINE.md
**What it covers:** Tracing, metrics, and the eval store — the three observability layers spanning both pipelines.

**PRD reference:** Section 12 (Observability)

**Key decisions documented:**
- Three independent write targets: LangSmith (per-run trace), Prometheus + Grafana (aggregated metrics), Postgres eval store (self-improvement loop input)
- LangSmith: zero-config with LangGraph via env vars; run metadata tags (ticker, user_id, pipeline_version) make traces filterable; trace URL captured and stored in eval store for linking
- Full Prometheus counter/histogram definitions for: pipeline runs, node latency, token cost, RAG empty block rate, guardrail failure rate, confidence distribution, gate pass rate
- `@observe_node` decorator for automatic timing instrumentation on LangGraph nodes
- Cost estimation table per model (Opus/Sonnet/Haiku) for USD tracking
- Grafana panel specs with alert thresholds (e.g., > $0.50/run, guardrail failure rate > 10%)
- Complete Postgres eval store schema (50+ columns) including all debug fields: persona outputs, guardrail checks, RAG chunks, trace URL
- Ground truth population job: runs daily after hold windows close, computes actual direction/magnitude/hit
- Standard eval queries: calibration curve, feature attribution on misses, price target error, timing analysis, version regression comparison
- Human eval UI: scoped to what needs custom building — run list, calibration chart, miss drill-down, regression table; LangSmith handles trace drill-down
- Stack by phase: LangSmith alone for dev; add Postgres eval store when self-improvement loop starts; add Grafana/Datadog at multi-tenant production

**Inputs:** Every pipeline run (automatic)
**Outputs:** LangSmith traces, Prometheus metrics, Postgres eval records

---

## Reading Order

If you are new to this project:

1. `PRD.md` — architecture, goals, build order
2. `CORPUS_GENERATOR.md` — how the knowledge base is built
3. `DATA_PULL_PIPELINE.md` — how hard numbers are pulled
4. `RAG_PIPELINE.md` — how qualitative context is retrieved
5. `REASONING_PIPELINE.md` — how LLMs reason over the combined context
6. `GUARDRAIL_PIPELINE.md` — how outputs are validated and retried
7. `LAYER2_PIPELINE.md` — how positions are sized
8. `DATASET_PIPELINE.md` — how historical training data is generated
9. `EVAL_PIPELINE.md` — how the system measures and improves itself
10. `OBSERVABILITY_PIPELINE.md` — how every step is instrumented

---

## File Map

```
swing-trader/
  PRD.md                        Product requirements — start here
  DIRECTORY.md                  This file

  CORPUS_GENERATOR.md           Build and maintain the RAG knowledge base
  DATA_PULL_PIPELINE.md         Deterministic fundamentals + macro pull
  RAG_PIPELINE.md               Vector indexing + retrieval + reranking
  REASONING_PIPELINE.md         Prompt composition + personas + judge synthesis
  GUARDRAIL_PIPELINE.md         Input/runtime/output guardrails + retry + cancel
  LAYER2_PIPELINE.md            Position sizing (Kelly formula + position card)
  DATASET_PIPELINE.md           Generate QoQ/YoY training datasets
  EVAL_PIPELINE.md              Calibration, regression, self-improvement loop
  OBSERVABILITY_PIPELINE.md     Tracing, metrics, eval store
```
