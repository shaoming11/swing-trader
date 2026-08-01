"""Entry point for the swing-trader pipeline.

Usage:
    python main.py AAPL 2024-01-01 2024-03-31
    python main.py TSLA 2024-06-01 2024-09-30 --run-type backfill
    python main.py --batch
    python main.py --batch --watchlist path/to/watchlist.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from swing_trader.observability.metrics import start_metrics_server
from swing_trader.data_pull.node import data_pull_node
from swing_trader.rag.node import rag_retrieval_node
from swing_trader.reasoning.node import persona_reasoning_node
from swing_trader.reasoning.judge import judge_synthesis_node
from swing_trader.reasoning.guardrails import guardrail_node
from swing_trader.state import initial_state

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("swing_trader")

# LangSmith tracing for the top-level pipeline
_traceable = None
if os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true":
    try:
        from langsmith import traceable
        _traceable = traceable
    except ImportError:
        pass


async def _run_pipeline_inner(
    ticker: str,
    window_start: date,
    window_end: date,
    run_type: str = "live",
    user_id: str | None = None,
    thesis_hint: str | None = None,
) -> dict:
    state = initial_state(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        run_type=run_type,
        user_id=user_id,
        thesis_hint=thesis_hint,
    )

    log.info("run_id=%s  ticker=%s  window=%s→%s", state["run_id"], ticker, window_start, window_end)

    state = await data_pull_node(state)
    log.info("data_pull done  gaps=%s", len(state["numeric_block"].data_gaps) if state["numeric_block"] else "?")

    state = await rag_retrieval_node(state)
    log.info(
        "rag done  chunks_retrieved=%s  chunks_used=%s",
        state["qualitative_block"].chunks_retrieved if state["qualitative_block"] else 0,
        state["qualitative_block"].chunks_used if state["qualitative_block"] else 0,
    )

    state = await persona_reasoning_node(state)
    log.info(
        "persona_reasoning done  personas_filled=%d  prompt_len=%d",
        sum(1 for p in ("bull", "bear", "macro", "technicals")
            if getattr(state["persona_outputs"], p, "")),
        len(state.get("composed_prompt", "")),
    )

    state = await judge_synthesis_node(state)
    log.info(
        "judge_synthesis done  direction=%s  confidence=%s",
        state["layer1_output"].direction if state["layer1_output"] else "FAILED",
        state["layer1_output"].confidence if state["layer1_output"] else "N/A",
    )

    state = await guardrail_node(state)
    checks = state.get("guardrail_checks", [])
    failed = [c for c in checks if not c.passed]
    log.info(
        "guardrails done  checks=%d  failed=%d  retries=%d  cancelled=%s",
        len(checks), len(failed), state.get("guardrail_retries", 0),
        state.get("pipeline_cancelled", False),
    )

    if state.get("warnings"):
        for w in state["warnings"]:
            log.warning(w)

    return state


async def run_pipeline(
    ticker: str,
    window_start: date,
    window_end: date,
    run_type: str = "live",
    user_id: str | None = None,
    thesis_hint: str | None = None,
) -> dict:
    """Top-level entry point — delegates to _run_pipeline_inner with LangSmith
    tracing applied when LANGCHAIN_TRACING_V2=true."""
    fn = _run_pipeline_inner
    if _traceable is not None:
        fn = _traceable(
            name="run_pipeline",
            run_type="chain",
            metadata={
                "ticker": ticker,
                "window_start": str(window_start),
                "window_end": str(window_end),
                "run_type": run_type,
                "user_id": user_id or "system",
                "pipeline_version": os.getenv("PIPELINE_VERSION", "0.0.0"),
            },
            tags=[ticker, run_type, os.getenv("ENVIRONMENT", "development")],
        )(fn)
    return await fn(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        run_type=run_type,
        user_id=user_id,
        thesis_hint=thesis_hint,
    )


def _parse_date(s: str) -> date:
    return date.fromisoformat(s)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing-trader pipeline")
    parser.add_argument("ticker", nargs="?", default=None, help="Stock ticker, e.g. AAPL")
    parser.add_argument("window_start", nargs="?", type=_parse_date, default=None,
                        help="Window start date YYYY-MM-DD (default: last completed quarter)")
    parser.add_argument("window_end", nargs="?", type=_parse_date, default=None,
                        help="Window end date YYYY-MM-DD (default: last completed quarter)")
    parser.add_argument("--run-type", default="live", choices=["live", "backfill", "eval"])
    parser.add_argument("--user-id", default=None)
    parser.add_argument("--thesis-hint", default=None)
    parser.add_argument("--metrics-port", type=int, default=9090)
    parser.add_argument("--no-metrics", action="store_true", help="Skip starting the Prometheus server")
    parser.add_argument("--batch", action="store_true", help="Run all tickers in watchlist sequentially")
    parser.add_argument("--watchlist", default="watchlist.yaml", help="Path to watchlist YAML (default: watchlist.yaml)")
    args = parser.parse_args()

    if not args.no_metrics:
        import os
        os.environ.setdefault("METRICS_PORT", str(args.metrics_port))
        start_metrics_server()
        log.info("Prometheus metrics on :%s/metrics", args.metrics_port)

    if args.batch:
        from swing_trader.batch import run_batch

        results = asyncio.run(run_batch(
            watchlist_path=args.watchlist,
            run_type_override=args.run_type if args.run_type != "live" else None,
        ))
        print(f"\n── Batch summary ({len(results)} tickers) ──")
        for r in results:
            status = r["status"]
            if status == "ok":
                print(f"  {r['ticker']:6s}  {r['direction']:5s}  confidence={r['confidence']:.2f}")
            else:
                print(f"  {r['ticker']:6s}  ERROR")
        return

    if not args.ticker:
        parser.error("ticker is required (or use --batch)")

    state = asyncio.run(
        run_pipeline(
            ticker=args.ticker,
            window_start=args.window_start,
            window_end=args.window_end,
            run_type=args.run_type,
            user_id=args.user_id,
            thesis_hint=args.thesis_hint,
        )
    )

    print("\n── Numeric block ──────────────────────────────────────────")
    if state["numeric_block"]:
        print(state["numeric_block"].rendered_text)
    else:
        print("(none)")

    print("\n── Qualitative block ──────────────────────────────────────")
    if state["qualitative_block"]:
        for item in state["qualitative_block"].items:
            print(f"  [{item.relevance_score:.2f}] {item.date}  {item.source}  {item.text[:120]}…")
    else:
        print("(none)")

    print("\n── Persona outputs ────────────────────────────────────────")
    if state.get("persona_outputs"):
        for persona in ("bull", "bear", "macro", "technicals"):
            text = getattr(state["persona_outputs"], persona, "")
            print(f"\n  [{persona.upper()}]")
            print(f"  {text}" if text else "  (empty)")
    else:
        print("(none)")

    print("\n── Judge synthesis ────────────────────────────────────────")
    if state.get("layer1_output"):
        l1 = state["layer1_output"]
        print(f"  Direction:    {l1.direction}")
        print(f"  Magnitude:    {l1.magnitude_bucket}")
        print(f"  Confidence:   {l1.confidence:.2f}")
        print(f"  Drivers:      {', '.join(l1.dominant_drivers)}")
        print(f"  Hold window:  {l1.hold_window_bucket}")
        print(f"  Thesis:       {l1.thesis}")
        print(f"  Sided with:   {', '.join(l1.sided_with)}")
        print(f"  Invalidation: {l1.invalidation_condition}")
    else:
        print("  (judge synthesis failed -- see warnings)")
        print(f"  Raw reasoning: {state.get('judge_reasoning', '')[:200]}")


if __name__ == "__main__":
    main()
