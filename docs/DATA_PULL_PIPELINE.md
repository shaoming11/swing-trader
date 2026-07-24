# Data Pull Pipeline — Design Spec

Covers the deterministic, no-LLM data pull that produces the structured numeric block injected into the composed prompt. This is Step 1 of the Layer 1 pipeline (PRD Section 5.2).

No model is involved at any point in this pipeline. Numbers come from APIs, are formatted into a fixed template, and are passed downstream as-is. Any summarization or interpretation happens in the reasoning stage, not here.

---

## 1. Pipeline Overview

```
[ticker + date_window]
        ↓
  ┌─────────────┐      ┌─────────────┐
  │ Fundamentals │      │    Macro    │
  │    Pull      │      │    Pull     │
  │ (EDGAR/FMP)  │      │   (FRED)    │
  └──────┬───────┘      └──────┬──────┘
         │                     │
         └──────────┬──────────┘
                    ↓
          [Structured Numeric Block]
```

Both pulls run in parallel. The numeric block is their merged output.

---

## 2. Fundamentals Pull

### 2.1 What to Pull

For a given ticker and date window (typically one quarter):

| Field | Source | Notes |
|---|---|---|
| EPS actual | FMP or EDGAR | From the most recent earnings report in or just before the window |
| EPS estimate (consensus) | FMP | Analyst consensus at time of report |
| EPS surprise % | Computed | `(actual - estimate) / abs(estimate) * 100` |
| Revenue actual | FMP or EDGAR | Quarterly revenue |
| Revenue estimate | FMP | Analyst consensus |
| Revenue surprise % | Computed | Same formula |
| Revenue growth YoY % | Computed | `(current_quarter - same_quarter_prior_year) / same_quarter_prior_year * 100` |
| Gross margin % | FMP | Current quarter vs prior quarter |
| Forward guidance (EPS) | FMP / earnings transcript flag | Was guidance raised, lowered, or maintained? |
| P/E ratio (trailing) | FMP | At window_start |
| P/E ratio (forward) | FMP | At window_start |
| Price at window_start | Polygon / yfinance | For context |
| 52-week high / low | Polygon / yfinance | At window_start |
| Corporate actions in window | FMP | Buybacks, splits, dividends, M&A announcements |

### 2.2 API Calls — Financial Modeling Prep (FMP)

**Earnings history (EPS + revenue actuals and estimates):**
```
GET https://financialmodelingprep.com/api/v3/earnings-surprises/{ticker}
    ?apikey={KEY}
```
Filter to the report date within or immediately before the window.

**Income statement (revenue, gross margin):**
```
GET https://financialmodelingprep.com/api/v3/income-statement/{ticker}
    ?period=quarter
    &limit=8
    &apikey={KEY}
```
Pull last 8 quarters to compute YoY comparisons.

**Key metrics / ratios (P/E):**
```
GET https://financialmodelingprep.com/api/v3/key-metrics/{ticker}
    ?period=quarter
    &limit=4
    &apikey={KEY}
```

**Analyst estimates (forward guidance context):**
```
GET https://financialmodelingprep.com/api/v3/analyst-estimates/{ticker}
    ?period=quarter
    &limit=4
    &apikey={KEY}
```

**Corporate actions:**
```
GET https://financialmodelingprep.com/api/v3/historical/earning_calendar/{ticker}
    ?from={window_start}
    &to={window_end}
    &apikey={KEY}

GET https://financialmodelingprep.com/api/v4/stock_news
    ?tickers={ticker}
    &from={window_start}
    &to={window_end}
    &limit=50
    &apikey={KEY}
```
Filter news for corporate action keywords: buyback, acquisition, merger, dividend, split, CEO, CFO, launch.

### 2.3 API Calls — SEC EDGAR (fallback / supplement)

Use EDGAR as a fallback when FMP data is missing or for full filing access:

**Company facts (structured financial data):**
```
GET https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
```
Extract `us-gaap/EarningsPerShareBasic`, `us-gaap/Revenues`, `us-gaap/GrossProfit` for the target quarter.

**CIK lookup:**
```
GET https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom
    &startdt={window_start}&enddt={window_end}&forms=10-Q,10-K,8-K
```

EDGAR is free and has no rate limit for reasonable use (max 10 req/sec per their policy). No API key required.

### 2.4 Price Data — Polygon.io

```
GET https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{window_start}/{window_end}
    ?adjusted=true
    &sort=asc
    &apikey={KEY}
```

Used for: price at window_start, 52-week high/low, volume profile during the window.

---

## 3. Macro Pull

### 3.1 Sector-Relevant Filtering

