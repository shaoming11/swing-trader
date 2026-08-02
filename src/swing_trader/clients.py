"""Central LLM and embedding client factory.

All modules import from here — never instantiate clients inline.
To swap providers, change this file only.

Current backend:
  - Embeddings: Jina AI (cloud, free — 1M tokens/month)
  - Fast/mid LLM: Groq (cloud, free)
  - Judge LLM: Groq (cloud, free) — handles the 70B model
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

# ── Groq (all LLM inference) ──────────────────────────────────────────────────
_GROQ_BASE = "https://api.groq.com/openai/v1"
_GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Jina AI (embeddings + reranker) ──────────────────────────────────────────
_JINA_BASE = "https://api.jina.ai/v1"
JINA_API_KEY = os.getenv("JINA_API_KEY", "")
_JINA_API_KEY = JINA_API_KEY  # backward compat for internal use

# ── Model names ───────────────────────────────────────────────────────────────

# 768-dim — same dimensionality as nomic-embed-text, no vector store rebuild needed
EMBED_MODEL = os.getenv("EMBED_MODEL", "jina-embeddings-v2-base-en")

# Fast/cheap: sentiment tagging, classification — Groq free tier
LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "llama-3.2-3b-preview")

# Mid-tier: persona reasoning calls — Groq free tier
LLM_MID_MODEL = os.getenv("LLM_MID_MODEL", "llama-3.1-8b-instant")

# Judge synthesis: Groq free tier
LLM_JUDGE_MODEL = os.getenv("LLM_JUDGE_MODEL", "llama-3.3-70b-versatile")


def get_llm_client() -> AsyncOpenAI:
    """OpenAI-compatible client for fast/mid models — points at Groq cloud."""
    return AsyncOpenAI(base_url=_GROQ_BASE, api_key=_GROQ_API_KEY)


def get_judge_client() -> AsyncOpenAI:
    """OpenAI-compatible client for judge synthesis — points at Groq cloud."""
    return AsyncOpenAI(base_url=_GROQ_BASE, api_key=_GROQ_API_KEY)


def get_embed_client() -> AsyncOpenAI:
    """OpenAI-compatible client for embeddings — points at Jina AI cloud."""
    return AsyncOpenAI(base_url=_JINA_BASE, api_key=_JINA_API_KEY)


# ── To swap back to Anthropic + OpenAI: ──────────────────────────────────────
#
# EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
# LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "claude-haiku-4-5-20251001")
# LLM_MID_MODEL = os.getenv("LLM_MID_MODEL", "claude-sonnet-4-6")
# LLM_JUDGE_MODEL = os.getenv("LLM_JUDGE_MODEL", "claude-opus-4-6")
#
# Use anthropic.AsyncAnthropic() for get_llm_client() and get_judge_client().
# Note: Anthropic response format differs — update call sites in retriever.py
# (response.content[0].text vs response.choices[0].message.content).
