"""Central LLM and embedding client factory.

All modules import from here — never instantiate clients inline.
To swap providers, change this file only.

Current backend:
  - Embeddings + fast/mid LLM: Ollama (local)
  - Judge LLM: Groq (cloud, free) — handles the 70B model without local disk cost
"""
from __future__ import annotations

import os

from openai import AsyncOpenAI

# ── Ollama (local) ────────────────────────────────────────────────────────────
_OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")

# ── Groq (cloud judge) ────────────────────────────────────────────────────────
_GROQ_BASE = "https://api.groq.com/openai/v1"
_GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# ── Model names ───────────────────────────────────────────────────────────────

EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")

# Fast/cheap: sentiment tagging, classification — local Ollama
LLM_FAST_MODEL = os.getenv("LLM_FAST_MODEL", "llama3.2:3b")

# Mid-tier: persona reasoning calls — local Ollama
LLM_MID_MODEL = os.getenv("LLM_MID_MODEL", "llama3.1:8b")

# Judge synthesis: runs on Groq cloud (free, no local disk needed)
LLM_JUDGE_MODEL = os.getenv("LLM_JUDGE_MODEL", "llama-3.3-70b-versatile")


def get_llm_client() -> AsyncOpenAI:
    """OpenAI-compatible client for fast/mid models — points at local Ollama."""
    return AsyncOpenAI(base_url=_OLLAMA_BASE, api_key="ollama")


def get_judge_client() -> AsyncOpenAI:
    """OpenAI-compatible client for judge synthesis — points at Groq cloud.

    Falls back to local Ollama if GROQ_API_KEY is not set (useful for testing
    with a smaller local model by also overriding LLM_JUDGE_MODEL in .env).
    """
    if _GROQ_API_KEY:
        return AsyncOpenAI(base_url=_GROQ_BASE, api_key=_GROQ_API_KEY)
    return AsyncOpenAI(base_url=_OLLAMA_BASE, api_key="ollama")


def get_embed_client() -> AsyncOpenAI:
    """OpenAI-compatible client for embeddings — points at local Ollama."""
    return AsyncOpenAI(base_url=_OLLAMA_BASE, api_key="ollama")


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
