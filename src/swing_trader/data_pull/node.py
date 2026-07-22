"""LangGraph node: data_pull.

Runs fundamentals and macro pulls in parallel via asyncio.gather.
Partial failures degrade gracefully — a gap is noted in the block's NOTES
section, and the pipeline continues with whatever data is available.
"""
from __future__ import annotations

import asyncio
import os

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

    fundamentals_task = pull_fundamentals(ticker, window_start, window_end)
    macro_task = pull_macro(ticker, window_start, window_end)

    fundamentals, macro = await asyncio.gather(
        fundamentals_task, macro_task, return_exceptions=True
    )

    warnings = list(state.get("warnings", []))

    if isinstance(fundamentals, Exception):
        warnings.append(f"data_pull: fundamentals failed — {fundamentals}")
        fundamentals = FundamentalsResult.empty(ticker)

    if isinstance(macro, Exception):
        warnings.append(f"data_pull: macro failed — {macro}")
        macro = MacroResult.empty()

    block = build_numeric_block(ticker, window_start, window_end, fundamentals, macro)

    add_node_metadata({
        "sources_used": block.sources_used,
        "data_gaps_count": len(block.data_gaps),
        "fundamentals_report_date": str(block.fundamentals_report_date),
        "macro_series_pulled": block.macro_series_pulled,
    })

    return {
        **state,
        "numeric_block": block,
        "warnings": warnings,
    }
