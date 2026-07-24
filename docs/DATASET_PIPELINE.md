# Dataset Pipeline — Design Spec

Covers generating the training and testing datasets used by the eval loop and self-improvement process. A dataset entry is a frozen snapshot of all inputs for a given ticker/quarter plus the known ground truth outcome. No lookahead — every input must have been publicly available before the window closes.

---

## 1. Dataset Overview

Two dataset types, both structured the same way:

| Type | Description | Window Definition |
|---|---|---|
| QoQ (Quarter-over-Quarter) | Most recent quarter vs. prior quarter | Earnings release date → next earnings release date (or 90 days max) |
| YoY (Year-over-Year) | Same quarter this year vs. same quarter last year | Same window as QoQ but comparative data from 12 months prior |

Each entry is one row: one ticker, one quarter, one set of frozen inputs, one ground truth outcome.

---

## 2. Entry Schema

```python
from pydantic import BaseModel
from datetime import date
from typing import Literal

class DatasetEntry(BaseModel):
    # Identity
    entry_id: str                          # UUID
    dataset_version: str                   # e.g., "v1.2"
    entry_type: Literal["QoQ", "YoY"]
    ticker: str
    quarter: str                           # e.g., "2023Q4"
    fiscal_quarter: str                    # company's fiscal quarter label if different
    window_start: date                     # start of prediction window
    window_end: date                       # end of prediction window (when we'd close the trade)

    # Frozen inputs — only data available before window_end
    frozen_numeric_block: str              # rendered structured numeric block text
    frozen_fundamentals: dict              # raw fundamentals snapshot (EPS, revenue, etc.)
    frozen_macro: dict                     # raw macro snapshot (FRED series values)
    frozen_rag_chunks: list[dict]          # top-10 chunks after rerank, with metadata
    frozen_corporate_actions: list[str]   # any corporate actions in window

    # Data quality flags
    data_gaps: list[str]                   # fields that could not be populated
    lookahead_violations: list[str]        # chunks or data points that failed date check
    data_quality_score: float              # 0.0–1.0; entries below 0.7 excluded from golden set

    # Ground truth — populated after window_end
    actual_price_at_start: float
    actual_price_at_end: float
    actual_magnitude_pct: float            # (end - start) / start * 100
    actual_direction: Literal["bullish", "bearish", "neutral"]
    actual_magnitude_bucket: Literal["0-3%", "3-8%", "8%+"]
    dominant_driver_label: Literal["fundamental", "macro", "sentiment", "technical"]
    dominant_driver_method: Literal["manual", "llm"]
    dominant_driver_notes: str             # explanation of what actually drove the move

    # Split assignment
    split: Literal["train", "validation", "test"]
    created_at: date
    ground_truth_populated: bool = False
```

---

## 3. Quarter Identification

### 3.1 Calendar vs. Fiscal Quarters

Most companies do not follow the calendar quarter. Fiscal quarter boundaries come from FMP:

```python
async def get_fiscal_quarters(ticker: str) -> list[FiscalQuarter]:
    """
    Returns list of fiscal quarters for the ticker with their earnings release dates.
    """
    data = await fmp_get(f"/v3/earnings-surprises/{ticker}")
    quarters = []
    for item in data:
        quarters.append(FiscalQuarter(
            ticker=ticker,
            fiscal_label=item["period"],           # e.g., "Q4 2023"
            report_date=date.fromisoformat(item["date"]),
            eps_actual=item["actualEarningResult"],
            eps_estimate=item["estimatedEarning"]
        ))
    return sorted(quarters, key=lambda q: q.report_date)
```

### 3.2 Window Definition

The prediction window runs from the earnings release date (when new information becomes public) to the next earnings release date, capped at 90 days:

```python
def compute_window(quarters: list[FiscalQuarter], index: int) -> tuple[date, date]:
    current_report = quarters[index].report_date
    if index + 1 < len(quarters):
        next_report = quarters[index + 1].report_date
        window_end = min(next_report - timedelta(days=1), current_report + timedelta(days=90))
    else:
        window_end = current_report + timedelta(days=90)
    return current_report, window_end
```

