# Production Data Pull — Free APIs Only

## Problem

`yfinance` covers price + valuation well but has two gaps in production:

1. **EPS actuals / revenue actuals** — `earnings_history` only keeps recent quarters; historical backfill fails
2. **Reliability** — Yahoo Finance is unofficial; no SLA, rate limits enforced silently

This doc describes how to close both gaps using only free, official APIs.

---

## Free API Stack

| Data | Source | Key Required | Limit |
|---|---|---|---|
| Price (OHLCV), P/E, basic info | yfinance (Yahoo) | None | ~2k req/day before throttling |
| EPS actuals, revenue actuals, gross margin | SEC EDGAR | None | 10 req/sec (official) |
| Earnings surprises (beat/miss) | Alpha Vantage | Free signup | 25 req/day (free tier) |
| Macro (CPI, Fed Funds, GDP, etc.) | FRED | Free signup | 120 req/min |
| Corporate news | yfinance news | None | bundled |

**Bottom line:** EDGAR covers the EPS/revenue gap completely and is rate-limit-friendly. Alpha Vantage fills in the beat/miss surprise metric.

---

## 1. SEC EDGAR Integration

EDGAR exposes every 10-Q/10-K filing as structured JSON. No key, no cost, official SEC data.

```
GET https://data.sec.gov/api/xbrl/companyfacts/{CIK}.json
```

The response contains a `facts` object with `us-gaap` taxonomy entries. The key fields:

| Field needed | EDGAR concept |
|---|---|
| Revenue | `us-gaap/Revenues` or `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax` |
| Gross Profit | `us-gaap/GrossProfit` |
| EPS Actual (diluted) | `us-gaap/EarningsPerShareDiluted` |
| Net Income | `us-gaap/NetIncomeLoss` |

### CIK Lookup

EDGAR uses CIK numbers, not tickers. Resolve once and cache permanently:

```
GET https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=AAPL&type=10-Q&dateb=&owner=include&count=1&search_text=&output=atom
```

Or use the bulk ticker→CIK map (updated daily by SEC):
```
GET https://www.sec.gov/files/company_tickers.json
```

### Implementation sketch

```python
import asyncio
import httpx
from datetime import date

_EDGAR_FACTS = "https://data.sec.gov/api/xbrl/companyfacts"
_TICKER_MAP  = "https://www.sec.gov/files/company_tickers.json"
_HEADERS = {"User-Agent": "swing-trader research (your@email.com)"}  # required by SEC

async def get_cik(ticker: str, client: httpx.AsyncClient) -> str | None:
    """Resolve ticker → zero-padded 10-digit CIK."""
    resp = await client.get(_TICKER_MAP, headers=_HEADERS)
    data = resp.json()
    for entry in data.values():
        if entry["ticker"].upper() == ticker.upper():
            return str(entry["cik_str"]).zfill(10)
    return None


async def pull_edgar_fundamentals(
    ticker: str,
    window_start: date,
    window_end: date,
    client: httpx.AsyncClient,
) -> dict:
    """
    Returns dict with keys:
      eps_actual, revenue_actual_b, gross_margin_pct,
      revenue_yoy_pct, report_date
    All values are None if not found.
    """
    cik = await get_cik(ticker, client)
    if not cik:
        return _empty_edgar()

    resp = await client.get(f"{_EDGAR_FACTS}/CIK{cik}.json", headers=_HEADERS)
    facts = resp.json().get("facts", {}).get("us-gaap", {})

    # Quarterly EPS diluted
    eps_series = _extract_quarterly(facts, "EarningsPerShareDiluted", window_start, window_end)

    # Revenue — try two common concept names
    rev_series = _extract_quarterly(facts, "RevenueFromContractWithCustomerExcludingAssessedTax", window_start, window_end)
    if not rev_series:
        rev_series = _extract_quarterly(facts, "Revenues", window_start, window_end)

    # Gross profit
    gp_series = _extract_quarterly(facts, "GrossProfit", window_start, window_end)

    # YoY: find same quarter 1 year prior
    rev_yoy = None
    if rev_series and rev_series.get("value"):
        prior = _extract_quarterly(facts, "RevenueFromContractWithCustomerExcludingAssessedTax",
                                   window_start.replace(year=window_start.year - 1),
                                   window_end.replace(year=window_end.year - 1))
        if not prior:
            prior = _extract_quarterly(facts, "Revenues",
                                       window_start.replace(year=window_start.year - 1),
                                       window_end.replace(year=window_end.year - 1))
        if prior and prior.get("value"):
            rev_yoy = (rev_series["value"] - prior["value"]) / abs(prior["value"]) * 100

    rev_b = rev_series["value"] / 1e9 if rev_series else None
    gm = None
    if gp_series and rev_series and rev_series["value"]:
        gm = gp_series["value"] / rev_series["value"] * 100

    return {
        "eps_actual": eps_series["value"] if eps_series else None,
        "report_date": eps_series["end"] if eps_series else None,
        "revenue_actual_b": round(rev_b, 2) if rev_b else None,
        "gross_margin_pct": round(gm, 1) if gm else None,
        "revenue_yoy_pct": round(rev_yoy, 1) if rev_yoy else None,
    }


def _extract_quarterly(facts: dict, concept: str, window_start: date, window_end: date) -> dict | None:
    """Find the quarterly filing (form=10-Q or 10-K) whose end date falls in the window."""
    entries = facts.get(concept, {}).get("units", {})
    # EPS is USD/shares, revenue/GP are USD
    unit_data = entries.get("USD/shares") or entries.get("USD") or {}
    filings = unit_data if isinstance(unit_data, list) else []

    best = None
    for f in filings:
        if f.get("form") not in ("10-Q", "10-K"):
            continue
        try:
            end = date.fromisoformat(f["end"])
            # quarterly entries have ~90-day spans; filter out annual rollups
            start_d = date.fromisoformat(f.get("start", f["end"]))
            span = (end - start_d).days
            if span > 120:  # skip annual (>4 months span)
                continue
            if window_start <= end <= window_end:
                best = {"value": f["val"], "end": end}
        except Exception:
            continue
    return best


def _empty_edgar() -> dict:
    return {k: None for k in
            ["eps_actual", "report_date", "revenue_actual_b", "gross_margin_pct", "revenue_yoy_pct"]}
```

