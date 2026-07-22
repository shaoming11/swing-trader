# Golden Set — Curation Guide

The golden set is the primary source of truth for evaluating the pipeline.
It must be curated manually. It is frozen once entries are committed — do not
retroactively change ground truth labels after a pipeline version has been
evaluated against them.

**Minimum size:** 50 entries before calibration curves are statistically meaningful.
**Target:** 10+ entries per confidence bucket (90%+, 70-85%, 50-65%, <50%).

---

## Entry Structure

Each entry is a JSON file under `datasets/golden_set/entries/`.
File naming: `{TICKER}_{YYYY}Q{Q}_{optional_note}.json`

Example: `AAPL_2024Q1.json`, `TSLA_2023Q3_post_earnings.json`

See `template.json` for the full schema.

---

## Required Fields

Every entry must have all of the following filled in before it is used in eval:

| Field | What it represents |
|---|---|
| `ticker` | Stock ticker |
| `window_start` / `window_end` | Analysis window (usually earnings quarter) |
| `direction` | What the pipeline should have predicted: bullish / bearish / neutral |
| `magnitude_bucket` | 0-3% / 3-8% / 8%+ |
| `confidence` | What confidence score is appropriate for this setup |
| `dominant_drivers` | Which input type actually drove the move |
| `invalidation_condition` | A concrete, checkable condition that would have invalidated the thesis |
| `actual_direction` | What actually happened |
| `actual_magnitude_pct` | Actual % price move over the hold window |
| `hit` | true / false — did direction + magnitude bucket match? |
| `dominant_driver_label_method` | `manual` or `llm_haiku` |
| `data_quality_score` | 0.0–1.0; exclude entries below 0.70 from eval |

---

## Lookahead Bias Rules

**Critical:** Every field in a golden set entry must represent information
that was available at `window_start`. Violation silently inflates accuracy.

- `direction` / `thesis`: based only on info available at or before `window_start`
- `dominant_drivers`: manually labeled based on what information was public at `window_start`
- `actual_*` fields: populated from price data at or after `window_end` — these are ok to know in advance
- Never label `dominant_drivers` based on what actually moved the stock — label based on what the evidence suggested going in

---

## Labeling Dominant Drivers

The `dominant_drivers` field must be labeled by a human (or verified LLM pass).
This is the most error-prone field — bad labels here corrupt feature attribution.

Rules:
1. Choose the driver that most clearly explains why the setup existed **at entry time**
2. Multi-driver is OK (e.g., `["fundamental", "macro"]`) — cap at 2 unless very clear
3. If uncertain, label `manual_uncertain: true` in the notes field

Driver definitions:
- `fundamental`: EPS beat/miss, revenue surprise, guidance change, corporate action
- `macro`: Fed decision, CPI print, GDP report, yield curve move
- `sentiment`: Analyst upgrade/downgrade, Reddit/social momentum, options flow
- `technical`: Volume breakout, support/resistance level, momentum indicator

---

## Data Quality Score

Score each entry 0.0–1.0 based on data availability:

| Deduction | Reason |
|---|---|
| -0.3 | No earnings report in or before window |
| -0.2 | FMP data missing (EDGAR fallback only) |
| -0.1 | RAG corpus has <3 chunks for this ticker+window |
| -0.1 | FRED macro data unavailable for the window |
| -0.1 | Price data from Polygon unavailable |

Entries below **0.70** are excluded from golden set evaluation runs.

---

## Time Splits

Do not use random train/test splits. Split by time only:

| Split | Period |
|---|---|
| Train | Pre-2023 |
| Validation | 2023 |
| Test | 2024 onward |

Never train on entries from the test split. Run the self-improvement loop only
against train and validation; report final metrics on test.

---

## Sector Coverage

Aim for coverage across at least 5 sectors to avoid sector-specific bias
in calibration curves:

- Technology (AAPL, MSFT, NVDA, GOOGL)
- Financials (JPM, GS, BAC)
- Consumer Discretionary (AMZN, TSLA, HD)
- Healthcare (JNJ, PFE, UNH)
- Energy (XOM, CVX)
- Industrials (CAT, BA, GE)

---

## Running Eval Against the Golden Set

```python
from swing_trader.eval.harness import load_golden_set, calibration_curve, feature_attribution

records = load_golden_set("datasets/golden_set/entries/")
# Filter to entries with ground truth (hit is not None)
records = [r for r in records if r.has_ground_truth]

curve = calibration_curve(records)
print(curve.to_dict())
print(curve.corrective_actions())

attribution = feature_attribution(records)
for a in attribution:
    print(f"{a.driver}: miss rate {a.miss_rate:.1%} ({a.misses}/{a.total_calls})")
```