---

## 4. Generating QoQ Entries

```python
async def generate_qoq_entry(
    ticker: str,
    quarter_index: int,
    quarters: list[FiscalQuarter]
) -> DatasetEntry | None:

    current_q = quarters[quarter_index]
    prior_q   = quarters[quarter_index - 1] if quarter_index > 0 else None

    window_start, window_end = compute_window(quarters, quarter_index)

    # 1. Pull fundamentals as of window_start
    fundamentals = await pull_fundamentals_as_of(ticker, window_start)
    if fundamentals is None:
        return None  # skip if no data

    # 2. Pull macro for the window
    sector = await get_sector(ticker)
    macro = await pull_macro_for_window(ticker, sector, window_start, window_end)

    # 3. Build numeric block from fundamentals + macro
    numeric_block = build_numeric_block(ticker, window_start, window_end, fundamentals, macro)

    # 4. Retrieve RAG chunks — ONLY articles with date < window_end
    rag_chunks = await retrieve_chunks_as_of(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        max_date=window_end  # hard lookahead guard
    )

    # 5. Lookahead check — reject any chunk published after window_end
    clean_chunks, violations = filter_lookahead(rag_chunks, window_end)

    # 6. Compute data quality score
    quality_score = compute_quality_score(fundamentals, macro, clean_chunks, violations)

    entry = DatasetEntry(
        entry_id=str(uuid4()),
        dataset_version=DATASET_VERSION,
        entry_type="QoQ",
        ticker=ticker,
        quarter=current_q.fiscal_label,
        fiscal_quarter=current_q.fiscal_label,
        window_start=window_start,
        window_end=window_end,
        frozen_numeric_block=numeric_block.rendered_text,
        frozen_fundamentals=fundamentals.model_dump(),
        frozen_macro=macro.model_dump(),
        frozen_rag_chunks=[c.model_dump() for c in clean_chunks],
        frozen_corporate_actions=fundamentals.corporate_actions,
        data_gaps=numeric_block.data_gaps,
        lookahead_violations=violations,
        data_quality_score=quality_score,
        # ground truth populated separately
        ground_truth_populated=False,
        split=assign_split(window_start),
        created_at=date.today()
    )
    return entry
```

### 4.1 QoQ Comparative Data

QoQ entries include prior quarter metrics in the numeric block for comparison (revenue growth, EPS growth). These are computed from the `frozen_fundamentals.prior_quarter` field, not fetched separately.

---

## 5. Generating YoY Entries

YoY entries compare the same fiscal quarter across two consecutive years. Same window definition, but the comparative data is from 12 months prior.

```python
async def generate_yoy_entry(
    ticker: str,
    quarter_index: int,
    quarters: list[FiscalQuarter]
) -> DatasetEntry | None:

    current_q = quarters[quarter_index]

    # Find the matching quarter from prior year
    prior_year_q = find_same_quarter_prior_year(quarters, quarter_index)
    if prior_year_q is None:
        return None

    # Window is the same as QoQ
    window_start, window_end = compute_window(quarters, quarter_index)

    # Pull same data as QoQ — YoY comparison lives in the numeric block template
    fundamentals = await pull_fundamentals_as_of(ticker, window_start, compare_to=prior_year_q.report_date)
    macro = await pull_macro_for_window(ticker, await get_sector(ticker), window_start, window_end)
    numeric_block = build_numeric_block(ticker, window_start, window_end, fundamentals, macro, mode="yoy")
    rag_chunks = await retrieve_chunks_as_of(ticker, window_start, window_end, max_date=window_end)
    clean_chunks, violations = filter_lookahead(rag_chunks, window_end)

    entry = DatasetEntry(
        entry_id=str(uuid4()),
        entry_type="YoY",
        # ... same fields as QoQ ...
    )
    return entry
```

---

## 6. Ground Truth Labeling

