"""Macro pull — FRED API with sector-relevant indicator selection.

Pulls CPI, Fed funds rate, unemployment, GDP, and sector-specific indicators
for the given date window. Also detects FOMC meetings within the window.
No LLM involved.

Uses `requests` (synchronous) instead of httpx so the asyncio event loop is
never touched. Call via asyncio.to_thread from async contexts.
"""

from __future__ import annotations

import os
import time
from datetime import date
from typing import Any

import requests

from swing_trader.data_pull import cache as disk_cache
from swing_trader.schemas.pipeline import FOMCEvent, MacroDataPoint, MacroResult

_FRED = "https://api.stlouisfed.org/fred"

# ── Sector → FRED series mapping ──────────────────────────────────────────────

_SECTOR_SERIES: dict[str, list[tuple[str, str, str]]] = {
    "Technology": [
        ("DGS10", "10-Year Treasury Yield", "%"),
        ("FEDFUNDS", "Fed Funds Rate", "%"),
        ("CPIAUCSL", "CPI (index)", ""),
    ],
    "Financials": [
        ("FEDFUNDS", "Fed Funds Rate", "%"),
        ("DGS10", "10-Year Treasury Yield", "%"),
        ("T10Y2Y", "10Y-2Y Yield Spread", "%"),
        ("BAMLH0A0HYM2", "HY OAS Spread", "bps"),
    ],
    "Real Estate": [
        ("FEDFUNDS", "Fed Funds Rate", "%"),
        ("DGS10", "10-Year Treasury Yield", "%"),
        ("MORTGAGE30US", "30-Year Mortgage Rate", "%"),
    ],
    "Consumer Discretionary": [
        ("UNRATE", "Unemployment Rate", "%"),
        ("UMCSENT", "Consumer Sentiment", "index"),
        ("CPIAUCSL", "CPI (index)", ""),
    ],
    "Energy": [
        ("DCOILWTICO", "WTI Crude Oil", "$/bbl"),
        ("DHHNGSP", "Natural Gas", "$/MMBtu"),
    ],
    "Healthcare": [
        ("CPIMEDSL", "Medical CPI (index)", ""),
        ("FEDFUNDS", "Fed Funds Rate", "%"),
    ],
    "Industrials": [
        ("INDPRO", "Industrial Production Index", "index"),
        ("DCOILWTICO", "WTI Crude Oil", "$/bbl"),
    ],
}

_DEFAULT_SERIES: list[tuple[str, str, str]] = [
    ("CPIAUCSL", "CPI (index)", ""),
    ("FEDFUNDS", "Fed Funds Rate", "%"),
    ("UNRATE", "Unemployment Rate", "%"),
    ("GDP", "GDP", "$B"),
]

_FOMC_RELEASE_ID = "82"
_TIMEOUT = 15  # seconds per request
_MAX_RETRIES = 3


def _fred_key() -> str:
    key = os.getenv("FRED_API_KEY", "")
    if not key:
        raise EnvironmentError("FRED_API_KEY is not set")
    return key


def _fred_get(endpoint: str, params: dict) -> Any:
    """Synchronous FRED request with simple retry."""
    url = f"{_FRED}/{endpoint}"
    full_params = {**params, "api_key": _fred_key(), "file_type": "json"}
    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(url, params=full_params, timeout=_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                time.sleep(2**attempt)
    raise last_exc  # type: ignore[misc]


def pull_macro(
    ticker: str,
    window_start: date,
    window_end: date,
    sector: str | None = None,
) -> MacroResult:
    """Pull macro indicators for the sector. Synchronous — run via asyncio.to_thread."""
    series_list = _SECTOR_SERIES.get(sector or "", _DEFAULT_SERIES) or _DEFAULT_SERIES

    gaps: list[str] = []
    pulled: list[MacroDataPoint] = []
    fomc_in_window = False
    fomc_event: FOMCEvent | None = None

    for series_id, label, unit in series_list:
        dp = _fetch_series(series_id, label, unit, window_start, window_end, gaps)
        if dp:
            pulled.append(dp)

    try:
        fomc_in_window, fomc_event = _detect_fomc(window_start, window_end)
    except Exception as e:
        gaps.append(f"FOMC detection: {e}")

    return MacroResult(
        sector=sector or "default",
        series=pulled,
        fomc_in_window=fomc_in_window,
        fomc_event=fomc_event,
        data_gaps=gaps,
    )


def _fetch_series(
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
        data = _fred_get(
            "series/observations",
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

        def _val(o: dict) -> float | None:
            try:
                return float(o["value"])
            except (KeyError, ValueError, TypeError):
                return None

        dp = MacroDataPoint(
            series_id=series_id,
            label=label,
            value_start=_val(obs[0]),
            value_end=_val(obs[-1]),
            date_start=obs[0].get("date"),
            date_end=obs[-1].get("date"),
            unit=unit,
        )
        disk_cache.set_macro(series_id, year_month, dp.model_dump(mode="json"))
        return dp
    except Exception as e:
        gaps.append(f"FRED {series_id}: {e}")
        return None


def _detect_fomc(
    window_start: date,
    window_end: date,
) -> tuple[bool, FOMCEvent | None]:
    cached = disk_cache.get_fomc(window_start.year)
    if cached is None:
        data = _fred_get(
            "release/dates",
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
            decision = _get_fomc_decision(meeting_date)
            return True, FOMCEvent(meeting_date=d_str, decision=decision)

    return False, None


def _get_fomc_decision(meeting_date: date) -> str:
    try:
        data = _fred_get(
            "series/observations",
            {
                "series_id": "FEDFUNDS",
                "observation_start": str(meeting_date),
                "observation_end": str(meeting_date),
            },
        )
        obs = data.get("observations", [])
        if obs and obs[0].get("value") != ".":
            rate = float(obs[0]["value"])
            return f"Fed funds rate at {rate:.2f}% as of {meeting_date}"
    except Exception:
        pass
    return f"FOMC meeting on {meeting_date} — decision details unavailable"
