"""@observe_node decorator — wraps a LangGraph node function with timing
metrics AND LangSmith tracing (via @traceable).
"""

from __future__ import annotations

import asyncio
import os
import time
from functools import wraps
from typing import Callable

from swing_trader.observability.metrics import NODE_DURATION_SECONDS

# Only import traceable when LangSmith is enabled to avoid import overhead.
_TRACING_ENABLED = os.getenv("LANGCHAIN_TRACING_V2", "").lower() == "true"

if _TRACING_ENABLED:
    try:
        from langsmith import traceable as _traceable
    except ImportError:
        _TRACING_ENABLED = False


def observe_node(node_name: str):
    """Decorator that records NODE_DURATION_SECONDS and creates a LangSmith
    span for a LangGraph node.

    Works for both sync and async node functions.

    Usage:
        @observe_node("rag_retrieval")
        async def rag_retrieval_node(state: PipelineState) -> PipelineState:
            ...
    """

    def decorator(fn: Callable) -> Callable:
        if asyncio.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(state):
                start = time.perf_counter()
                try:
                    return await fn(state)
                finally:
                    NODE_DURATION_SECONDS.labels(node_name=node_name).observe(
                        time.perf_counter() - start
                    )

            wrapped = async_wrapper
        else:

            @wraps(fn)
            def sync_wrapper(state):
                start = time.perf_counter()
                try:
                    return fn(state)
                finally:
                    NODE_DURATION_SECONDS.labels(node_name=node_name).observe(
                        time.perf_counter() - start
                    )

            wrapped = sync_wrapper

        # Layer LangSmith tracing on top if enabled
        if _TRACING_ENABLED:
            wrapped = _traceable(name=node_name, run_type="chain")(wrapped)

        return wrapped

    return decorator
