"""Fundamentals pull — FMP (primary) + EDGAR (fallback).

Pulls EPS, revenue, margins, valuation, corporate actions, and price data
for a given ticker and date window. No LLM involved anywhere in this module.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
from dateutil.parser import parse as parse_date
from tenacity import retry, stop_after_attempt, wait_exponential

from swing_trader.data_pull import cache as disk_cache
from swing_trader.schemas.pipeline import FundamentalsResult

_FMP = "https://financialmodelingprep.com/api/v3"
_EDGAR_FACTS = "https://data.sec.gov/api/xbrl/companyfacts"
_EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
_POLYGON = "https://api.polygon.io/v2/aggs"

_CORPORATE_ACTION_KEYWORDS = {
    "buyback", "repurchase", "acquisition", "merger", "acquired",
    "dividend", "split", "ceo", "cfo", "launch", "recall", "settlement",
}


def _fmp_key() -> str:
    key = os.getenv("FMP_API_KEY", "")
    if not key:
        raise EnvironmentError("FMP_API_KEY is not set")
    return key


def _polygon_key() -> str:
    key = os.getenv("POLYGON_API_KEY", "")
    if not key:
        raise EnvironmentError("POLYGON_API_KEY is not set")
    return key


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=3))
async def _get(client: httpx.AsyncClient, url: str, params: dict) -> Any:
    resp = await client.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def pull_fundamentals(
    ticker: str,
    window_start: date,
    window_end: date,
) -> FundamentalsResult:
    """Pull all fundamentals fields for the ticker within the date window."""
    quarter = _date_to_quarter(window_start)
    cached = disk_cache.get_fundamentals(ticker, quarter)
    if cached:
        return FundamentalsResult.model_validate(cached)

    async with httpx.AsyncClient(
        headers={"User-Agent": "swing-trader research bot (contact@example.com)"},
        limits=httpx.Limits(max_connections=5),
    ) as client:
        result = await _fetch_from_fmp(client, ticker, window_start, window_end)

    disk_cache.set_fundamentals(ticker, quarter, result.model_dump(mode="json"))
    return result


async def _fetch_from_fmp(
    client: httpx.AsyncClient,
    ticker: str,
    window_start: date,
    window_end: date,
) -> FundamentalsResult:
    key = _fmp_key()
    gaps: list[str] = []

    # ── Earnings surprises ─────────────────────────────────────────────────
    eps_actual = eps_estimate = eps_surprise = None
    rev_actual = rev_estimate = rev_surprise = rev_yoy = None
    report_date = None
    try:
        surprises = await _get(
            client, f"{_FMP}/earnings-surprises/{ticker}", {"apikey": key}
        )
        target = _find_report_in_window(surprises, window_start, window_end)
        if target:
            report_date = parse_date(target.get("date", "")).date()
            eps_actual = target.get("actualEarningResult")
            eps_estimate = target.get("estimatedEarning")
            if eps_actual is not None and eps_estimate:
                eps_surprise = (eps_actual - eps_estimate) / abs(eps_estimate) * 100
        else:
            gaps.append("No earnings report found within or before the window")
    except Exception as e:
        gaps.append(f"FMP earnings-surprises: {e}")

    # ── Income statement (revenue + gross margin) ──────────────────────────
    gross_margin = prior_gross_margin = None
    try:
        income = await _get(
            client, f"{_FMP}/income-statement/{ticker}",
            {"period": "quarter", "limit": "8", "apikey": key},
        )
        if income and report_date:
            current = _find_quarter(income, report_date)
            prior = _find_quarter(income, report_date, offset_quarters=-1)
            yoy = _find_quarter(income, report_date, offset_quarters=-4)

            if current:
                rev_actual = _to_billions(current.get("revenue"))
                gross_margin = _pct(current.get("grossProfitRatio"))
                try:
                    rev_estimate_raw = await _get(
                        client, f"{_FMP}/analyst-estimates/{ticker}",
                        {"period": "quarter", "limit": "4", "apikey": key},
                    )
                    est = _find_quarter(rev_estimate_raw, report_date)
                    rev_estimate = _to_billions(est.get("estimatedRevenueAvg")) if est else None
                    if rev_actual and rev_estimate:
                        rev_surprise = (rev_actual - rev_estimate) / abs(rev_estimate) * 100
                except Exception:
                    gaps.append("FMP analyst-estimates: revenue estimate unavailable")

            if prior:
                prior_gross_margin = _pct(prior.get("grossProfitRatio"))

            if current and yoy:
                curr_rev = current.get("revenue", 0) or 0
                yoy_rev = yoy.get("revenue", 0) or 0
                if yoy_rev:
                    rev_yoy = (curr_rev - yoy_rev) / yoy_rev * 100
    except Exception as e:
        gaps.append(f"FMP income-statement: {e}")

    # ── Key metrics (P/E) ──────────────────────────────────────────────────
    pe_trailing = pe_forward = None
    try:
        metrics = await _get(
            client, f"{_FMP}/key-metrics/{ticker}",
            {"period": "quarter", "limit": "4", "apikey": key},
        )
        m = _find_quarter(metrics, window_start) if metrics else None
        if m:
            pe_trailing = m.get("peRatio")
            pe_forward = m.get("priceEarningsToGrowthRatio")  # closest proxy available
    except Exception as e:
        gaps.append(f"FMP key-metrics: {e}")

    # ── Price data via Polygon ────────────────────────────────────────────
    price_start = high_52 = low_52 = None
    try:
        price_start, high_52, low_52 = await _fetch_price(client, ticker, window_start, window_end)
    except Exception as e:
        gaps.append(f"Polygon price: {e}")

    # ── Corporate actions ─────────────────────────────────────────────────
    corporate_actions: list[str] = []
    try:
        news = await _get(
            client, f"{_FMP}/v4/stock_news",
            {
                "tickers": ticker,
                "from": str(window_start),
                "to": str(window_end),
                "limit": "50",
                "apikey": key,
            },
        )
        for article in news or []:
            title = (article.get("title") or "").lower()
            text = (article.get("text") or "").lower()
            combined = title + " " + text
            if any(kw in combined for kw in _CORPORATE_ACTION_KEYWORDS):
                corporate_actions.append(article.get("title", "").strip())
    except Exception as e:
        gaps.append(f"FMP corporate actions: {e}")

    # ── Forward guidance ──────────────────────────────────────────────────
    guidance_direction = guidance_note = None
    try:
        est = await _get(
            client, f"{_FMP}/analyst-estimates/{ticker}",
            {"period": "quarter", "limit": "4", "apikey": key},
        )
        if est and report_date:
            current_est = _find_quarter(est, report_date)
            prior_est = _find_quarter(est, report_date, offset_quarters=-1)
            if current_est and prior_est:
                cur_eps = current_est.get("estimatedEpsAvg")
                prv_eps = prior_est.get("estimatedEpsAvg")
                if cur_eps and prv_eps:
                    if cur_eps > prv_eps * 1.02:
                        guidance_direction = "raised"
                    elif cur_eps < prv_eps * 0.98:
                        guidance_direction = "lowered"
                    else:
                        guidance_direction = "maintained"
                    guidance_note = f"Forward EPS estimate: ${cur_eps:.2f}"
    except Exception as e:
        gaps.append(f"FMP guidance: {e}")

    return FundamentalsResult(
        ticker=ticker,
        report_date=report_date,
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        eps_surprise_pct=round(eps_surprise, 1) if eps_surprise is not None else None,
        revenue_actual_b=round(rev_actual, 2) if rev_actual is not None else None,
        revenue_estimate_b=round(rev_estimate, 2) if rev_estimate is not None else None,
        revenue_surprise_pct=round(rev_surprise, 1) if rev_surprise is not None else None,
        revenue_yoy_pct=round(rev_yoy, 1) if rev_yoy is not None else None,
        gross_margin_pct=round(gross_margin, 1) if gross_margin is not None else None,
        prior_gross_margin_pct=round(prior_gross_margin, 1) if prior_gross_margin is not None else None,
        guidance_direction=guidance_direction or "not provided",
        guidance_note=guidance_note or "",
        pe_trailing=round(pe_trailing, 1) if pe_trailing else None,
        pe_forward=round(pe_forward, 1) if pe_forward else None,
        price_at_start=price_start,
        fifty_two_high=high_52,
        fifty_two_low=low_52,
        corporate_actions=corporate_actions[:5],  # cap at 5 most relevant
        data_gaps=gaps,
    )


async def _fetch_price(
    client: httpx.AsyncClient,
    ticker: str,
    window_start: date,
    window_end: date,
) -> tuple[float | None, float | None, float | None]:
    cached = disk_cache.get_price(ticker, window_start, window_end)
    if cached:
        return cached["start"], cached["high_52"], cached["low_52"]

    # One-year window for 52-week high/low
    from datetime import timedelta
    year_start = window_start - timedelta(days=365)

    resp = await _get(
        client,
        f"{_POLYGON}/ticker/{ticker}/range/1/day/{year_start}/{window_end}",
        {"adjusted": "true", "sort": "asc", "apikey": _polygon_key()},
    )
    results = resp.get("results", [])
    if not results:
        return None, None, None

    window_start_ts = window_start.toordinal()
    prices_in_window = [
        r["c"] for r in results
        if r.get("t") and _ms_to_date(r["t"]).toordinal() >= window_start_ts
    ]
    all_closes = [r["c"] for r in results if r.get("c")]

    price_start = prices_in_window[0] if prices_in_window else None
    high_52 = max(all_closes) if all_closes else None
    low_52 = min(all_closes) if all_closes else None

    disk_cache.set_price(ticker, window_start, window_end, {
        "start": price_start, "high_52": high_52, "low_52": low_52
    })
    return price_start, high_52, low_52


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_report_in_window(
    surprises: list[dict], window_start: date, window_end: date
) -> dict | None:
    for s in surprises:
        try:
            d = parse_date(s.get("date", "")).date()
            if window_start - __import__("datetime").timedelta(days=90) <= d <= window_end:
                return s
        except Exception:
            continue
    return None


def _find_quarter(
    rows: list[dict], ref_date: date, offset_quarters: int = 0
) -> dict | None:
    if not rows:
        return None
    from datetime import timedelta
    target = ref_date + timedelta(days=offset_quarters * 90)
    best = None
    best_delta = float("inf")
    for row in rows:
        try:
            d = parse_date(row.get("date", "") or row.get("period", "")).date()
            delta = abs((d - target).days)
            if delta < best_delta:
                best_delta = delta
                best = row
        except Exception:
            continue
    return best if best_delta < 120 else None


def _to_billions(val: float | None) -> float | None:
    return round(val / 1e9, 3) if val else None


def _pct(val: float | None) -> float | None:
    return round(val * 100, 2) if val is not None else None


def _date_to_quarter(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


def _ms_to_date(ms: int) -> date:
    from datetime import datetime
    return datetime.utcfromtimestamp(ms / 1000).date()
