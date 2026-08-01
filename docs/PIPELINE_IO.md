# Pipeline Input/Output Specification

Every LLM call, data input, and expected output across the full pipeline.

```
ticker + dates ──► Data Pull ──► RAG Retrieval ──► Persona Reasoning (x4) ──► Judge ──► Eval Store
                   (no LLM)      (LLM: tag)        (LLM: mid)                (LLM: judge)
```

---

## Stage 1: Data Pull

**No LLM calls.** Pure API fetches + caching.

### Input

| Field | Type | Example |
|-------|------|---------|
| `ticker` | `str` | `"AAPL"` |
| `window_start` | `date` | `2024-01-01` |
| `window_end` | `date` | `2024-03-31` |

### Output: `NumericBlock`

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `str` | Ticker symbol |
| `window_start` | `date` | Window start |
| `window_end` | `date` | Window end |
| `rendered_text` | `str` | Markdown tables (fundamentals + macro) |
| `data_gaps` | `list[str]` | Missing data points |
| `sources_used` | `list[str]` | e.g. `["yfinance", "FRED"]` |
| `fundamentals_report_date` | `date \| None` | Earnings date in window |
| `macro_series_pulled` | `list[str]` | e.g. `["CPIAUCSL", "FEDFUNDS"]` |

`rendered_text` example (what downstream stages see):

```markdown
## Fundamentals — AAPL (Q1 2024)
| Metric            | Value   |
|--------------------|---------|
| EPS actual         | 2.18    |
| EPS estimate       | 2.10    |
| Earnings surprise  | +3.8%   |
| Revenue            | $119.6B |
| Gross margin       | 45.9%   |
| P/E trailing       | 30.2    |
| P/E forward        | 28.1    |
| Price (window start)| $185.64|
| 52-week high       | $199.62 |
| 52-week low        | $164.08 |

## Macro Environment
| Series         | Start  | End    | Unit |
|----------------|--------|--------|------|
| CPI            | 3.4%   | 3.5%   | %    |
| Fed Funds Rate | 5.33%  | 5.33%  | %    |
| Unemployment   | 3.7%   | 3.8%   | %    |

FOMC: Meeting 2024-01-31, decision: hold
```

---

## Stage 2: Corpus Tagging (batch, runs during corpus generation)

### Input

Batch of raw corpus articles (up to 20 per call).

```json
[
  {"ticker": "AAPL", "text": "Apple reported Q1 earnings beating estimates..."},
  {"ticker": "AAPL", "text": "Morgan Stanley upgrades AAPL to overweight..."}
]
```

### LLM Call

| Field | Value |
|-------|-------|
| **Model** | `llama-3.2-3b-preview` (Groq) |
| **Temperature** | 0 (deterministic) |
| **Max tokens** | ~600 |

**Prompt:**

```
You are tagging financial news items for a swing trading RAG corpus.

For each item return exactly:
{"relevance_tags": [...], "sentiment_label": "bullish|bearish|neutral", "sentiment_reason": "one sentence"}

Valid relevance_tags values: ['analyst_rating', 'corporate_action', 'earnings',
'geopolitics', 'macro', 'sentiment', 'technical']
Set relevance_tags to [] if the item is not meaningfully about the ticker.

Items:
[{ticker, text}, ...]

Return a JSON array in the same order, one object per item.
```

### Expected Output

```json
[
  {
    "relevance_tags": ["earnings", "sentiment"],
    "sentiment_label": "bullish",
    "sentiment_reason": "Company beat EPS expectations by 3.8%"
  },
  {
    "relevance_tags": ["analyst_rating"],
    "sentiment_label": "bullish",
    "sentiment_reason": "Major bank upgrade signals institutional confidence"
  }
]
```

---

## Stage 3: RAG Retrieval

### Input

| Field | Type | Example |
|-------|------|---------|
| `ticker` | `str` | `"AAPL"` |
| `window_start` | `date` | `2024-01-01` |
| `window_end` | `date` | `2024-03-31` |
| `thesis_hint` | `str \| None` | `"AI iPhone cycle"` |

### Internal Steps (mostly non-LLM)

