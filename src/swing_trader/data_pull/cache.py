"""Disk-based JSON cache for API responses.

Historical data is cached permanently. Current-quarter data has a 24-hour TTL.
Cache hits skip the API call entirely.

Cache layout:
    cache/
      fundamentals/{ticker}_{quarter}.json       e.g. AAPL_2024Q1.json
      macro/{series_id}_{YYYY-MM}.json           e.g. CPIAUCSL_2024-03.json
      price/{ticker}_{window_start}_{window_end}.json
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_CACHE_ROOT = Path(os.getenv("CACHE_DIR", "cache"))
_TTL_SECONDS = 24 * 3600  # 24-hour TTL for current-quarter data


def _is_historical(window_end: date) -> bool:
    return window_end < date.today() - timedelta(days=90)


def _read(path: Path, ttl: float | None) -> Any | None:
    if not path.exists():
        return None
    if ttl is not None and (time.time() - path.stat().st_mtime) > ttl:
        return None
    with path.open() as f:
        return json.load(f)


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(data, f, default=str)


def get_fundamentals(ticker: str, quarter: str) -> Any | None:
    """quarter format: '2024Q1'"""
    path = _CACHE_ROOT / "fundamentals" / f"{ticker}_{quarter}.json"
    # Fundamentals from closed quarters are permanent
    ttl = None if _is_historical(_quarter_end(quarter)) else _TTL_SECONDS
    return _read(path, ttl)


def set_fundamentals(ticker: str, quarter: str, data: Any) -> None:
    path = _CACHE_ROOT / "fundamentals" / f"{ticker}_{quarter}.json"
    _write(path, data)


def get_macro(series_id: str, year_month: str) -> Any | None:
    """year_month format: '2024-03'"""
    path = _CACHE_ROOT / "macro" / f"{series_id}_{year_month}.json"
    ref_date = date.fromisoformat(f"{year_month}-01")
    ttl = None if _is_historical(ref_date) else _TTL_SECONDS
    return _read(path, ttl)


def set_macro(series_id: str, year_month: str, data: Any) -> None:
    path = _CACHE_ROOT / "macro" / f"{series_id}_{year_month}.json"
    _write(path, data)


def get_price(ticker: str, window_start: date, window_end: date) -> Any | None:
    key = f"{ticker}_{window_start}_{window_end}"
    path = _CACHE_ROOT / "price" / f"{key}.json"
    ttl = None if _is_historical(window_end) else _TTL_SECONDS
    return _read(path, ttl)


def set_price(ticker: str, window_start: date, window_end: date, data: Any) -> None:
    key = f"{ticker}_{window_start}_{window_end}"
    path = _CACHE_ROOT / "price" / f"{key}.json"
    _write(path, data)


def get_fomc(year: int) -> Any | None:
    path = _CACHE_ROOT / "macro" / f"FOMC_{year}.json"
    ttl = None if year < date.today().year else _TTL_SECONDS
    return _read(path, ttl)


def set_fomc(year: int, data: Any) -> None:
    path = _CACHE_ROOT / "macro" / f"FOMC_{year}.json"
    _write(path, data)


# ── Quarter-level cache (fundamentals + macro merged per quarter) ─────────────


def get_quarter(ticker: str, quarter: str) -> Any | None:
    """quarter format: '2024Q4'. Returns combined {fundamentals, macro} dict or None."""
    path = _CACHE_ROOT / "quarters" / f"{ticker}_{quarter}.json"
    ttl = None if _is_historical(_quarter_end(quarter)) else _TTL_SECONDS
    return _read(path, ttl)


def set_quarter(ticker: str, quarter: str, data: Any) -> None:
    path = _CACHE_ROOT / "quarters" / f"{ticker}_{quarter}.json"
    _write(path, data)


def _quarter_end(quarter: str) -> date:
    """'2024Q1' -> date(2024, 3, 31)"""
    year, q = int(quarter[:4]), int(quarter[5])
    month_end = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    last_day = {3: 31, 6: 30, 9: 30, 12: 31}[month_end]
    return date(year, month_end, last_day)
