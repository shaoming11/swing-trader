"""RAG retriever — four-stage pipeline: hard filter → embed search → rerank → sentiment tag.

Usage:
    block = await retrieve(ticker="AAPL", window_start=date(2024,1,1), window_end=date(2024,3,31))
    prompt_text = block.render()
"""
from __future__ import annotations

import json
from datetime import date
from typing import cast

import httpx

from swing_trader.clients import EMBED_MODEL, JINA_API_KEY, LLM_FAST_MODEL, get_embed_client, get_llm_client
from swing_trader.rag.store import get_vector_store
from swing_trader.schemas.pipeline import QualItem, QualitativeBlock, SentimentLabel
_RERANKER_SCORE_THRESHOLD = 0.2  # Jina reranker returns normalized 0-1 scores
_JINA_RERANK_URL = "https://api.jina.ai/v1/rerank"
_JINA_RERANK_MODEL = "jina-reranker-v2-base-multilingual"
_MAX_QUAL_BLOCK_TOKENS = 1500
_SUMMARY_MAX_CHARS = 800  # ~200 tokens


async def retrieve(
    ticker: str,
    window_start: date,
    window_end: date,
    thesis_hint: str | None = None,
    top_k_before_rerank: int = 200,
    top_k_after_rerank: int = 10,
) -> QualitativeBlock:
    """Run the full four-stage retrieval pipeline and return a QualitativeBlock."""
    store = get_vector_store()

    # Stage 1 — hard metadata filter (applied inside vector store search)
    hard_filter = {
        "ticker": ticker.upper(),
        "date_gte": str(window_start),
        "date_lte": str(window_end),
        "active": True,
    }

    # Stage 2 — embedding search
    query_text = f"{ticker} stock price movement drivers {window_start} to {window_end}"
    if thesis_hint:
        query_text += f". Focus: {thesis_hint}"

    query_embedding = await _embed(query_text)
    raw_results = store.search(query_embedding, hard_filter, top_k=top_k_before_rerank)

    # Fallback: if date-filtered search returns nothing, retry without dates
    # so the pipeline still gets qualitative context from the nearest available data.
    if not raw_results:
        fallback_filter = {k: v for k, v in hard_filter.items() if k not in ("date_gte", "date_lte")}
        raw_results = store.search(query_embedding, fallback_filter, top_k=top_k_before_rerank)

    chunks_retrieved = len(raw_results)

    if not raw_results:
        return QualitativeBlock(
            ticker=ticker,
            window_start=window_start,
            window_end=window_end,
            chunks_retrieved=0,
            chunks_used=0,
        )

    # Stage 3 — Jina reranker (cloud API)
    reranked = await _rerank(query_text, raw_results, top_n=top_k_after_rerank)

    # Drop chunks below the relevance threshold
    reranked = [r for r in reranked if r["rerank_score"] >= _RERANKER_SCORE_THRESHOLD]

    # Deduplicate: same file → keep highest-scoring chunk
    reranked = _deduplicate(reranked)

    if not reranked:
        return QualitativeBlock(
            ticker=ticker,
            window_start=window_start,
            window_end=window_end,
            chunks_retrieved=chunks_retrieved,
            chunks_used=0,
        )

    # Stage 4 — sentiment tag pass (for any chunks missing sentiment_label)
    reranked = await _ensure_sentiment(ticker, reranked)

    # Build QualItems
    items = [_to_qual_item(r) for r in reranked]

    # Truncate to token budget (rough: 1 token ≈ 4 chars)
    items = _truncate_to_budget(items, _MAX_QUAL_BLOCK_TOKENS)

    return QualitativeBlock(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        chunks_retrieved=chunks_retrieved,
        chunks_used=len(items),
        items=items,
    )


async def _embed(text: str) -> list[float]:
    client = get_embed_client()
    resp = await client.embeddings.create(model=EMBED_MODEL, input=[text])
    return resp.data[0].embedding