1. **Hard filter** — ticker, date range, `active=True`
2. **Embedding search** — query: `"{ticker} stock price movement drivers {window_start} to {window_end}"` (+ thesis_hint if present). Top 50 by cosine similarity.
3. **Cross-encoder rerank** — `cross-encoder/ms-marco-MiniLM-L-6-v2`, keep top 10 above score 0.3, deduplicate by file.
4. **Sentiment tag** (LLM, only if chunk missing sentiment) — same model/format as corpus tagging but per-chunk:

**Prompt (if needed):**

```
Ticker: AAPL

Tag each item below as it relates to the stock's price outlook.
Return a JSON array in the same order as the items.

Items:
["Apple reported Q1 earnings...", "Morgan Stanley upgrades..."]

For each item return exactly:
{"sentiment_label": "bullish|bearish|neutral", "sentiment_reason": "one sentence"}
```

### Output: `QualitativeBlock`

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | `str` | Ticker symbol |
| `window_start` | `date` | Window start |
| `window_end` | `date` | Window end |
| `chunks_retrieved` | `int` | Total chunks from vector search |
| `chunks_used` | `int` | After reranking + dedup |
| `items` | `list[QualItem]` | Ranked context items |

Each `QualItem`:

```json
{
  "source_type": "analyst",
  "date": "2024-02-05",
  "source": "Financial Modeling Prep",
  "sentiment_label": "bullish",
  "sentiment_reason": "Major bank upgrade signals institutional confidence",
  "summary": "Morgan Stanley upgrades AAPL to overweight with $220 PT...",
  "relevance_score": 0.87
}
```

---

## Stage 4: Prompt Composition

**No LLM call.** Assembles `NumericBlock.rendered_text` + `QualitativeBlock.render()` into a single string.

### Output: `composed_prompt` (~2,600 tokens)

Token budget:
- Numeric block: 800 tokens (never truncated)
- Qualitative block: 1,500 tokens (truncated by priority: analyst > news > macro > social)
- Output instruction: 300 tokens

---

## Stage 5: Persona Reasoning (4 parallel calls)

### Input

The `composed_prompt` string from Stage 4, identical for all four personas.

### LLM Calls (x4, concurrent)

| Field | Value |
|-------|-------|
| **Model** | `llama-3.1-8b-instant` (Groq) |
| **Temperature** | 0.7 |
| **Max tokens** | 300 |

Each persona gets the same **user prompt**:

```
Analyze the following data for a swing trade assessment:

{composed_prompt}
```

But a different **system prompt**:

#### BULL

```
You are a bullish equity analyst. Your job is to find the strongest
case FOR buying this stock in the given window. Emphasize upside
catalysts, positive earnings surprises, improving fundamentals, and
favorable sentiment. Cite specific numbers from the data provided.
Keep your response to 3-5 concise sentences.
```

#### BEAR

```
You are a bearish equity analyst. Your job is to find the strongest
case AGAINST buying this stock in the given window. Emphasize downside
risks, deteriorating fundamentals, overvaluation, and negative
sentiment. Cite specific numbers from the data provided.
Keep your response to 3-5 concise sentences.
```

#### MACRO

```
You are a macroeconomic strategist. Evaluate this stock purely through
the lens of macro conditions: interest rates, inflation, GDP, sector
rotation, and Fed policy. Determine whether macro tailwinds or
headwinds dominate for this name. Cite specific macro data points
provided. Keep your response to 3-5 concise sentences.
```

#### TECHNICALS

```
You are a technical analyst focused on price action and valuation
multiples. Assess whether the stock appears overbought or oversold
based on the valuation data, price levels relative to 52-week range,
and any momentum signals available. State a directional lean with
reasoning. Keep your response to 3-5 concise sentences.
```

### Expected Output (each persona)

```
- AAPL beat EPS by 3.8% with revenue of $119.6B, showing strong demand.
- Gross margin expanded to 45.9%, signaling improving mix.
- Morgan Stanley upgrade to overweight with $220 PT adds institutional support.

VERDICT:
Direction: bullish
Magnitude: 3-8%
Dominant driver: fundamental
```

### Output: `PersonaOutputs`

