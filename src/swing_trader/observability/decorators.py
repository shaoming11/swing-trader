"""@observe_node decorator — wraps a LangGraph node function with timing metrics."""
from __future__ import annotations

import asyncio
import time
from functools import wraps
from typing import Callable

from swing_trader.observability.metrics import NODE_DURATION_SECONDS


def observe_node(node_name: str):
    """Decorator that records NODE_DURATION_SECONDS for a LangGraph node.

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
            return async_wrapper
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
            return sync_wrapper
    return decorator
