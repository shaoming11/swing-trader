"""Run management routes.

POST  /runs                 — trigger a pipeline run (returns run_id immediately)
GET   /runs/{run_id}/stream — SSE stream of node events for a live run
GET   /runs/{run_id}        — fetch stored run from eval store
GET   /runs                 — list past runs with optional filters
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from typing import AsyncIterator

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

# ── In-memory SSE queues (per run_id, cleared after stream closes) ─────────────
_run_queues: dict[str, asyncio.Queue] = {}


# ── Request / response models ──────────────────────────────────────────────────

class RunRequest(BaseModel):
    ticker: str
    window_start: date | None = None
    window_end: date | None = None
    run_type: str = "live"
    thesis_hint: str | None = None
    user_id: str | None = None


class RunResponse(BaseModel):
    run_id: str
    ticker: str
    window_start: date
    window_end: date
    run_type: str


# ── Background pipeline task ───────────────────────────────────────────────────

async def _run_pipeline(run_id: str, req: RunRequest) -> None:
    """Execute the pipeline and publish SSE events for each node."""
    q = _run_queues.get(run_id)

    async def emit(event: dict) -> None:
        if q:
            await q.put(event)

    # Pre-flight: validate all pipeline imports before touching any node state.
    # ImportError here means a missing Python dependency — surface it clearly.
    try:
        from swing_trader.state import initial_state
        from swing_trader.data_pull.node import data_pull_node
        from swing_trader.rag.node import rag_retrieval_node
        from swing_trader.reasoning.node import persona_reasoning_node
        from swing_trader.reasoning.judge import judge_synthesis_node
    except ImportError as exc:
        pkg = str(exc).replace("No module named ", "").strip("'\"")
        await emit({
            "event": "error",
            "message": (
                f"Missing Python dependency: {pkg}. "
                "Run `pip install -e .` in the project root, then restart uvicorn."
            ),
            "ts": time.time(),
        })
        if q:
            await q.put(None)
        return

    try:
        state = initial_state(
            ticker=req.ticker,
            window_start=req.window_start,
            window_end=req.window_end,
            run_type=req.run_type,
            user_id=req.user_id,
            thesis_hint=req.thesis_hint,
        )

        # ── data_pull node ─────────────────────────────────────────────────────
        await emit({"event": "node_start", "node": "data_pull", "ts": time.time()})
        t0 = time.perf_counter()
        state = await data_pull_node(state)
        elapsed = round(time.perf_counter() - t0, 2)

        numeric = state.get("numeric_block")
        await emit({
            "event": "node_done",
            "node": "data_pull",
            "elapsed_s": elapsed,
            "ts": time.time(),
            "output": {
                "rendered_text": numeric.rendered_text if numeric else None,
                "data_gaps": list(numeric.data_gaps) if numeric else [],
                "sources_used": list(numeric.sources_used) if numeric else [],
                "fundamentals_report_date": str(numeric.fundamentals_report_date) if numeric and numeric.fundamentals_report_date else None,
                "macro_series_pulled": list(numeric.macro_series_pulled) if numeric else [],
            },
        })

        # ── rag_retrieval node ─────────────────────────────────────────────────
        await emit({"event": "node_start", "node": "rag_retrieval", "ts": time.time()})
        t0 = time.perf_counter()
        state = await rag_retrieval_node(state)
        elapsed = round(time.perf_counter() - t0, 2)

        rag = state.get("qualitative_block")
        await emit({
            "event": "node_done",
            "node": "rag_retrieval",
            "elapsed_s": elapsed,
            "ts": time.time(),
            "output": {
                "chunks_retrieved": rag.chunks_retrieved if rag else 0,
                "chunks_used": rag.chunks_used if rag else 0,
                "items": [
                    {
                        "date": item.date,
                        "source": item.source,
                        "source_type": item.source_type,
                        "sentiment_label": item.sentiment_label,
                        "sentiment_reason": item.sentiment_reason,
                        "summary": item.summary,
                        "relevance_score": round(item.relevance_score, 4),
                    }
                    for item in (rag.items if rag else [])
                ],
            },
        })

        # ── persona reasoning (4 parallel LLM calls) ──────────────────────────
        personas = ["persona_bull", "persona_bear", "persona_macro", "persona_technicals"]
        for p in personas:
            await emit({"event": "node_start", "node": p, "ts": time.time()})

        t0 = time.perf_counter()
        state = await persona_reasoning_node(state)
        elapsed = round(time.perf_counter() - t0, 2)

        po = state.get("persona_outputs")
        for p, key in zip(personas, ["bull", "bear", "macro", "technicals"]):
            text = getattr(po, key, "") if po else ""
            await emit({
                "event": "node_done",
                "node": p,
                "elapsed_s": elapsed,
                "ts": time.time(),
                "output": {"text": text},
            })

        # ── judge synthesis ─────────────────────────────────────────────────
        await emit({"event": "node_start", "node": "judge", "ts": time.time()})
        t0 = time.perf_counter()
        state = await judge_synthesis_node(state)
        elapsed = round(time.perf_counter() - t0, 2)

        l1 = state.get("layer1_output")
        await emit({
            "event": "node_done",
            "node": "judge",
            "elapsed_s": elapsed,
            "ts": time.time(),
            "output": {
                "direction": l1.direction if l1 else None,
                "magnitude_bucket": l1.magnitude_bucket if l1 else None,
                "confidence": l1.confidence if l1 else None,
                "dominant_drivers": list(l1.dominant_drivers) if l1 else [],
                "hold_window_bucket": l1.hold_window_bucket if l1 else None,
                "thesis": l1.thesis if l1 else None,
                "sided_with": list(l1.sided_with) if l1 else [],
                "invalidation_condition": l1.invalidation_condition if l1 else None,
            } if l1 else {"error": state.get("judge_reasoning", "parse failed")},
        })

        # ── guardrails (placeholder — pass-through for now) ─────────────────
        await emit({"event": "node_start", "node": "guardrails", "ts": time.time()})
        guardrail_checks = state.get("guardrail_checks", [])
        await emit({
            "event": "node_done",
            "node": "guardrails",
            "elapsed_s": 0.0,
            "ts": time.time(),
            "output": {
                "checks": [{"name": c.name, "passed": c.passed, "reason": c.reason} for c in guardrail_checks] if guardrail_checks else [],
                "retries": state.get("guardrail_retries", 0),
            },
        })

        # ── layer2 (placeholder — pass-through for now) ─────────────────────
        await emit({"event": "node_start", "node": "layer2", "ts": time.time()})
        l2 = state.get("layer2_output")
        await emit({
            "event": "node_done",
            "node": "layer2",
            "elapsed_s": 0.0,
            "ts": time.time(),
            "output": l2.model_dump(mode="json") if l2 else {"gate_passed": None, "note": "not yet implemented"},
        })

        warnings = state.get("warnings", [])
        await emit({
            "event": "pipeline_done",
            "run_id": run_id,
            "warnings": warnings,
            "ts": time.time(),
        })

    except Exception as exc:
        await emit({"event": "error", "message": str(exc), "ts": time.time()})
    finally:
        if q:
            await q.put(None)  # sentinel: close the SSE stream


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("", response_model=RunResponse, status_code=202)
async def create_run(req: RunRequest, background_tasks: BackgroundTasks):
    """Trigger a pipeline run. Returns immediately with the run_id.
    Connect to GET /runs/{run_id}/stream for live node events.
    """
    from swing_trader.state import initial_state
    from swing_trader.window import WindowError
    try:
        state = initial_state(
        ticker=req.ticker,
        window_start=req.window_start,
        window_end=req.window_end,
        run_type=req.run_type,
        user_id=req.user_id,
        thesis_hint=req.thesis_hint,
    )
    except WindowError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    run_id = state["run_id"]

    q: asyncio.Queue = asyncio.Queue()
    _run_queues[run_id] = q

    background_tasks.add_task(_run_pipeline, run_id, req)

    return RunResponse(
        run_id=run_id,
        ticker=req.ticker,
        window_start=state["window_start"],
        window_end=state["window_end"],
        run_type=req.run_type,
    )


@router.get("/{run_id}/stream")
async def stream_run(run_id: str):
    """SSE endpoint — streams node events for an in-flight pipeline run.

    Events: node_start | node_done | pipeline_done | error
    """
    async def event_generator() -> AsyncIterator[str]:
        q = _run_queues.get(run_id)
        if q is None:
            yield f"data: {json.dumps({'event': 'error', 'message': 'run not found or already completed'})}\n\n"
            return

        try:
            while True:
                event = await asyncio.wait_for(q.get(), timeout=120.0)
                if event is None:
                    yield "data: {\"event\": \"stream_closed\"}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
        except asyncio.TimeoutError:
            yield "data: {\"event\": \"timeout\"}\n\n"
        finally:
            _run_queues.pop(run_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{run_id}")
async def get_run(run_id: str):
    """Fetch a completed run from the eval store."""
    from swing_trader.db.store import get_run as _get_run
    row = await _get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return _serialize_row(row)


@router.get("")
async def list_runs(
    ticker: str | None = Query(None),
    run_type: str | None = Query(None),
    limit: int = Query(50, le=500),
):
    """List past pipeline runs from the eval store, newest first."""
    from swing_trader.db.pool import get_pool
    pool = await get_pool()

    conditions = []
    params: list = []
    if ticker:
        params.append(ticker.upper())
        conditions.append(f"ticker = ${len(params)}")
    if run_type:
        params.append(run_type)
        conditions.append(f"run_type = ${len(params)}")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT run_id, ticker, window_start, window_end, run_type, pipeline_version,
                   direction, magnitude_bucket, confidence, dominant_drivers,
                   gate_passed, entry_price, target_price, position_size_pct,
                   guardrail_retries, pipeline_cancelled, cancellation_reason,
                   warnings, hit, completed_at
            FROM pipeline_runs
            {where}
            ORDER BY completed_at DESC NULLS LAST
            LIMIT ${len(params)}
            """,
            *params,
        )

    return [_serialize_row(dict(row)) for row in rows]


def _serialize_row(row: dict) -> dict:
    """Make a DB row JSON-serializable."""
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, (list, dict)):
            out[k] = v
        else:
            out[k] = v
    return out
