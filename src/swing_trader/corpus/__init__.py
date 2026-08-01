"""Corpus generator — builds and maintains the RAG corpus from external sources."""
from swing_trader.corpus.generator import backfill, live_run

__all__ = ["backfill", "live_run"]