```python
PersonaOutputs(
    bull="...",       # raw text + verdict
    bear="...",
    macro="...",
    technicals="..."
)
```

---

## Stage 6: Judge Synthesis

### Input

All four persona outputs + the original composed data prompt.

### LLM Call

| Field | Value |
|-------|-------|
| **Model** | `llama-3.3-70b-versatile` (Groq) — spec calls for `claude-opus-4-6` |
| **Temperature** | 0 |
| **Tool use** | `submit_verdict` function call |

**System prompt:**

```
You are a senior portfolio manager synthesizing four analyst perspectives into
one trade verdict.

You will receive four arguments: bull case, bear case, macro view, and technicals view.
Your job is to:
1. Weigh all arguments against the underlying data
2. Side with the argument(s) best supported by the evidence
3. Name which argument(s) you are siding with and exactly why
4. Produce one structured verdict using the tool provided

Rules:
- Your confidence must reflect how much the arguments agree. If bull and bear are
  evenly matched, confidence cannot exceed 0.65. If three of four agree, confidence
  can be 0.80+. If all four agree, confidence can reach 0.90.
- The invalidation_condition must be a specific, checkable event or price level.
  "If fundamentals deteriorate" is not acceptable. "If next CPI print exceeds 3.5%"
  or "if price breaks below $142" are acceptable.
- dominant_drivers must only list drivers that appear in the provided input data.
  Do not cite information not present in the structured data or qualitative context.
- The thesis should be 2-4 sentences suitable for a trade journal entry.
```

**User prompt:**

```
## Data
{composed_prompt}

## Bull Case
{persona_outputs.bull}

## Bear Case
{persona_outputs.bear}

## Macro View
{persona_outputs.macro}

## Technicals View
{persona_outputs.technicals}
```

**Tool schema (`submit_verdict`):**

```json
{
  "name": "submit_verdict",
  "input_schema": {
    "type": "object",
    "required": ["direction", "magnitude_bucket", "confidence", "dominant_drivers",
                  "invalidation_condition", "hold_window_bucket", "thesis",
                  "sided_with", "sided_reasoning"],
    "properties": {
      "direction": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
      "magnitude_bucket": {"type": "string", "enum": ["0-3%", "3-8%", "8%+"]},
      "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
      "dominant_drivers": {
        "type": "array",
        "items": {"type": "string", "enum": ["fundamental", "macro", "sentiment", "technical"]}
      },
      "invalidation_condition": {"type": "string"},
      "hold_window_bucket": {"type": "string", "enum": ["days", "weeks", "quarter"]},
      "thesis": {"type": "string"},
      "sided_with": {
        "type": "array",
        "items": {"type": "string", "enum": ["bull", "bear", "macro", "technicals"]}
      },
      "sided_reasoning": {"type": "string"}
    }
  }
}
```

### Expected Output: `Layer1Output`

```json
{
  "direction": "bullish",
  "magnitude_bucket": "3-8%",
  "confidence": 0.78,
  "dominant_drivers": ["fundamental", "sentiment"],
  "invalidation_condition": "If price breaks below $175 or next quarter EPS misses by >5%",
  "hold_window_bucket": "weeks",
  "thesis": "AAPL's 3.8% EPS beat and expanding gross margins signal improving fundamentals. The Morgan Stanley upgrade adds institutional backing. Macro headwinds from elevated rates are real but insufficient to offset the earnings momentum.",
  "sided_with": ["bull", "technicals"],
  "sided_reasoning": "Bull and technicals cases are best supported by the concrete earnings beat and valuation data; bear case relies on general overvaluation concerns without a specific catalyst."
}
```

**Validation rules (Pydantic):**
- `invalidation_condition` rejects vague phrases like "if fundamentals deteriorate"
- `dominant_drivers` rejects duplicates
- `confidence` clamped to 0.0–1.0

---

## Stage 7: Layer 2 — Gate & Position Sizing

**No LLM call.** Pure arithmetic.

### Input: `Layer1Output`

### Gate Check

```python
CONFIDENCE_THRESHOLD = 0.60
NEGLIGIBLE_BUCKETS = {"0-3%"}

passed = (confidence >= 0.60) and (magnitude_bucket not in {"0-3%"})
```