### 6.1 Price-Based Labels (Automated)

```python
async def populate_ground_truth(entry: DatasetEntry) -> DatasetEntry:
    price_start = await fetch_close_price(entry.ticker, entry.window_start)
    price_end   = await fetch_close_price(entry.ticker, entry.window_end)

    magnitude_pct = (price_end - price_start) / price_start * 100

    # Direction thresholds
    if magnitude_pct > 1.5:
        direction = "bullish"
    elif magnitude_pct < -1.5:
        direction = "bearish"
    else:
        direction = "neutral"

    # Magnitude bucket
    abs_mag = abs(magnitude_pct)
    if abs_mag < 3:
        bucket = "0-3%"
    elif abs_mag < 8:
        bucket = "3-8%"
    else:
        bucket = "8%+"

    entry.actual_price_at_start  = price_start
    entry.actual_price_at_end    = price_end
    entry.actual_magnitude_pct   = magnitude_pct
    entry.actual_direction       = direction
    entry.actual_magnitude_bucket = bucket
    entry.ground_truth_populated = True
    return entry
```

### 6.2 Dominant Driver Labeling

This requires judgment — which factor actually drove the price move? Two options:

**Option A — LLM pass (fast, scalable):**

```python
async def label_dominant_driver_llm(entry: DatasetEntry) -> str:
    prompt = f"""
A stock moved {entry.actual_magnitude_pct:+.1f}% from {entry.window_start} to {entry.window_end}.

Available context at the time:
{entry.frozen_numeric_block}

Top news/sentiment chunks:
{format_top_chunks(entry.frozen_rag_chunks[:3])}

The stock moved {entry.actual_direction} by {entry.actual_magnitude_bucket}.

Which single factor most explains this move?
Options: fundamental | macro | sentiment | technical

Respond with one word only: fundamental, macro, sentiment, or technical.
"""
    response = await claude_call(prompt, model="claude-haiku-4-5-20251001", max_tokens=10)
    return response.strip().lower()
```

**Option B — Manual labeling:**

Present the curator with: frozen inputs, actual outcome, and a 4-option radio button. Curators review 10–20 entries per session. Used for the golden set; LLM labeling used for the broader training corpus.

### 6.3 Edge Cases

| Scenario | Handling |
|---|---|
| Stock halted during window | Use last available price before halt as `actual_price_at_end`; add flag `halted=true` |
| Acquisition announced in window | Use acquisition price as `actual_price_at_end`; label driver as `fundamental`; add `acquisition=true` flag |
| Stock splits in window | Adjust prices for split before computing magnitude |
| Window < 5 trading days of data | Mark `data_quality_score < 0.5`; exclude from golden set |

---

## 7. Lookahead Bias Prevention

This is the most critical correctness requirement. Any post-window data in the frozen inputs invalidates the entry.

```python
def filter_lookahead(
    chunks: list[QualItem],
    window_end: date
) -> tuple[list[QualItem], list[str]]:
    clean = []
    violations = []
    for chunk in chunks:
        chunk_date = date.fromisoformat(chunk.date)
        if chunk_date >= window_end:
            violations.append(
                f"Chunk {chunk.id} dated {chunk.date} is on or after window_end {window_end}"
            )
        else:
            clean.append(chunk)
    return clean, violations
```

Also applies to fundamentals: ensure earnings report date < window_end (use the report immediately preceding the window, not the one released during it if it falls inside).

---

## 8. Data Quality Score

```python
def compute_quality_score(
    fundamentals: FundamentalsResult,
    macro: MacroResult,
    chunks: list[QualItem],
    violations: list[str]
) -> float:
    score = 1.0

    # Deduct for missing fundamentals
    if fundamentals.eps_actual is None:    score -= 0.2
    if fundamentals.revenue_actual is None: score -= 0.1
    if fundamentals.pe_trailing is None:   score -= 0.05

    # Deduct for missing macro
    if not macro.series_pulled:            score -= 0.1

    # Deduct for no RAG chunks
    if len(chunks) == 0:                   score -= 0.2
    elif len(chunks) < 3:                  score -= 0.1

    # Deduct for lookahead violations (should be 0 after filtering)
    score -= len(violations) * 0.05

    return max(0.0, round(score, 2))
```

