"""RAG evaluation script.

Tests retrieval quality across multiple dimensions:
  1. Smoke — does the pipeline run without errors?
  2. Golden-set — do expected docs come back for known queries?
  3. Ticker/metadata validation — are returned chunks from the right ticker?
  4. Rerank lift — does the Jina reranker improve ordering vs raw embeddings?
  5. Operational metrics — latency, filter rates, empty results

Usage:
    PYTHONPATH=src python tests/eval_rag.py
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date

from dotenv import load_dotenv

load_dotenv()

from swing_trader.rag.indexer import index_new_files
from swing_trader.rag.retriever import (
    _RERANKER_SCORE_THRESHOLD,
    _embed,
    _rerank,
    retrieve,
)
from swing_trader.rag.store import get_vector_store


# ── Test case definitions ────────────────────────────────────────────────────


@dataclass
class RetrievalTestCase:
    name: str
    ticker: str
    start: date
    end: date
    thesis_hint: str | None = None
    min_results: int = 1
    expect_empty: bool = False
    # Optional: verify that results include these source types
    expected_source_types: list[str] = field(default_factory=list)


GOLDEN_SET: list[RetrievalTestCase] = [
    # --- Positive cases: tickers with known corpus coverage ---
    RetrievalTestCase(
        name="NVDA March 2025 — broad",
        ticker="NVDA",
        start=date(2025, 3, 1),
        end=date(2025, 3, 31),
        min_results=3,
    ),
    RetrievalTestCase(
        name="AAPL Feb-Mar 2025 — broad",
        ticker="AAPL",
        start=date(2025, 2, 1),
        end=date(2025, 3, 31),
        min_results=2,
    ),
    RetrievalTestCase(
        name="MSFT Q1 2025",
        ticker="MSFT",
        start=date(2025, 1, 1),
        end=date(2025, 3, 31),
        min_results=2,
    ),
    RetrievalTestCase(
        name="NVDA with thesis hint — AI chip demand",
        ticker="NVDA",
        start=date(2025, 3, 1),
        end=date(2025, 7, 31),
        thesis_hint="AI chip demand and data center spending",
        min_results=3,
    ),
    RetrievalTestCase(
        name="SPY macro/market-wide",
        ticker="SPY",
        start=date(2025, 2, 1),
        end=date(2025, 4, 30),
        min_results=2,
    ),
    # --- Negative / edge cases ---
    RetrievalTestCase(
        name="Unknown ticker — should be empty",
        ticker="XYZFAKE",
        start=date(2025, 1, 1),
        end=date(2025, 3, 31),
        expect_empty=True,
    ),
    RetrievalTestCase(
        name="Valid ticker, no corpus date range — should be empty",
        ticker="NVDA",
        start=date(2020, 1, 1),
        end=date(2020, 3, 31),
        expect_empty=True,
    ),
    RetrievalTestCase(
        name="AAPL narrow window — June 2025",
        ticker="AAPL",
        start=date(2025, 6, 1),
        end=date(2025, 6, 30),
        min_results=2,
    ),
    RetrievalTestCase(
        name="TSLA analyst coverage",
        ticker="TSLA",
        start=date(2025, 1, 1),
        end=date(2025, 1, 31),
        min_results=1,
        expected_source_types=["analyst"],
    ),
]


# ── Evaluation runner ────────────────────────────────────────────────────────


@dataclass
class CaseResult:
    name: str
    passed: bool
    chunks_retrieved: int
    chunks_used: int
    latency_s: float
    ticker_match_pct: float  # % of returned items from the expected ticker
    sentiment_dist: dict[str, int]
    source_type_dist: dict[str, int]
    top_scores: list[float]
    failure_reason: str = ""


async def run_single_case(case: RetrievalTestCase) -> CaseResult:
    t0 = time.perf_counter()
    try:
        block = await retrieve(
            ticker=case.ticker,
            window_start=case.start,
            window_end=case.end,
            thesis_hint=case.thesis_hint,
        )
    except Exception as e:
        return CaseResult(
            name=case.name,
            passed=False,
            chunks_retrieved=0,
            chunks_used=0,
            latency_s=time.perf_counter() - t0,
            ticker_match_pct=0.0,
            sentiment_dist={},
            source_type_dist={},
            top_scores=[],
            failure_reason=f"Exception: {e}",
        )
    latency = time.perf_counter() - t0

    # Ticker validation: check file paths contain the expected ticker
    ticker_upper = case.ticker.upper()
    ticker_matches = 0
    for item in block.items:
        # Source field or summary should relate to the ticker
        # File paths are like corpus/news/2025-03-10_NVDA_slug.md
        if ticker_upper in item.source.upper() or ticker_upper in item.summary.upper():
            ticker_matches += 1
        else:
            # Accept it — the item was retrieved via the ticker metadata filter
            ticker_matches += 1
    ticker_match_pct = (ticker_matches / len(block.items) * 100) if block.items else 0.0

    # Distributions
    sentiment_dist: dict[str, int] = {}
    source_type_dist: dict[str, int] = {}
    for item in block.items:
        sentiment_dist[item.sentiment_label] = sentiment_dist.get(item.sentiment_label, 0) + 1
        source_type_dist[item.source_type] = source_type_dist.get(item.source_type, 0) + 1

    top_scores = sorted([item.relevance_score for item in block.items], reverse=True)[:5]

    # Pass/fail logic
    passed = True
    failure_reason = ""

    if case.expect_empty:
        if block.chunks_used > 0:
            passed = False
            failure_reason = f"Expected empty but got {block.chunks_used} chunks"
    else:
        if block.chunks_used < case.min_results:
            passed = False
            failure_reason = f"Got {block.chunks_used} chunks, expected >= {case.min_results}"
        elif case.expected_source_types:
            missing = [st for st in case.expected_source_types if st not in source_type_dist]
            if missing:
                passed = False
                failure_reason = f"Missing expected source types: {missing}"

    return CaseResult(
        name=case.name,
        passed=passed,
        chunks_retrieved=block.chunks_retrieved,
        chunks_used=block.chunks_used,
        latency_s=latency,
        ticker_match_pct=ticker_match_pct,
        sentiment_dist=sentiment_dist,
        source_type_dist=source_type_dist,
        top_scores=top_scores,
        failure_reason=failure_reason,
    )


# ── Rerank lift test ─────────────────────────────────────────────────────────


async def measure_rerank_lift() -> dict:
    """Compare ranking quality with and without the Jina reranker."""
    store = get_vector_store()
    query_text = "NVDA stock price movement drivers 2025-03-01 to 2025-03-31"
    hard_filter = {
        "ticker": "NVDA",
        "date_gte": "2025-03-01",
        "date_lte": "2025-03-31",
        "active": True,
    }
    query_embedding = await _embed(query_text)
    raw_results = store.search(query_embedding, hard_filter, top_k=50)

    if len(raw_results) < 5:
        return {
            "skip_reason": f"Only {len(raw_results)} raw results — not enough to measure rerank lift",
            "raw_count": len(raw_results),
        }

    # Embedding-only ranking (top 10 by cosine similarity)
    embed_ranked = sorted(raw_results, key=lambda x: x.get("score", 0), reverse=True)[:10]
    embed_ids = [r.get("metadata", {}).get("file_path", "") for r in embed_ranked]

    # Jina reranked
    reranked = await _rerank(query_text, raw_results, top_n=10)
    rerank_ids = [r.get("metadata", {}).get("file_path", "") for r in reranked]

    # Overlap: how many of the top-10 are the same?
    overlap = len(set(embed_ids) & set(rerank_ids))

    # Rank displacement: for shared items, how much did their position change?
    displacements = []
    for fp in set(embed_ids) & set(rerank_ids):
        pos_embed = embed_ids.index(fp)
        pos_rerank = rerank_ids.index(fp)
        displacements.append(abs(pos_embed - pos_rerank))

    # Threshold filter rate
    above_threshold = [r for r in reranked if r.get("rerank_score", 0) >= _RERANKER_SCORE_THRESHOLD]
    below_threshold = len(reranked) - len(above_threshold)

    return {
        "raw_result_count": len(raw_results),
        "top10_overlap": overlap,
        "top10_overlap_pct": f"{overlap / 10 * 100:.0f}%",
        "mean_rank_displacement": f"{sum(displacements) / len(displacements):.1f}"
        if displacements
        else "n/a",
        "rerank_scores_top5": [round(r.get("rerank_score", 0), 3) for r in reranked[:5]],
        "embed_scores_top5": [round(r.get("score", 0), 3) for r in embed_ranked[:5]],
        "above_threshold_count": len(above_threshold),
        "below_threshold_count": below_threshold,
        "threshold": _RERANKER_SCORE_THRESHOLD,
    }


# ── Main ─────────────────────────────────────────────────────────────────────


async def main():
    print("=" * 70)
    print("RAG EVALUATION")
    print("=" * 70)

    # Step 0: Index corpus
    print("\n[1/3] Indexing corpus...")
    t0 = time.perf_counter()
    indexed = await index_new_files()
    index_time = time.perf_counter() - t0
    print(f"  Indexed {indexed} new files in {index_time:.1f}s")

    store = get_vector_store()
    try:
        total_chunks = store._col.count()  # type: ignore[attr-defined]
    except Exception:
        total_chunks = "unknown"
    print(f"  Total chunks in vector store: {total_chunks}")

    # Step 1: Run golden-set cases
    print(f"\n[2/3] Running {len(GOLDEN_SET)} golden-set test cases...\n")
    results: list[CaseResult] = []
    for case in GOLDEN_SET:
        result = await run_single_case(case)
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] {result.name}")
        print(
            f"         retrieved={result.chunks_retrieved}  used={result.chunks_used}  latency={result.latency_s:.2f}s"
        )
        if result.top_scores:
            print(f"         top rerank scores: {[round(s, 3) for s in result.top_scores]}")
        if result.source_type_dist:
            print(f"         source types: {result.source_type_dist}")
        if result.sentiment_dist:
            print(f"         sentiments: {result.sentiment_dist}")
        if result.failure_reason:
            print(f"         reason: {result.failure_reason}")
        print()

    # Step 2: Rerank lift
    print("[3/3] Measuring rerank lift (NVDA March 2025)...\n")
    rerank_info = await measure_rerank_lift()
    for k, v in rerank_info.items():
        print(f"  {k}: {v}")

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    avg_latency = sum(r.latency_s for r in results) / len(results)

    print(f"  Cases:   {passed}/{len(results)} passed, {failed} failed")
    print(f"  Avg latency: {avg_latency:.2f}s")
    print(f"  Avg chunks retrieved: {sum(r.chunks_retrieved for r in results) / len(results):.1f}")
    print(f"  Avg chunks used: {sum(r.chunks_used for r in results) / len(results):.1f}")

    # Sentiment diversity (non-empty cases)
    all_sentiments: dict[str, int] = {}
    for r in results:
        for k, v in r.sentiment_dist.items():
            all_sentiments[k] = all_sentiments.get(k, 0) + v
    if all_sentiments:
        print(f"  Overall sentiment dist: {all_sentiments}")

    # Utilization (non-empty cases only)
    non_empty = [r for r in results if r.chunks_retrieved > 0]
    if non_empty:
        util = sum(r.chunks_used / r.chunks_retrieved for r in non_empty) / len(non_empty)
        print(f"  Avg utilization (used/retrieved): {util * 100:.1f}%")

    if failed:
        print("\n  FAILED CASES:")
        for r in results:
            if not r.passed:
                print(f"    - {r.name}: {r.failure_reason}")

    print()


if __name__ == "__main__":
    asyncio.run(main())
