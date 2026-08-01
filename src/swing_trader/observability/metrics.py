"""Prometheus metrics for the swing trader pipeline.

All counters and histograms are registered globally at import time.
Start the metrics HTTP server with start_metrics_server() at app startup.
"""
from __future__ import annotations

import os

from prometheus_client import Counter, Histogram, start_http_server

# ── Pipeline-level ────────────────────────────────────────────────────────────

PIPELINE_RUNS_TOTAL = Counter(
    "pipeline_runs_total",
    "Total pipeline runs",
    ["ticker", "run_type", "outcome"],  # outcome: completed | cancelled | failed
)

PIPELINE_DURATION_SECONDS = Histogram(
    "pipeline_duration_seconds",
    "End-to-end pipeline duration in seconds",
    ["run_type"],
    buckets=[5, 10, 30, 60, 120, 300],
)

# ── Node-level ────────────────────────────────────────────────────────────────

NODE_DURATION_SECONDS = Histogram(
    "node_duration_seconds",
    "Per-node execution duration in seconds",
    ["node_name"],
    buckets=[0.5, 1, 2, 5, 10, 30],
)

# ── LLM cost ─────────────────────────────────────────────────────────────────

LLM_TOKENS_TOTAL = Counter(
    "llm_tokens_total",
    "Total tokens consumed",
    ["node_name", "model", "token_type"],  # token_type: input | output
)

LLM_COST_USD_TOTAL = Counter(
    "llm_cost_usd_total",
    "Estimated LLM cost in USD",
    ["node_name", "model"],
)

# ── RAG ───────────────────────────────────────────────────────────────────────

RAG_CHUNKS_RETRIEVED = Histogram(
    "rag_chunks_retrieved",
    "Number of chunks retrieved before rerank",
    buckets=[0, 5, 10, 20, 50],
)

RAG_CHUNKS_USED = Histogram(
    "rag_chunks_used",
    "Number of chunks used after rerank and dedup",
    buckets=[0, 2, 5, 10],
)

RAG_EMPTY_BLOCK_TOTAL = Counter(
    "rag_empty_block_total",
    "Times retrieval returned no usable chunks",
    ["ticker"],
)

# ── Guardrails ────────────────────────────────────────────────────────────────

GUARDRAIL_CHECKS_TOTAL = Counter(
    "guardrail_checks_total",
    "Guardrail check executions",
    ["check_name", "result"],  # result: pass | fail
)

GUARDRAIL_RETRIES_TOTAL = Counter(
    "guardrail_retries_total",
    "Judge call retries triggered by guardrail failures",
    ["reason"],
)

PIPELINE_CANCELLATIONS_TOTAL = Counter(
    "pipeline_cancellations_total",
    "Pipeline runs cancelled after guardrail exhaustion",
    ["reason"],
)

# ── Layer 1 outputs ───────────────────────────────────────────────────────────

CONFIDENCE_DISTRIBUTION = Histogram(
    "layer1_confidence",
    "Distribution of Layer 1 confidence scores",
    buckets=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
)

DIRECTION_TOTAL = Counter(
    "layer1_direction_total",
    "Layer 1 direction outputs",
    ["direction"],  # bullish | bearish | neutral
)

# ── Layer 2 gate ──────────────────────────────────────────────────────────────

LAYER2_GATE_TOTAL = Counter(
    "layer2_gate_total",
    "Layer 2 gate decisions",
    ["decision"],  # acted | skipped
)

# ── Cost table ────────────────────────────────────────────────────────────────
# Groq + Jina free tiers — cost is $0. Table kept for when switching back
# to paid providers; unknown models default to $0.

_MODEL_COST_PER_1K: dict[str, dict[str, float]] = {
    # Groq free tier
    "llama-3.3-70b-versatile":    {"input": 0.0, "output": 0.0},
    "llama-3.1-8b-instant":       {"input": 0.0, "output": 0.0},
    "llama-3.2-3b-preview":       {"input": 0.0, "output": 0.0},
    # Jina AI free tier
    "jina-embeddings-v2-base-en": {"input": 0.0, "output": 0.0},
    # Anthropic (restore when switching back)
    "claude-opus-4-6":   {"input": 0.015,   "output": 0.075},
    "claude-sonnet-4-6": {"input": 0.003,   "output": 0.015},
    "claude-haiku-4-5-20251001": {"input": 0.00025, "output": 0.00125},
    # OpenAI (restore when switching back)
    "text-embedding-3-small": {"input": 0.00002, "output": 0.0},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _MODEL_COST_PER_1K.get(model, {"input": 0.0, "output": 0.0})
    return (input_tokens / 1000 * rates["input"]) + (output_tokens / 1000 * rates["output"])


def record_llm_usage(node_name: str, model: str, response) -> None:
    """Extract token counts from an Anthropic response and update metrics."""
    try:
        usage = response.usage
        input_tok = getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", 0)
        output_tok = getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", 0)
        LLM_TOKENS_TOTAL.labels(node_name=node_name, model=model, token_type="input").inc(input_tok)
        LLM_TOKENS_TOTAL.labels(node_name=node_name, model=model, token_type="output").inc(output_tok)
        cost = estimate_cost(model, input_tok, output_tok)
        LLM_COST_USD_TOTAL.labels(node_name=node_name, model=model).inc(cost)
    except Exception:
        pass  # metrics are never allowed to crash the pipeline


def start_metrics_server() -> None:
    port = int(os.getenv("METRICS_PORT", "9090"))
    start_http_server(port)
