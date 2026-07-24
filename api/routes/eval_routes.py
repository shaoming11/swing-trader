"""Eval harness routes — serve calibration, attribution, and regression data.

GET /eval/calibration  — confidence calibration curve
GET /eval/attribution  — per-driver miss rates
GET /eval/regression   — version-over-version comparison
GET /eval/retrieval    — RAG retrieval metrics (requires labeled eval set on disk)
"""
from __future__ import annotations

from fastapi import APIRouter, Query

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
