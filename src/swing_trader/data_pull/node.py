"""LangGraph node: data_pull.

Fetches fundamentals (yfinance) and macro (FRED) data quarter by quarter.
Each quarter is cached to disk at cache/quarters/{ticker}_{Q}.json on first
fetch; subsequent runs load from cache instantly.

Both pulls are fully synchronous internally (yfinance and requests) and run
concurrently in the thread pool via asyncio.to_thread. The asyncio event loop
is never touched by network I/O, which prevents the OSError / RetryError that
appeared when httpx competed with yfinance inside uvicorn's event loop.
"""
from __future__ import annotations

import asyncio
import calendar
from datetime import date

from swing_trader.data_pull import cache as disk_cache
from swing_trader.data_pull.block import build_numeric_block
from swing_trader.data_pull.fundamentals import pull_fundamentals
from swing_trader.data_pull.macro import pull_macro
from swing_trader.observability.decorators import observe_node
from swing_trader.observability.langsmith_config import add_node_metadata
from swing_trader.schemas.pipeline import FundamentalsResult, MacroResult
from swing_trader.state import PipelineState


@observe_node("data_pull")
async def data_pull_node(state: PipelineState) -> PipelineState:
    ticker = state["ticker"]
    window_start = state["window_start"]
    window_end = state["window_end"]

    warnings = list(state.get("warnings", []))
    fetched: list[tuple[str, date, date, FundamentalsResult, MacroResult]] = []

    for qlabel, qstart, qend in _iter_quarters(window_start, window_end):
        cached = disk_cache.get_quarter(ticker, qlabel)

        if cached is not None:
            fundamentals = FundamentalsResult.model_validate(cached["fundamentals"])
            macro = MacroResult.model_validate(cached["macro"])
        else:
            # Both pulls run in the thread pool concurrently.
            # pull_fundamentals uses asyncio.to_thread internally.
            # pull_macro is now fully sync, so wrap it here.
            fundamentals, macro = await asyncio.gather(
                pull_fundamentals(ticker, qstart, qend),
                asyncio.to_thread(pull_macro, ticker, qstart, qend),
            )
            disk_cache.set_quarter(ticker, qlabel, {
                "fundamentals": fundamentals.model_dump(mode="json"),
                "macro": macro.model_dump(mode="json"),
            })

        fetched.append((qlabel, qstart, qend, fundamentals, macro))

        for gap in fundamentals.data_gaps + macro.data_gaps:
            if gap not in warnings:
                warnings.append(gap)

    block = build_numeric_block(ticker, window_start, window_end, fetched)

    add_node_metadata({
        "quarters": [q[0] for q in fetched],
        "sources_used": block.sources_used,
        "data_gaps_count": len(block.data_gaps),
        "macro_series_pulled": block.macro_series_pulled,
    })

    return {**state, "numeric_block": block, "warnings": warnings}


# ── Quarter enumeration ───────────────────────────────────────────────────────

def _iter_quarters(
    start: date,
    end: date,
) -> list[tuple[str, date, date]]:
    """Return (label, qstart, qend) for every calendar quarter overlapping [start, end]."""
    results = []
    y = start.year
    q = (start.month - 1) // 3 + 1

    while True:
        qm_start = (q - 1) * 3 + 1
        qm_end = q * 3
        qstart = date(y, qm_start, 1)
        qend = date(y, qm_end, calendar.monthrange(y, qm_end)[1])

        if qstart > end:
            break

        results.append((f"{y}Q{q}", max(qstart, start), min(qend, end)))

        q += 1
        if q > 4:
            q, y = 1, y + 1

    return results