Entries with `data_quality_score < 0.70` are excluded from the golden set and regression suite. They remain in the broader training corpus but are flagged.

---

## 9. Dataset Splits

Always split by time — never randomly. Training on future data to predict the past is lookahead bias at the dataset level.

```python
def assign_split(window_start: date) -> str:
    if window_start < date(2023, 1, 1):
        return "train"
    elif window_start < date(2024, 1, 1):
        return "validation"
    else:
        return "test"
```

Default split boundaries (update as more data accumulates):
- **Train:** everything before 2023
- **Validation:** 2023 (used for self-improvement loop tuning)
- **Test:** 2024+ (held out; used only for final evaluation)

Never tune prompts or parameters based on test set performance.

---

## 10. Storage Layout

```
datasets/
  v1.2/
    entries/
      {entry_id}.json          # one file per entry (all fields)
    manifest.json              # index: [{entry_id, ticker, quarter, type, split, quality_score}]
    splits/
      train.jsonl              # entry_ids in train split (JSONL for streaming)
      validation.jsonl
      test.jsonl
    stats.json                 # dataset statistics: count by split/type/ticker/quarter
  _archive/
    v1.0/
    v1.1/
```

### JSONL Format (for streaming large datasets)

```jsonl
{"entry_id": "...", "ticker": "AAPL", "quarter": "2023Q4", ...}
{"entry_id": "...", "ticker": "MSFT", "quarter": "2023Q4", ...}
```

---

## 11. Dataset Versioning

A new dataset version is created when:
- New tickers are added to the corpus
- Ground truth labels are corrected
- Lookahead violations are found and fixed
- Split boundaries are updated

Version format: `v{major}.{minor}` — major for structural changes (new fields, split boundary changes), minor for additive changes (new entries, label corrections).

The pipeline version in the eval store references the dataset version used: `pipeline_runs.dataset_version`.

---

## 12. Generation Script

```python
async def generate_dataset(
    tickers: list[str],
    start_quarter: str,   # e.g., "2021Q1"
    end_quarter: str,     # e.g., "2024Q4"
    types: list[str] = ["QoQ", "YoY"]
) -> None:

    for ticker in tickers:
        quarters = await get_fiscal_quarters(ticker)
        quarters = filter_quarters(quarters, start_quarter, end_quarter)

        for i, quarter in enumerate(quarters):
            for entry_type in types:
                if entry_type == "QoQ" and i > 0:
                    entry = await generate_qoq_entry(ticker, i, quarters)
                elif entry_type == "YoY" and i >= 4:  # need 4 quarters back for YoY
                    entry = await generate_yoy_entry(ticker, i, quarters)
                else:
                    continue

                if entry and entry.data_quality_score >= 0.50:
                    save_entry(entry)

        print(f"Done: {ticker} — {len(quarters)} quarters processed")
```

---

## 13. Implementation Notes

- **Generation time:** ~2–5 minutes per ticker for a 3-year backfill (dominated by API rate limits). Run overnight for large ticker lists
- **LLM labeling cost:** ~$0.001 per entry with Claude Haiku. 500 entries = ~$0.50. Negligible
- **Idempotency:** check `manifest.json` before generating — skip entries that already exist
- **Parallel generation:** use `asyncio.Semaphore(3)` across tickers; within a ticker, quarters are sequential (each depends on prior quarter for QoQ)
- **Storage:** each entry JSON is ~5–15 KB. 1,000 entries ≈ 10 MB. No compression needed at this scale
- **API dependencies:** FMP for fundamentals and fiscal calendar; FRED for macro; Polygon for price data; vector store for RAG chunks. All should hit the cache first (see DATA_PULL_PIPELINE.md Section 6)
