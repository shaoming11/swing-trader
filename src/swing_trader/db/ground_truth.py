"""Ground truth population job.

Run daily (via cron or scheduler) after hold windows close.
Fetches the actual exit price for completed trades and computes
direction, magnitude, hit/miss, and price target error.

Usage:
    python -m swing_trader.db.ground_truth
"""
from __future__ import annotations

import asyncio
import os
from datetime import date

import httpx

from swing_trader.db.pool import close_pool, get_pool

_POLYGON = "https://api.polygon.io/v2/aggs"

_MAGNITUDE_BUCKET_MIDPOINTS = {
    "0-3%": 1.5,
    "3-8%": 5.5,
    "8%+": 12.0,
}


async def populate_ground_truth() -> int:
    """Populate ground truth for all runs where the hold window has closed.

    Returns the number of records updated.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        pending = await conn.fetch(
            """
            SELECT run_id, ticker, hold_window_start, hold_window_end,
                   entry_price, target_price, direction, magnitude_bucket
            FROM pipeline_runs
            WHERE ground_truth_populated = FALSE
              AND gate_passed = TRUE
              AND pipeline_cancelled = FALSE
              AND hold_window_end <= CURRENT_DATE
              AND hold_window_start IS NOT NULL
              AND hold_window_end IS NOT NULL
            """
        )

    updated = 0
    async with httpx.AsyncClient() as client:
        for run in pending:
            try:
                await _update_run(client, dict(run))
                updated += 1
            except Exception as e:
                print(f"[ground_truth] failed for {run['run_id']}: {e}")

    return updated


async def _update_run(client: httpx.AsyncClient, run: dict) -> None:
    ticker = run["ticker"]
    entry_date: date = run["hold_window_start"]
    exit_date: date = run["hold_window_end"]

    actual_entry = await _fetch_close(client, ticker, entry_date)
    actual_exit = await _fetch_close(client, ticker, exit_date)

    if actual_entry is None or actual_exit is None:
        return

    actual_magnitude = (actual_exit - actual_entry) / actual_entry * 100

    if actual_magnitude > 1:
        actual_direction = "bullish"
    elif actual_magnitude < -1:
        actual_direction = "bearish"
    else:
        actual_direction = "neutral"

    hit = (
        actual_direction == run["direction"]
        and _in_magnitude_bucket(actual_magnitude, run["magnitude_bucket"])
    )

    predicted_mid = _MAGNITUDE_BUCKET_MIDPOINTS.get(run["magnitude_bucket"] or "", None)
    price_target_error = None
    if predicted_mid is not None and actual_magnitude != 0:
        price_target_error = (predicted_mid - actual_magnitude) / abs(actual_magnitude) * 100

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE pipeline_runs SET
                actual_price_at_entry   = $1,
                actual_price_at_exit    = $2,
                actual_direction        = $3,
                actual_magnitude_pct    = $4,
                hit                     = $5,
                price_target_error_pct  = $6,
                ground_truth_populated  = TRUE
            WHERE run_id = $7
            """,
            actual_entry,
            actual_exit,
            actual_direction,
            round(actual_magnitude, 2),
            hit,
            round(price_target_error, 1) if price_target_error is not None else None,
            run["run_id"],
        )


async def _fetch_close(
    client: httpx.AsyncClient, ticker: str, target_date: date
) -> float | None:
    """Fetch the closing price on or nearest to target_date from Polygon."""
    api_key = os.getenv("POLYGON_API_KEY", "")
    from datetime import timedelta
    # Search ±5 days to handle weekends/holidays
    start = target_date - timedelta(days=5)
    end = target_date + timedelta(days=1)

    resp = await client.get(
        f"{_POLYGON}/ticker/{ticker}/range/1/day/{start}/{end}",
        params={"adjusted": "true", "sort": "desc", "limit": "5", "apikey": api_key},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])

    # Return the closest available close on or before target_date
    target_ord = target_date.toordinal()
    for r in results:
        from datetime import datetime
        r_date = datetime.utcfromtimestamp(r["t"] / 1000).date()
        if r_date.toordinal() <= target_ord:
            return float(r["c"])
    return None


def _in_magnitude_bucket(magnitude: float, bucket: str | None) -> bool:
    if bucket is None:
        return False
    abs_mag = abs(magnitude)
    if bucket == "0-3%":
        return abs_mag < 3
    if bucket == "3-8%":
        return 3 <= abs_mag < 8
    if bucket == "8%+":
        return abs_mag >= 8
    return False


if __name__ == "__main__":
    async def _main():
        n = await populate_ground_truth()
        print(f"Updated {n} records")
        await close_pool()

    asyncio.run(_main())
