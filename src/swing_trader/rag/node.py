"""LangGraph node: rag_retrieval."""

from __future__ import annotations

from swing_trader.observability.decorators import observe_node
from swing_trader.observability.langsmith_config import add_node_metadata
from swing_trader.observability.metrics import (
    RAG_CHUNKS_RETRIEVED,
    RAG_CHUNKS_USED,
    RAG_EMPTY_BLOCK_TOTAL,
)
from swing_trader.rag.retriever import retrieve
from swing_trader.state import PipelineState


@observe_node("rag_retrieval")
async def rag_retrieval_node(state: PipelineState) -> PipelineState:
    block = await retrieve(
        ticker=state["ticker"],
        window_start=state["window_start"],
        window_end=state["window_end"],
        thesis_hint=state.get("thesis_hint"),
    )

    RAG_CHUNKS_RETRIEVED.observe(block.chunks_retrieved)
    RAG_CHUNKS_USED.observe(block.chunks_used)

    warnings = list(state.get("warnings", []))
    if block.chunks_used == 0:
        RAG_EMPTY_BLOCK_TOTAL.labels(ticker=state["ticker"]).inc()
        warnings.append(
            "rag_retrieval: no relevant chunks found — reasoning will rely on structured data only"
        )

    add_node_metadata(
        {
            "chunks_retrieved": block.chunks_retrieved,
            "chunks_used": block.chunks_used,
            "top_reranker_score": max((i.relevance_score for i in block.items), default=0),
            "empty_block": block.chunks_used == 0,
        }
    )

    return {
        **state,
        "qualitative_block": block,
        "warnings": warnings,
    }