Not all macro indicators matter for every sector. Apply this mapping before deciding which FRED series to pull:

| Sector | Pull These Indicators |
|---|---|
| Technology | DGS10, FEDFUNDS, CPI |
| Financials / Banks | FEDFUNDS, DGS10, T10Y2Y (yield curve), BAMLH0A0HYM2 (HY spread) |
| Real Estate / REITs | FEDFUNDS, DGS10, MORTGAGE30US |
| Consumer Discretionary | UNRATE, UMCSENT (consumer sentiment), CPIAUCSL |
| Energy | DCOILWTICO (oil price), DHHNGSP (natural gas), DXY (dollar) |
| Healthcare | CPIMEDSL (medical CPI), FEDFUNDS |
| Industrials | INDPRO (industrial production), DCOILWTICO |
| Default (all sectors) | CPIAUCSL, FEDFUNDS, UNRATE, GDP |

The sector for a ticker is resolved from FMP's company profile endpoint:
```
GET https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={KEY}
```
Extract `sector` field.

### 3.2 FRED API Calls

**Series observations:**
```
GET https://api.stlouisfed.org/fred/series/observations
    ?series_id={SERIES_ID}
    &observation_start={window_start}
    &observation_end={window_end}
    &api_key={KEY}
    &file_type=json
    &sort_order=desc
    &limit=5
```

Pull the most recent 5 observations within the window (captures any mid-quarter prints). For GDP (quarterly), pull the last 2 observations to compute QoQ change.

**FRED Series Reference:**

| Series ID | Indicator | Frequency |
|---|---|---|
| `CPIAUCSL` | CPI (All Urban Consumers) | Monthly |
| `FEDFUNDS` | Fed Funds Effective Rate | Monthly |
| `UNRATE` | Unemployment Rate | Monthly |
| `GDP` | Gross Domestic Product | Quarterly |
| `T10YIE` | 10-Year Breakeven Inflation | Daily |
| `DGS10` | 10-Year Treasury Yield | Daily |
| `T10Y2Y` | 10Y-2Y Yield Spread | Daily |
| `MORTGAGE30US` | 30-Year Mortgage Rate | Weekly |
| `UMCSENT` | U of M Consumer Sentiment | Monthly |
| `INDPRO` | Industrial Production Index | Monthly |
| `DCOILWTICO` | WTI Crude Oil Price | Daily |
| `BAMLH0A0HYM2` | High Yield OAS Spread | Daily |

For daily series, pull the value at `window_start` and `window_end` to show the change over the window.

### 3.3 FOMC Events

FOMC decisions are not in FRED series format — pull them separately:

```
GET https://api.stlouisfed.org/fred/release/dates
    ?release_id=82
    &api_key={KEY}
    &file_type=json
```

If a FOMC meeting date falls within the window, flag it and pull the decision (rate change + direction). This is a critical event that the reasoning stage needs to know explicitly occurred.

---

## 4. Output — Structured Numeric Block

Both pulls merge into one fixed-template block. This is rendered as plain text injected into the composed prompt — not JSON, not prose, not summarized.

### 4.1 Template

```
=== STRUCTURED DATA: {TICKER} ({window_start} to {window_end}) ===

--- FUNDAMENTALS ---
Earnings (most recent report: {report_date})
  EPS Actual:        ${eps_actual}
  EPS Estimate:      ${eps_estimate}
  EPS Surprise:      {eps_surprise_pct:+.1f}%
  Revenue Actual:    ${revenue_actual_B}B
  Revenue Estimate:  ${revenue_estimate_B}B
  Revenue Surprise:  {revenue_surprise_pct:+.1f}%
  Revenue YoY:       {revenue_yoy_pct:+.1f}%
  Gross Margin:      {gross_margin_pct:.1f}% (prior quarter: {prior_gross_margin_pct:.1f}%)
  Forward Guidance:  {guidance_direction}  [{guidance_note}]

Valuation (as of {window_start})
  P/E Trailing:      {pe_trailing:.1f}x
  P/E Forward:       {pe_forward:.1f}x
  Price:             ${price_at_start}
  52-Week High:      ${fifty_two_high}
  52-Week Low:       ${fifty_two_low}

Corporate Actions in Window
  {corporate_actions_list or "None"}

--- MACRO ({sector}) ---
{macro_block — dynamically built from sector mapping}

Example for Technology sector:
  Fed Funds Rate:    {fedfunds_current:.2f}% (window start: {fedfunds_start:.2f}%)
  10Y Treasury:      {dgs10_current:.2f}% (window start: {dgs10_start:.2f}%)
  CPI (latest):      {cpi_yoy:.1f}% YoY  [print date: {cpi_date}]
  FOMC in Window:    {fomc_occurred}  [{fomc_decision if occurred}]

--- NOTES ---
{Any data gaps — fields that could not be populated and why}
```

