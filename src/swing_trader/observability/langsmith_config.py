"""LangSmith tracing configuration and trace URL capture.

LangSmith tracing is enabled automatically via environment variables when
LANGCHAIN_TRACING_V2=true. This module handles run metadata tagging and
trace URL retrieval for storage in the eval store.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.runnables import RunnableConfig


def build_run_config(
    ticker: str,
    window_start: str,
    window_end: str,
    user_id: str | None,
    run_type: str,
    quarter: str,
) -> RunnableConfig:
    """Build the RunnableConfig to pass to pipeline.invoke().

    Tags and metadata make every run filterable in LangSmith by ticker,
    user, quarter, and environment.
    """
    environment = os.getenv("ENVIRONMENT", "development")
    pipeline_version = os.getenv("PIPELINE_VERSION", "0.0.0")

    return RunnableConfig(
        metadata={
            "ticker": ticker,
            "window_start": window_start,
            "window_end": window_end,
            "user_id": user_id or "system",
            "run_type": run_type,
            "pipeline_version": pipeline_version,
        },
        tags=[ticker, quarter, environment, pipeline_version],
    )


def get_trace_url(run_id: str) -> str | None:
    """Return the public LangSmith trace URL for a completed run.

    Returns None if LangSmith is not configured or the run cannot be found.
    """
    try:
        from langsmith import Client

        client = Client()
        run = client.read_run(run_id)
        return f"https://smith.langchain.com/public/{run.id}/r"
    except Exception:
        return None


def add_node_metadata(metadata: dict[str, Any]) -> None:
    """Attach structured metadata to the current LangSmith span.

    Call inside a LangGraph node to enrich the trace with node-specific data
    (e.g., chunk counts, guardrail check results).
    """
    try:
        from langsmith.run_helpers import get_current_run_tree

        run = get_current_run_tree()
        if run:
            run.add_metadata(metadata)
    except Exception:
        pass  # never let observability crash the pipeline