### Rate limiting

SEC enforces 10 req/sec. Add a simple semaphore:

```python
_EDGAR_SEMAPHORE = asyncio.Semaphore(8)  # stay under the 10 req/sec limit

async def _edgar_get(client, url):
    async with _EDGAR_SEMAPHORE:
        resp = await client.get(url, headers=_EDGAR_HEADERS)
        resp.raise_for_status()
        return resp.json()
```

---

## 2. Alpha Vantage — Earnings Surprise (beat/miss %)

Free tier: 25 req/day. Use only for the beat/miss field; everything else comes from EDGAR + yfinance.

```python
_AV_BASE = "https://www.alphavantage.co/query"

async def pull_eps_estimate(ticker: str, client: httpx.AsyncClient) -> float | None:
    """Returns the analyst EPS consensus for the most recent quarter."""
    key = os.getenv("ALPHA_VANTAGE_API_KEY", "")
    if not key:
        return None
    resp = await client.get(_AV_BASE, params={
        "function": "EARNINGS",
        "symbol": ticker,
        "apikey": key,
    })
    data = resp.json()
    quarterly = data.get("quarterlyEarnings", [])
    if not quarterly:
        return None
    latest = quarterly[0]
    return _safe_float(latest.get("estimatedEPS"))
```

Sign up at https://www.alphavantage.co/support/#api-key (free, instant).

---

## 3. Production `pull_fundamentals` — merged fetch

```python
async def pull_fundamentals(ticker, window_start, window_end) -> FundamentalsResult:
    quarter = _date_to_quarter(window_start)
    cached = disk_cache.get_fundamentals(ticker, quarter)
    if cached:
        return FundamentalsResult.model_validate(cached)

    async with httpx.AsyncClient(headers=_HEADERS, timeout=20) as client:
        # Run yfinance (sync) + EDGAR (async) in parallel
        yf_task = asyncio.to_thread(_fetch_yfinance_fields, ticker, window_start, window_end)
        edgar_task = pull_edgar_fundamentals(ticker, window_start, window_end, client)
        av_task = pull_eps_estimate(ticker, client)

        yf_data, edgar_data, eps_estimate = await asyncio.gather(
            yf_task, edgar_task, av_task, return_exceptions=True
        )

    # Merge: EDGAR wins for actuals, yfinance wins for price/valuation
    gaps = []
    if isinstance(yf_data, Exception):
        gaps.append(f"yfinance: {yf_data}")
        yf_data = {}
    if isinstance(edgar_data, Exception):
        gaps.append(f"EDGAR: {edgar_data}")
        edgar_data = _empty_edgar()
    if isinstance(eps_estimate, Exception):
        eps_estimate = None

    eps_actual = edgar_data.get("eps_actual")
    eps_surprise = None
    if eps_actual and eps_estimate:
        eps_surprise = (eps_actual - eps_estimate) / abs(eps_estimate) * 100

    return FundamentalsResult(
        ticker=ticker,
        report_date=edgar_data.get("report_date"),
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        eps_surprise_pct=round(eps_surprise, 1) if eps_surprise else None,
        revenue_actual_b=edgar_data.get("revenue_actual_b"),
        revenue_yoy_pct=edgar_data.get("revenue_yoy_pct"),
        gross_margin_pct=edgar_data.get("gross_margin_pct"),
        pe_trailing=yf_data.get("pe_trailing"),
        pe_forward=yf_data.get("pe_forward"),
        price_at_start=yf_data.get("price_start"),
        fifty_two_high=yf_data.get("high_52"),
        fifty_two_low=yf_data.get("low_52"),
        guidance_direction=yf_data.get("guidance_direction", "not provided"),
        guidance_note=yf_data.get("guidance_note", ""),
        corporate_actions=yf_data.get("corporate_actions", []),
        # revenue_estimate from yfinance revenue_estimate df
        revenue_estimate_b=yf_data.get("revenue_estimate_b"),
        revenue_surprise_pct=_compute_surprise(
            edgar_data.get("revenue_actual_b"), yf_data.get("revenue_estimate_b")
        ),
        prior_gross_margin_pct=yf_data.get("prior_gross_margin"),
        data_gaps=gaps,
    )


def _compute_surprise(actual, estimate) -> float | None:
    if actual and estimate:
        return round((actual - estimate) / abs(estimate) * 100, 1)
    return None
```

