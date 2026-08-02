"""Batch orchestrator — loop through a watchlist of tickers sequentially."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import yaml

log = logging.getLogger("swing_trader.batch")


def load_watchlist(path: str | Path) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not data or "tickers" not in data:
        raise ValueError(f"watchlist at {path} must contain a 'tickers' list")
    return data


async def run_batch(
    watchlist_path: str | Path = "watchlist.yaml",
    run_type_override: str | None = None,
) -> list[dict]:
    """Run the pipeline for every ticker in the watchlist, sequentially with delays."""
    from main import run_pipeline  # deferred to avoid circular import

    wl = load_watchlist(watchlist_path)
    defaults = wl.get("defaults", {})
    window_days = defaults.get("window_days", 90)
    delay_seconds = defaults.get("delay_seconds", 30)
    run_type = run_type_override or defaults.get("run_type", "live")

    tickers: list[str] = wl["tickers"]
    today = date.today()
    window_start = today - timedelta(days=window_days)
    window_end = today

    log.info(
        "batch start  tickers=%d  window=%s→%s  delay=%ds",
        len(tickers),
        window_start,
        window_end,
        delay_seconds,
    )

    results: list[dict] = []
    successes = 0
    failures = 0

    for i, ticker in enumerate(tickers):
        t0 = time.monotonic()
        try:
            log.info("[%d/%d] %s — starting", i + 1, len(tickers), ticker)
            state = await run_pipeline(
                ticker=ticker,
                window_start=window_start,
                window_end=window_end,
                run_type=run_type,
            )
            elapsed = time.monotonic() - t0
            direction = state["layer1_output"].direction if state.get("layer1_output") else "FAILED"
            confidence = state["layer1_output"].confidence if state.get("layer1_output") else None
            log.info(
                "[%d/%d] %s — done in %.1fs  direction=%s  confidence=%s",
                i + 1,
                len(tickers),
                ticker,
                elapsed,
                direction,
                confidence,
            )
            results.append(
                {"ticker": ticker, "status": "ok", "direction": direction, "confidence": confidence}
            )
            successes += 1
        except Exception:
            elapsed = time.monotonic() - t0
            log.exception("[%d/%d] %s — failed after %.1fs", i + 1, len(tickers), ticker, elapsed)
            results.append({"ticker": ticker, "status": "error"})
            failures += 1

        # delay between tickers (skip after last)
        if i < len(tickers) - 1:
            log.info("sleeping %ds before next ticker", delay_seconds)
            await asyncio.sleep(delay_seconds)

    log.info("batch complete  successes=%d  failures=%d", successes, failures)
    return results