async def _rerank(query: str, results: list[dict], top_n: int) -> list[dict]:
    """Rerank using Jina Reranker API (cloud, returns 0-1 scores)."""
    try:
        documents = [r["content"] for r in results]
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                _JINA_RERANK_URL,
                headers={
                    "Authorization": f"Bearer {JINA_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _JINA_RERANK_MODEL,
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        for item in data["results"]:
            idx = item["index"]
            results[idx]["rerank_score"] = float(item["relevance_score"])

        scored = [r for r in results if "rerank_score" in r]
        return sorted(scored, key=lambda x: x["rerank_score"], reverse=True)[:top_n]
    except Exception:
        # Fall back to embedding similarity score on API failure
        for r in results:
            r["rerank_score"] = r.get("score", 0.0)
        return sorted(results, key=lambda x: x["rerank_score"], reverse=True)[:top_n]


def _deduplicate(results: list[dict]) -> list[dict]:
    """Keep only the highest-scoring chunk per source file."""
    seen: dict[str, dict] = {}
    for r in results:
        fp = r.get("metadata", {}).get("file_path", "")
        if fp not in seen or r["rerank_score"] > seen[fp]["rerank_score"]:
            seen[fp] = r
    return list(seen.values())


async def _ensure_sentiment(ticker: str, results: list[dict]) -> list[dict]:
    """Run LLM sentiment tagging on any chunks missing sentiment_label."""
    untagged = [r for r in results if not r.get("metadata", {}).get("sentiment_label")]
    if not untagged:
        return results

    texts = [r["content"][:400] for r in untagged]  # truncate for API call
    prompt = (
        f"Ticker: {ticker}\n\n"
        f"Tag each item below as it relates to the stock's price outlook. "
        f"Return a JSON array in the same order as the items.\n"
        f"Items:\n{json.dumps(texts)}\n\n"
        f"For each item return exactly: "
        f'{{\"sentiment_label\": \"bullish|bearish|neutral\", \"sentiment_reason\": \"one sentence\"}}'
    )

    try:
        client = get_llm_client()
        response = await client.chat.completions.create(
            model=LLM_FAST_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        # Extract JSON array
        start = raw.find("[")
        end = raw.rfind("]") + 1
        tags = json.loads(raw[start:end])

        for r, tag in zip(untagged, tags):
            r.setdefault("metadata", {})
            r["metadata"]["sentiment_label"] = tag.get("sentiment_label", "neutral")
            r["metadata"]["sentiment_reason"] = tag.get("sentiment_reason", "")
    except Exception:
        # Fail silently — missing sentiment is not fatal
        for r in untagged:
            r.setdefault("metadata", {})
            r["metadata"].setdefault("sentiment_label", "neutral")
            r["metadata"].setdefault("sentiment_reason", "")

    return results


def _to_qual_item(r: dict) -> QualItem:
    meta = r.get("metadata", {})
    content = r.get("content", "")
    summary = content[:_SUMMARY_MAX_CHARS]
    if len(content) > _SUMMARY_MAX_CHARS:
        summary = summary.rsplit(" ", 1)[0] + "…"

    return QualItem(
        source_type=meta.get("source_type", "news"),
        date=meta.get("date", ""),
        source=meta.get("source", ""),
        sentiment_label=cast(SentimentLabel, meta.get("sentiment_label", "neutral")),
        sentiment_reason=meta.get("sentiment_reason", ""),
        summary=summary,
        relevance_score=r.get("rerank_score", 0.0),
    )


def _truncate_to_budget(items: list[QualItem], max_tokens: int) -> list[QualItem]:
    """Truncate items to fit within the token budget.

    Priority order: analyst > news > macro > social (as per spec).
    """
    priority = {"analyst": 0, "news": 1, "macro": 2, "social": 3}
    items_sorted = sorted(items, key=lambda x: priority.get(x.source_type, 4))

    budget_chars = max_tokens * 4  # rough
    kept = []
    used = 0
    for item in items_sorted:
        size = len(item.summary) + len(item.sentiment_reason) + 50
        if used + size > budget_chars:
            break
        kept.append(item)
        used += size

    return kept
