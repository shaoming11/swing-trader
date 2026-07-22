"""Macro pull — FRED API with sector-relevant indicator selection.

Pulls CPI, Fed funds rate, unemployment, GDP, and sector-specific indicators
for the given date window. Also detects FOMC meetings within the window.
No LLM involved.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from swing_trader.data_pull import cache as disk_cache
from swing_trader.schemas.pipeline import FOMCEvent, MacroDataPoint, MacroResult

_FRED = "https://api.stlouisfed.org/fred"

# ── Sector → FRED series mapping ──────────────────────────────────────────────

_SECTOR_SERIES: dict[str, list[tuple[str, str, str]]] = {
    # (series_id, human label, unit)
    "Technology": [
        ("DGS10",    "10-Year Treasury Yield",  "%"),
        ("FEDFUNDS", "Fed Funds Rate",           "%"),
        ("CPIAUCSL", "CPI YoY",                 "%"),
    ],
    "Financials": [
        ("FEDFUNDS",      "Fed Funds Rate",         "%"),
        ("DGS10",         "10-Year Treasury Yield", "%"),
        ("T10Y2Y",        "10Y-2Y Yield Spread",    "%"),
        ("BAMLH0A0HYM2",  "HY OAS Spread",          "bps"),
    ],
    "Real Estate": [
        ("FEDFUNDS",     "Fed Funds Rate",        "%"),
        ("DGS10",        "10-Year Treasury Yield","%"),
        ("MORTGAGE30US", "30-Year Mortgage Rate", "%"),
    ],
    "Consumer Discretionary": [
        ("UNRATE",   "Unemployment Rate",      "%"),
        ("UMCSENT",  "Consumer Sentiment",     "index"),
        ("CPIAUCSL", "CPI YoY",               "%"),
    ],
    "Energy": [
        ("DCOILWTICO", "WTI Crude Oil",    "$/bbl"),
        ("DHHNGSP",    "Natural Gas",      "$/MMBtu"),
    ],
    "Healthcare": [
        ("CPIMEDSL", "Medical CPI YoY", "%"),
        ("FEDFUNDS", "Fed Funds Rate",  "%"),
    ],
    "Industrials": [
        ("INDPRO",     "Industrial Production Index", "index"),
        ("DCOILWTICO", "WTI Crude Oil",               "$/bbl"),
    ],
}

_DEFAULT_SERIES: list[tuple[str, str, str]] = [
    ("CPIAUCSL", "CPI YoY",          "%"),
    ("FEDFUNDS", "Fed Funds Rate",   "%"),
    ("UNRATE",   "Unemployment Rate","%"),
    ("GDP",      "GDP",              "$B"),
]

_FOMC_RELEASE_ID = "82"


def _fred_key() -> str:
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        raise EnvironmentError("FRED_API_KEY is not set")
    return key


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=3))
async def _fred_get(client: httpx.AsyncClient, endpoint: str, params: dict) -> Any:
    params = {**params, "api_key": _fred_key(), "file_type": "json"}
    resp = await client.get(f"{_FRED}/{endpoint}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


async def pull_macro(
    ticker: str,
    window_start: date,
    window_end: date,
    sector: str | None = None,
) -> MacroResult:
    """Pull macro indicators relevant to the sector."""
    series_list = _SECTOR_SERIES.get(sector or "", _DEFAULT_SERIES)
    if not series_list:
        series_list = _DEFAULT_SERIES

    gaps: list[str] = []
    pulled: list[MacroDataPoint] = []
    fomc_in_window = False
    fomc_event: FOMCEvent | None = None

    async with httpx.AsyncClient() as client:
        for series_id, label, unit in series_list:
            dp = await _fetch_series(
                client, series_id, label, unit, window_start, window_end, gaps
            )
            if dp:
                pulled.append(dp)

        # FOMC detection
        try:
            fomc_in_window, fomc_event = await _detect_fomc(
                client, window_start, window_end
            )
        except Exception as e:
            gaps.append(f"FOMC detection: {e}")

    return MacroResult(
        sector=sector or "default",
        series=pulled,
        fomc_in_window=fomc_in_window,
        fomc_event=fomc_event,
        data_gaps=gaps,
    )


async def _fetch_series(
    client: httpx.AsyncClient,
    series_id: str,
    label: str,
    unit: str,
    window_start: date,
    window_end: date,
    gaps: list[str],
) -> MacroDataPoint | None:
    year_month = f"{window_start.year}-{window_start.month:02d}"
    cached = disk_cache.get_macro(series_id, year_month)
    if cached:
        return MacroDataPoint.model_validate(cached)

    try:
        data = await _fred_get(
            client, "series/observations",
            {
                "series_id": series_id,
                "observation_start": str(window_start),
                "observation_end": str(window_end),
                "sort_order": "asc",
                "limit": "10",
            },
        )
        obs = [o for o in (data.get("observations") or []) if o.get("value") != "."]
        if not obs:
            gaps.append(f"No {series_id} observations in window")
            return None

        first = obs[0]
        last = obs[-1]

        def _val(o: dict) -> float | None:
            try:
                return float(o["value"])
            except (KeyError, ValueError, TypeError):
                return None

        dp = MacroDataPoint(
            series_id=series_id,
            label=label,
            value_start=_val(first),
            value_end=_val(last),
            date_start=first.get("date"),
            date_end=last.get("date"),
            unit=unit,
        )
        disk_cache.set_macro(series_id, year_month, dp.model_dump(mode="json"))
        return dp
    except Exception as e:
        gaps.append(f"FRED {series_id}: {e}")
        return None


async def _detect_fomc(
    client: httpx.AsyncClient,
    window_start: date,
    window_end: date,
) -> tuple[bool, FOMCEvent | None]:
    cached = disk_cache.get_fomc(window_start.year)
    if cached is None:
        data = await _fred_get(
            client, "release/dates",
            {"release_id": _FOMC_RELEASE_ID, "sort_order": "asc", "limit": "20"},
        )
        release_dates = [d["date"] for d in (data.get("release_dates") or [])]
        disk_cache.set_fomc(window_start.year, release_dates)
        cached = release_dates

    for d_str in cached:
        try:
            meeting_date = date.fromisoformat(d_str)
        except ValueError:
            continue
        if window_start <= meeting_date <= window_end:
            # Fetch the actual rate decision from FEDFUNDS for that date
            decision = await _get_fomc_decision(client, meeting_date)
            return True, FOMCEvent(
                meeting_date=d_str,
                decision=decision,
            )

    return False, None


async def _get_fomc_decision(client: httpx.AsyncClient, meeting_date: date) -> str:
    """Return a human-readable description of the FOMC rate decision."""
    try:
        data = await _fred_get(
            client, "series/observations",
            {
                "series_id": "FEDFUNDS",
                "observation_start": str(meeting_date),
                "observation_end": str(meeting_date),
                "file_type": "json",
            },
        )
        obs = data.get("observations", [])
        if obs and obs[0].get("value") != ".":
            rate = float(obs[0]["value"])
            return f"Fed funds rate at {rate:.2f}% as of {meeting_date}"
    except Exception:
        pass
    return f"FOMC meeting on {meeting_date} — decision details unavailable"