### 4.2 Output Schema (Internal)

```python
@dataclass
class NumericBlock:
    ticker: str
    window_start: date
    window_end: date
    rendered_text: str          # the filled template above — injected into prompt
    data_gaps: list[str]        # fields that could not be populated
    sources_used: list[str]     # e.g., ["FMP", "FRED", "Polygon"]
    fundamentals_report_date: date | None
    macro_series_pulled: list[str]
```

---

## 5. Data Gap Handling

Missing data is surfaced explicitly rather than silently omitted. The reasoning stage needs to know when it is working with incomplete information.

| Scenario | Behavior |
|---|---|
| No earnings report in or before the window | Flag in `data_gaps`; omit EPS/revenue fields; note "No recent earnings in window" in NOTES |
| FMP endpoint returns empty | Retry once; if still empty, fall back to EDGAR |
| EDGAR also empty | Flag in `data_gaps`; continue with whatever is available |
| FRED series has no observations in window | Note "No {indicator} print during window" in NOTES |
| Polygon price data unavailable | Use prior close from FMP profile endpoint as fallback |
| Sector not recognized | Fall back to default macro set (CPI, FEDFUNDS, UNRATE, GDP) |

Do NOT fill gaps with estimates or inferences. If the data is not available, say so. The reasoning model should know it is reasoning under incomplete information.

---

## 6. Caching

API responses are cached to disk to avoid redundant calls and to support re-runs during the self-improvement loop.

```
cache/
  fundamentals/
    {ticker}_{quarter}.json      # e.g., AAPL_2024Q1.json
  macro/
    {series_id}_{YYYY-MM}.json   # e.g., CPIAUCSL_2024-03.json
  price/
    {ticker}_{window_start}_{window_end}.json
```

Cache TTL:
- Historical data (window_end in the past): permanent — never re-fetch
- Current quarter data: 24-hour TTL — re-fetch daily
- FOMC events: permanent once the decision is known

Cache check runs before any API call. On cache hit, skip the API call entirely.

---

## 7. Integration with LangGraph

Runs as a single LangGraph node: `data_pull`. Executes fundamentals and macro pulls in parallel using `asyncio.gather`.

```python
async def data_pull_node(state: PipelineState) -> PipelineState:
    fundamentals_task = pull_fundamentals(
        ticker=state.ticker,
        window_start=state.window_start,
        window_end=state.window_end
    )
    macro_task = pull_macro(
        ticker=state.ticker,
        window_start=state.window_start,
        window_end=state.window_end
    )

    fundamentals, macro = await asyncio.gather(
        fundamentals_task, macro_task, return_exceptions=True
    )

    # Handle partial failures gracefully
    if isinstance(fundamentals, Exception):
        state.warnings.append(f"data_pull: fundamentals failed — {fundamentals}")
        fundamentals = FundamentalsResult.empty(state.ticker)

    if isinstance(macro, Exception):
        state.warnings.append(f"data_pull: macro failed — {macro}")
        macro = MacroResult.empty()

    block = build_numeric_block(state.ticker, state.window_start, state.window_end, fundamentals, macro)

    # Log to observability
    log_node_output("data_pull", {
        "sources_used": block.sources_used,
        "data_gaps": block.data_gaps,
        "fundamentals_report_date": str(block.fundamentals_report_date),
        "macro_series_pulled": block.macro_series_pulled
    })

    state.numeric_block = block
    return state
```

A failure in either pull does not cancel the pipeline — it degrades gracefully and the gap is surfaced in the NOTES section of the numeric block.

---

## 8. Implementation Notes

- **HTTP client:** `httpx` with `AsyncClient` for parallel pulls
- **Rate limiting:** Use `asyncio.Semaphore` to cap concurrent FMP requests (FMP basic: 300/min → semaphore of 5 with short sleep is safe)
- **EDGAR:** No key required; respect 10 req/sec limit; add `User-Agent` header per EDGAR policy
- **FRED:** Free API key; 120 req/min limit — well within needs for single-ticker pulls
- **Retry policy:** 2 retries with exponential backoff (1s, 3s) on 429 or 5xx; give up and log on third failure
- **Currency:** All monetary values normalized to USD billions for revenue, USD dollars for EPS and price
- **Date handling:** All dates in ISO 8601 (`YYYY-MM-DD`); use `python-dateutil` for parsing mixed API date formats
- **Quarter mapping:** `window_start` → fiscal quarter resolved from the company's fiscal calendar (pulled from FMP profile once and cached)
