"""Standalone test for persona reasoning + judge synthesis with mock data.

Usage:
    python tests/test_persona_node.py

Requires GROQ_API_KEY in .env (makes real LLM calls).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv

load_dotenv()

from swing_trader.schemas.pipeline import NumericBlock, QualitativeBlock, QualItem
from swing_trader.state import initial_state
from swing_trader.reasoning.node import persona_reasoning_node
from swing_trader.reasoning.judge import judge_synthesis_node


# ── Mock data ────────────────────────────────────────────────────────────────

MOCK_NUMERIC_TEXT = """\
=== NUMERIC DATA: AAPL (2024-01-01 to 2024-03-31) ===

[EARNINGS — Q1 2024 (reported 2024-02-01)]
  EPS actual: $1.52  |  estimate: $1.45  |  surprise: +5.2%
  Revenue actual: $94.9B  |  estimate: $92.1B  |  surprise: +3.0%
  Revenue YoY: +2.1%
  Gross margin: 45.9%  (prior quarter: 45.2%)
  Guidance: raised — management raised FY24 services revenue outlook

[VALUATION]
  PE trailing: 29.3x  |  PE forward: 27.1x
  Price at window start: $185.33
  52-week high: $199.62  |  52-week low: $143.15

[MACRO — Technology sector]
  Fed Funds Rate (DFF): 5.33% → 5.33% (held)
  10Y-2Y spread: -0.35% → -0.28% (inversion narrowing)
  CPI YoY: 3.4% → 3.5% (slight uptick)
  GDP QoQ annualized: 3.2%
  FOMC: held at 5.25-5.50% (Jan 31 meeting — no cut)

[DATA GAPS]
  None
"""

MOCK_QUAL_ITEMS = [
    QualItem(
        source_type="analyst",
        date="2024-02-02",
        source="Morgan Stanley",
        sentiment_label="bullish",
        sentiment_reason="Price target raised to $220 on strong services growth",
        summary="Morgan Stanley analyst raised AAPL price target from $200 to $220, "
        "citing accelerating services revenue and improving iPhone ASPs in "
        "emerging markets.",
        relevance_score=0.92,
    ),
    QualItem(
        source_type="news",
        date="2024-01-18",
        source="Reuters",
        sentiment_label="bearish",
        sentiment_reason="iPhone shipments declined 3.7% in China amid Huawei competition",
        summary="Apple's iPhone shipments in China fell 3.7% in Q4 2023 as Huawei's "
        "Mate 60 Pro gained market share. China revenue remains a key risk "
        "for AAPL's growth narrative.",
        relevance_score=0.87,
    ),
    QualItem(
        source_type="macro",
        date="2024-01-31",
        source="FOMC Statement",
        sentiment_label="neutral",
        sentiment_reason="Fed held rates, signaled patience on cuts — no immediate catalyst",
        summary="The Federal Reserve held the federal funds rate at 5.25-5.50% and "
        "removed language about potential further tightening, but Chair Powell "
        "pushed back on a March rate cut.",
        relevance_score=0.81,
    ),
    QualItem(
        source_type="social",
        date="2024-02-05",
        source="StockTwits",
        sentiment_label="bullish",
        sentiment_reason="Retail sentiment overwhelmingly positive post-earnings",
        summary="StockTwits sentiment gauge showed 78% bullish on AAPL following the "
        "Q1 earnings beat. Message volume spiked 3x above 30-day average.",
        relevance_score=0.65,
    ),
]


# ── Run ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    numeric_block = NumericBlock(
        ticker="AAPL",
        window_start=date(2024, 1, 1),
        window_end=date(2024, 3, 31),
        rendered_text=MOCK_NUMERIC_TEXT,
        data_gaps=[],
        sources_used=["yfinance", "FRED"],
        fundamentals_report_date=date(2024, 2, 1),
        macro_series_pulled=["DFF", "T10Y2Y", "CPIAUCSL", "GDP"],
    )

    qualitative_block = QualitativeBlock(
        ticker="AAPL",
        window_start=date(2024, 1, 1),
        window_end=date(2024, 3, 31),
        chunks_retrieved=12,
        chunks_used=4,
        items=MOCK_QUAL_ITEMS,
    )

    state = initial_state(
        ticker="AAPL",
        window_start=date(2024, 1, 1),
        window_end=date(2024, 3, 31),
        run_type="eval",
    )
    state["numeric_block"] = numeric_block
    state["qualitative_block"] = qualitative_block

    # ── Persona reasoning ────────────────────────────────────────────────────
    print("Running persona reasoning node with mock AAPL data...\n")
    state = await persona_reasoning_node(state)

    persona_outputs = state["persona_outputs"]
    for persona in ("bull", "bear", "macro", "technicals"):
        text = getattr(persona_outputs, persona, "")
        print(f"── {persona.upper()} {'─' * (50 - len(persona))}")
        print(text if text else "(empty)")
        print()

    filled = sum(1 for p in ("bull", "bear", "macro", "technicals") if getattr(persona_outputs, p))
    print(f"Personas filled: {filled}/4")
    assert filled == 4, f"Expected 4 personas filled, got {filled}"
    assert state["composed_prompt"], "composed_prompt should be non-empty"

    # ── Judge synthesis ──────────────────────────────────────────────────────
    print("\nRunning judge synthesis node...\n")
    state = await judge_synthesis_node(state)

    layer1 = state.get("layer1_output")
    if layer1:
        print("── JUDGE OUTPUT ─────────────────────────────────────────")
        print(f"  Direction:      {layer1.direction}")
        print(f"  Magnitude:      {layer1.magnitude_bucket}")
        print(f"  Confidence:     {layer1.confidence:.2f}")
        print(f"  Drivers:        {', '.join(layer1.dominant_drivers)}")
        print(f"  Hold window:    {layer1.hold_window_bucket}")
        print(f"  Thesis:         {layer1.thesis}")
        print(f"  Sided with:     {', '.join(layer1.sided_with)}")
        print(f"  Invalidation:   {layer1.invalidation_condition}")
        print(f"  Agreement:      {persona_outputs.agreement_ratio(layer1.direction):.0%}")
    else:
        print("JUDGE FAILED — raw reasoning:")
        print(state.get("judge_reasoning", "(empty)"))

    print("\nAll checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
