"""Ground truth computation and per-run scoring.

Fetches actual price data for a window, computes what *actually* happened,
and scores a pipeline prediction against reality.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import yfinance as yf

log = logging.getLogger(__name__)


# ── Ground truth ─────────────────────────────────────────────────────────────


@dataclass
class GroundTruth:
    """What actually happened to the stock in the window."""

    ticker: str
    window_start: date
    window_end: date
    price_start: float | None
    price_end: float | None
    actual_return_pct: float | None  # (end - start) / start * 100
    actual_direction: str | None  # bullish | bearish | neutral
    actual_magnitude_bucket: str | None  # 0-3% | 3-8% | 8%+


def fetch_ground_truth(ticker: str, window_start: date, window_end: date) -> GroundTruth:
    """Fetch actual price data and compute ground truth for a window.

    Uses the closing price on (or near) window_start and window_end.
    """
    # Fetch a slightly wider range to handle weekends/holidays
    fetch_start = window_start - timedelta(days=5)
    fetch_end = window_end + timedelta(days=5)

    try:
        df = yf.download(
            ticker,
            start=fetch_start.isoformat(),
            end=fetch_end.isoformat(),
            progress=False,
        )
    except Exception as exc:
        log.warning("yfinance download failed for %s: %s", ticker, exc)
        return GroundTruth(
            ticker=ticker,
            window_start=window_start,
            window_end=window_end,
            price_start=None,
            price_end=None,
            actual_return_pct=None,
            actual_direction=None,
            actual_magnitude_bucket=None,
        )

    if df.empty:
        return GroundTruth(
            ticker=ticker,
            window_start=window_start,
            window_end=window_end,
            price_start=None,
            price_end=None,
            actual_return_pct=None,
            actual_direction=None,
            actual_magnitude_bucket=None,
        )

    # Find closest trading day on or after window_start
    close = df["Close"].dropna()
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]

    start_prices = close[close.index >= str(window_start)]
    end_prices = close[close.index <= str(window_end)]

    if start_prices.empty or end_prices.empty:
        return GroundTruth(
            ticker=ticker,
            window_start=window_start,
            window_end=window_end,
            price_start=None,
            price_end=None,
            actual_return_pct=None,
            actual_direction=None,
            actual_magnitude_bucket=None,
        )

    price_start = float(start_prices.iloc[0])
    price_end = float(end_prices.iloc[-1])
    ret_pct = (price_end - price_start) / price_start * 100

    abs_ret = abs(ret_pct)
    if abs_ret < 3:
        direction = "neutral"
        mag_bucket = "0-3%"
    elif abs_ret < 8:
        direction = "bullish" if ret_pct > 0 else "bearish"
        mag_bucket = "3-8%"
    else:
        direction = "bullish" if ret_pct > 0 else "bearish"
        mag_bucket = "8%+"

    return GroundTruth(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        price_start=price_start,
        price_end=price_end,
        actual_return_pct=round(ret_pct, 2),
        actual_direction=direction,
        actual_magnitude_bucket=mag_bucket,
    )


# ── Per-run scoring ──────────────────────────────────────────────────────────


@dataclass
class RunScore:
    """Score a single pipeline run against ground truth."""

    ticker: str
    predicted_direction: str
    predicted_magnitude: str
    predicted_confidence: float
    actual_direction: str
    actual_magnitude: str
    actual_return_pct: float

    direction_correct: bool
    magnitude_correct: bool
    price_target_error_pct: float  # abs(predicted implied move - actual move)
    hit: bool  # direction_correct AND not neutral-when-it-moved

    @property
    def high_confidence(self) -> bool:
        return self.predicted_confidence >= 0.7


def score_run(
    predicted_direction: str,
    predicted_magnitude: str,
    predicted_confidence: float,
    ground_truth: GroundTruth,
) -> RunScore | None:
    """Score a single prediction against ground truth. Returns None if no ground truth."""
    if ground_truth.actual_direction is None or ground_truth.actual_return_pct is None:
        return None

    dir_correct = predicted_direction == ground_truth.actual_direction
    mag_correct = predicted_magnitude == ground_truth.actual_magnitude_bucket

    # "hit" = got the direction right, unless we said neutral and it moved 3%+
    hit = dir_correct
    if predicted_direction == "neutral" and ground_truth.actual_magnitude_bucket != "0-3%":
        hit = False

    # Price target error: use midpoint of predicted bucket vs actual return
    bucket_midpoints = {"0-3%": 1.5, "3-8%": 5.5, "8%+": 12.0}
    predicted_move = bucket_midpoints.get(predicted_magnitude, 5.5)
    if predicted_direction == "bearish":
        predicted_move = -predicted_move
    elif predicted_direction == "neutral":
        predicted_move = 0.0
    price_error = abs(predicted_move - ground_truth.actual_return_pct)

    return RunScore(
        ticker=ground_truth.ticker,
        predicted_direction=predicted_direction,
        predicted_magnitude=predicted_magnitude,
        predicted_confidence=predicted_confidence,
        actual_direction=ground_truth.actual_direction,
        actual_magnitude=ground_truth.actual_magnitude_bucket,
        actual_return_pct=ground_truth.actual_return_pct,
        direction_correct=dir_correct,
        magnitude_correct=mag_correct,
        price_target_error_pct=round(price_error, 2),
        hit=hit,
    )