### Position Sizing (Kelly Criterion)

```python
KELLY_FRACTION = 0.25        # quarter-Kelly
MAX_POSITION_SIZE = 0.15     # 15% of portfolio

f_kelly = (p * b - q) / b    # where p=confidence, q=1-p, b=gain/loss ratio
position_size = min(f_kelly * KELLY_FRACTION * vol_scalar, MAX_POSITION_SIZE)
vol_scalar = max(0.2, 1.0 - volatility)
```

### Output: `PositionCard`

```json
{
  "gate_passed": true,
  "gate_skip_reason": null,
  "ticker": "AAPL",
  "entry_price": 185.64,
  "target_price": 198.00,
  "stop_loss": "$175.00",
  "hold_window_start": "2024-01-02",
  "hold_window_end": "2024-02-15",
  "position_size_pct": 0.08,
  "kelly_full": 0.34,
  "kelly_fractional": 0.085,
  "volatility_used": 0.22,
  "thesis": "..."
}
```

---

## Stage 8: Eval Store & Harness

**No LLM call.** Writes to PostgreSQL, runs offline analysis.

### Input: Full `PipelineState`

Everything from all stages is persisted to `pipeline_runs`:

| Column Group | Fields |
|--------------|--------|
| **Identity** | `run_id`, `ticker`, `window_start`, `window_end` |
| **Layer 1** | `direction`, `magnitude_bucket`, `confidence`, `dominant_drivers`, `invalidation_condition`, `hold_window_bucket`, `thesis`, `sided_with`, `sided_reasoning` |
| **Layer 2** | `gate_passed`, `gate_skip_reason`, `entry_price`, `target_price`, `stop_loss`, `position_size_pct`, `kelly_full`, `kelly_fractional`, `volatility_used` |
| **Inputs** | `numeric_block_text`, `data_gaps`, `sources_used`, `rag_chunks_retrieved`, `rag_chunks_used`, `rag_top_chunks` |
| **Debug** | `persona_bull_output`, `persona_bear_output`, `persona_macro_output`, `persona_technicals_output`, `judge_reasoning` |
| **Guardrails** | `guardrail_checks`, `guardrail_retries`, `pipeline_cancelled`, `cancellation_reason`, `warnings` |

### Eval Harness Functions

| Function | Input | Output |
|----------|-------|--------|
| `calibration_curve()` | All runs | Hit rate per confidence bucket (is 0.7 confidence right 70% of the time?) |
| `feature_attribution()` | All runs | Per-driver miss rate (which `dominant_driver` is least reliable?) |
| `regression_comparison()` | Two version tags | Side-by-side accuracy, confidence, and Brier score |
| `timing_distribution()` | All runs | Counts of early / on-time / late / never-hit trades |

### Self-Improvement Loop

```
1. Run backfill:  python main.py AAPL 2024-Q1 --run-type backfill
2. Score runs:    compare predicted direction/magnitude vs actual price move
3. Eval harness:  calibration_curve() + feature_attribution()
4. Adjust:        tune confidence thresholds, persona prompts, or gate params
5. Re-run:        regression_comparison(v1, v2) to verify improvement
```

---

## Full State Object

```python
class PipelineState(TypedDict, total=False):
    # --- Inputs ---
    run_id: str
    ticker: str
    window_start: date
    window_end: date
    user_id: str | None
    run_type: str                    # "live" | "backfill" | "eval"
    thesis_hint: str | None

    # --- Intermediate ---
    numeric_block: NumericBlock
    qualitative_block: QualitativeBlock
    composed_prompt: str
    persona_outputs: PersonaOutputs
    judge_reasoning: str

    # --- Outputs ---
    layer1_output: Layer1Output
    layer2_output: PositionCard

    # --- Guardrails ---
    guardrail_checks: list[GuardrailCheck]
    guardrail_retries: int
    guardrail_retry_notes: list[str]

    # --- Health ---
    warnings: list[str]
    pipeline_cancelled: bool
    cancellation_reason: str | None
    langsmith_trace_url: str | None
```