---

## 4. Caching strategy for production

The current file-based JSON cache is fine for < 100 tickers/day. For higher volume:

| Volume | Recommendation |
|---|---|
| < 100 runs/day | File cache (current) — no change needed |
| 100–1k runs/day | SQLite cache (single file, no server, thread-safe reads) |
| > 1k runs/day | Redis with TTLs — expire current-quarter data every 24h, never expire historical |

SQLite drop-in (replaces `cache.py` internals):

```python
import sqlite3, json
from pathlib import Path

_DB = Path("cache/fundamentals.db")

def _conn():
    _DB.parent.mkdir(exist_ok=True)
    return sqlite3.connect(_DB, check_same_thread=False)

def get_fundamentals(ticker, quarter):
    with _conn() as c:
        row = c.execute(
            "SELECT data FROM fundamentals WHERE ticker=? AND quarter=?",
            (ticker, quarter)
        ).fetchone()
    return json.loads(row[0]) if row else None

def set_fundamentals(ticker, quarter, data):
    with _conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS fundamentals (ticker TEXT, quarter TEXT, data TEXT, PRIMARY KEY (ticker, quarter))")
        c.execute("INSERT OR REPLACE INTO fundamentals VALUES (?,?,?)", (ticker, quarter, json.dumps(data, default=str)))
```

---

## 5. Pre-fetch job (decouple data pull from pipeline)

In production, don't pull data inline during a pipeline run. Pre-fetch on a schedule:

```python
# scripts/prefetch.py
# Run nightly via cron or a scheduler

WATCHLIST = ["AAPL", "TSLA", "MSFT", "NVDA", "GOOGL", "AMD", "META", "AMZN"]

async def prefetch_all():
    from datetime import date, timedelta
    today = date.today()
    window_start = today - timedelta(days=90)
    for ticker in WATCHLIST:
        try:
            result = await pull_fundamentals(ticker, window_start, today)
            print(f"{ticker}: {len(result.data_gaps)} gaps")
        except Exception as e:
            print(f"{ticker}: FAILED — {e}")

if __name__ == "__main__":
    asyncio.run(prefetch_all())
```

Run it:
```bash
# cron: every day at 6am
0 6 * * * cd /path/to/swing-trader && python scripts/prefetch.py
```

---

## 6. What you still can't get free

These fields require a paid provider no matter what:

| Field | Why |
|---|---|
| Real-time price (< 15min delay) | Yahoo is 15-min delayed; real-time needs Polygon/IEX paid |
| Analyst price targets (consensus) | No free source with full coverage |
| Institutional ownership changes | 13F filings lag 45 days; no aggregate free API |
| Options flow / unusual activity | No free source |

For a swing trade setup (holding days to weeks), 15-minute delayed price is fine — these gaps don't matter.

---

## Summary

To go prod with free APIs only:
1. Add EDGAR integration (closes EPS + revenue actual gap)
2. Add Alpha Vantage free key (closes beat/miss gap)
3. Keep yfinance for price, P/E, guidance, news
4. Keep FRED for macro (already working)
5. Move to SQLite cache if volume grows
6. Add a nightly prefetch job to decouple data from pipeline hot path

**Required env vars (all free):**
```
ALPHA_VANTAGE_API_KEY=your_free_key   # alphavantage.co/support/#api-key
FRED_API_KEY=your_free_key            # already set
# No key needed for EDGAR or yfinance
```
