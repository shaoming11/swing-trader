"""Eval harness routes — serve calibration, attribution, regression, and
self-improvement loop streaming.

GET  /eval/calibration    — confidence calibration curve
GET  /eval/attribution    — per-driver miss rates
GET  /eval/regression     — version-over-version comparison
POST /eval/self-improve   — trigger self-improvement loop (SSE stream)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()


async def _load_eval_records(
    ticker: str | None = None,
    pipeline_version: str | None = None,
):
    """Fetch eval records from Postgres and convert to EvalRecord objects."""
    from swing_trader.db.pool import get_pool
    from swing_trader.eval.harness import EvalRecord

    pool = await get_pool()
    conditions = []
    params: list = []

    if ticker:
        params.append(ticker.upper())
        conditions.append(f"ticker = ${len(params)}")
    if pipeline_version:
        params.append(pipeline_version)
        conditions.append(f"pipeline_version = ${len(params)}")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT run_id, ticker, pipeline_version, confidence, direction,
                   magnitude_bucket, dominant_drivers, guardrail_retries,
                   pipeline_cancelled, actual_direction, actual_magnitude_pct,
                   hit, price_target_error_pct, timing_result
            FROM pipeline_runs
            {where}
            ORDER BY completed_at DESC NULLS LAST
            LIMIT 5000
            """
        )

    return [EvalRecord.from_dict(dict(row)) for row in rows]


@router.get("/calibration")
async def calibration(
    ticker: str | None = Query(None),
    pipeline_version: str | None = Query(None),
):
    """Confidence calibration curve — stated confidence vs. actual hit rate per bucket."""
    from swing_trader.eval.harness import calibration_curve
    records = await _load_eval_records(ticker, pipeline_version)
    curve = calibration_curve(records)
    return {
        "total_records": len(records),
        "records_with_ground_truth": sum(1 for r in records if r.has_ground_truth),
        **curve.to_dict(),
    }


@router.get("/attribution")
async def attribution(
    ticker: str | None = Query(None),
    pipeline_version: str | None = Query(None),
):
    """Per-driver miss rates — which input type (fundamental/macro/sentiment/technical)
    is most associated with wrong calls."""
    from swing_trader.eval.harness import feature_attribution, price_target_error_by_ticker
    records = await _load_eval_records(ticker, pipeline_version)
    attrs = feature_attribution(records)
    price_errors = price_target_error_by_ticker(records)
    return {
        "total_records": len(records),
        "driver_miss_rates": [
            {
                "driver": a.driver,
                "total_calls": a.total_calls,
                "misses": a.misses,
                "miss_rate": round(a.miss_rate, 3),
            }
            for a in attrs
        ],
        "price_target_error_by_ticker": price_errors,
    }


@router.get("/regression")
async def regression(ticker: str | None = Query(None)):
    """Version-over-version hit rate / cancellation rate comparison.
    Flags regression when hit rate drops >5pp or cancellation rate rises >10pp.
    """
    from swing_trader.eval.harness import regression_comparison, timing_distribution
    records = await _load_eval_records(ticker)
    return {
        "total_records": len(records),
        "versions": regression_comparison(records),
        "timing_distribution": timing_distribution(records),
    }


# ── Self-improvement loop (SSE) ──────────────────────────────────────────────

class SelfImproveRequest(BaseModel):
    tickers: list[str] | None = None
    quarters: list[str] | None = None
    max_iterations: int = 5


# Only allow one loop at a time
_loop_running = False


@router.post("/self-improve")
async def self_improve(req: SelfImproveRequest):
    """Trigger the self-improvement loop and stream iteration events via SSE."""
    global _loop_running
    if _loop_running:
        return StreamingResponse(
            _single_event({"event": "error", "message": "A self-improvement loop is already running."}),
            media_type="text/event-stream",
        )

    from swing_trader.eval.self_improve import (
        default_eval_quarters,
        run_eval_loop_streaming,
    )

    quarters = req.quarters or default_eval_quarters()
    tickers: list[str]
    if req.tickers:
        tickers = [t.upper() for t in req.tickers]
    else:
        try:
            from swing_trader.batch import load_watchlist
            wl = load_watchlist("watchlist.yaml")
            tickers = wl["tickers"]
        except Exception:
            tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN"]

    q: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict):
        event["ts"] = time.time()
        await q.put(event)

    async def run_loop():
        global _loop_running
        _loop_running = True
        try:
            await run_eval_loop_streaming(
                tickers=tickers,
                quarters=quarters,
                max_iterations=req.max_iterations,
                emit=emit,
            )
        except Exception as exc:
            await q.put({"event": "error", "message": str(exc), "ts": time.time()})
        finally:
            await q.put(None)
            _loop_running = False

    asyncio.create_task(run_loop())

    async def event_generator() -> AsyncIterator[str]:
        while True:
            try:
                event = await asyncio.wait_for(q.get(), timeout=600.0)
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'event': 'timeout'})}\n\n"
                break
            if event is None:
                yield f"data: {json.dumps({'event': 'stream_closed'})}\n\n"
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _single_event(event: dict) -> AsyncIterator[str]:
    yield f"data: {json.dumps(event)}\n\n"
