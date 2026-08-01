"""Window configuration and validation.

FRED release lags by series:
  - Daily (DGS10, T10Y2Y, DCOILWTICO, DHHNGSP): ~1 business day
  - Weekly (MORTGAGE30US): ~1 week
  - Monthly (CPI, FEDFUNDS, UNRATE, UMCSENT, INDPRO, CPIMEDSL, BAMLH0A0HYM2): 2-6 weeks
  - Quarterly (GDP): ~30 days after quarter end

To guarantee all series have data, window_end must fall within a period
where at least one monthly observation has been published.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

# Minimum window width in days — must span a full quarter to guarantee
# at least one GDP observation and one FOMC meeting (~every 45 days).
MIN_WINDOW_DAYS = 90

# window_end must be at least this many days before today to ensure
# monthly FRED series (CPI, FEDFUNDS, UNRATE) have been released.
MIN_RECENCY_DAYS = 45

# Maximum window width in days — keeps data pull fast and focused.
MAX_WINDOW_DAYS = 365


class WindowError(ValueError):
    """Raised when the analysis window is invalid."""


def default_window() -> tuple[date, date]:
    """Return the last fully completed calendar quarter.

    Example: if today is 2026-07-28, returns (2026-04-01, 2026-06-30).
    All FRED series including GDP will have data for a completed quarter.
    """
    today = date.today()
    # Current quarter start
    current_q = (today.month - 1) // 3 + 1
    current_q_start_month = (current_q - 1) * 3 + 1
    current_q_start = date(today.year, current_q_start_month, 1)

    # Previous quarter end = day before current quarter start
    prev_q_end = current_q_start - timedelta(days=1)
    # Previous quarter start
    prev_q = (prev_q_end.month - 1) // 3 + 1
    prev_q_start_month = (prev_q - 1) * 3 + 1
    prev_q_start = date(prev_q_end.year, prev_q_start_month, 1)

    return prev_q_start, prev_q_end


def validate_window(window_start: date, window_end: date) -> None:
    """Raise WindowError if the window won't produce usable FRED data."""
    if window_start >= window_end:
        raise WindowError(
            f"window_start ({window_start}) must be before window_end ({window_end})"
        )

    span = (window_end - window_start).days
    if span < MIN_WINDOW_DAYS:
        raise WindowError(
            f"Window is {span} days — minimum is {MIN_WINDOW_DAYS}. "
            f"A full quarter is needed to guarantee GDP and FOMC observations."
        )

    if span > MAX_WINDOW_DAYS:
        raise WindowError(
            f"Window is {span} days — maximum is {MAX_WINDOW_DAYS}. "
            f"Use a narrower window for focused analysis."
        )

    today = date.today()
    days_ago = (today - window_end).days
    if days_ago < MIN_RECENCY_DAYS:
        safe_end = today - timedelta(days=MIN_RECENCY_DAYS)
        raise WindowError(
            f"window_end ({window_end}) is too recent — FRED monthly series "
            f"have a 2-6 week release lag. Use window_end <= {safe_end} "
            f"or omit dates to use the last completed quarter."
        )
