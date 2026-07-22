"""Eval store CRUD — write pipeline run records and read them back.

All writes are fire-and-forget (non-blocking to the pipeline hot path).
Use asyncio.create_task() to dispatch writes in the background.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from swing_trader.db.pool import get_pool
from swing_trader.state import PipelineState


async def write_eval_record(state: PipelineState, trace_url: str | None = None) -> None:
    """Write (or update) the pipeline run record in the eval store.

    Safe to call multiple times for the same run_id — uses ON CONFLICT upsert.
    Non-blocking: caller should fire-and-forget via asyncio.create_task().
    """
    layer1 = state.get("layer1_output")
    layer2 = state.get("layer2_output")
    numeric = state.get("numeric_block")
    rag = state.get("qualitative_block")
    personas = state.get("persona_outputs")

    record = {
        "run_id": state.get("run_id"),
        "ticker": state.get("ticker"),
        "window_start": state.get("window_start"),
        "window_end": state.get("window_end"),
        "user_id": state.get("user_id"),
        "run_type": state.get("run_type", "live"),
        "pipeline_version": __import__("os").getenv("PIPELINE_VERSION", "0.0.0"),

        # Layer 1
        "direction": layer1.direction if layer1 else None,
        "magnitude_bucket": layer1.magnitude_bucket if layer1 else None,
        "confidence": layer1.confidence if layer1 else None,
        "dominant_drivers": list(layer1.dominant_drivers) if layer1 else None,
        "invalidation_condition": layer1.invalidation_condition if layer1 else None,
        "hold_window_bucket": layer1.hold_window_bucket if layer1 else None,
        "thesis": layer1.thesis if layer1 else None,
        "sided_with": list(layer1.sided_with) if layer1 else None,
        "sided_reasoning": layer1.sided_reasoning if layer1 else None,

        # Layer 2
        "gate_passed": layer2.gate_passed if layer2 else None,
        "gate_skip_reason": layer2.gate_skip_reason if layer2 else None,
        "entry_price": layer2.entry_price if layer2 else None,
        "target_price": layer2.target_price if layer2 else None,
        "stop_loss": layer2.stop_loss if layer2 else None,
        "hold_window_start": layer2.hold_window_start if layer2 else None,
        "hold_window_end": layer2.hold_window_end if layer2 else None,
        "position_size_pct": layer2.position_size_pct if layer2 else None,
        "kelly_full": layer2.kelly_full if layer2 else None,
        "kelly_fractional": layer2.kelly_fractional if layer2 else None,
        "volatility_used": layer2.volatility_used if layer2 else None,

        # Debug: inputs
        "numeric_block_text": numeric.rendered_text if numeric else None,
        "data_gaps": list(numeric.data_gaps) if numeric else None,
        "sources_used": list(numeric.sources_used) if numeric else None,
        "rag_chunks_retrieved": rag.chunks_retrieved if rag else 0,
        "rag_chunks_used": rag.chunks_used if rag else 0,
        "rag_top_chunks": json.dumps([
            {
                "content": item.summary,
                "score": item.relevance_score,
                "source_type": item.source_type,
                "date": item.date,
            }
            for item in (rag.items if rag else [])
        ]),

        # Debug: reasoning
        "persona_bull_output": personas.bull if personas else None,
        "persona_bear_output": personas.bear if personas else None,
        "persona_macro_output": personas.macro if personas else None,
        "persona_technicals_output": personas.technicals if personas else None,
        "judge_reasoning": state.get("judge_reasoning"),

        # Debug: guardrails
        "guardrail_checks": json.dumps([
            gc.model_dump() for gc in (state.get("guardrail_checks") or [])
        ]),
        "guardrail_retries": state.get("guardrail_retries", 0),
        "pipeline_cancelled": state.get("pipeline_cancelled", False),
        "cancellation_reason": state.get("cancellation_reason"),
        "warnings": list(state.get("warnings", [])),

        "langsmith_trace_url": trace_url,
        "completed_at": datetime.utcnow(),
    }

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO pipeline_runs (
                    run_id, ticker, window_start, window_end, user_id, run_type, pipeline_version,
                    direction, magnitude_bucket, confidence, dominant_drivers,
                    invalidation_condition, hold_window_bucket, thesis,
                    sided_with, sided_reasoning,
                    gate_passed, gate_skip_reason, entry_price, target_price,
                    stop_loss, hold_window_start, hold_window_end, position_size_pct,
                    kelly_full, kelly_fractional, volatility_used,
                    numeric_block_text, data_gaps, sources_used,
                    rag_chunks_retrieved, rag_chunks_used, rag_top_chunks,
                    persona_bull_output, persona_bear_output,
                    persona_macro_output, persona_technicals_output, judge_reasoning,
                    guardrail_checks, guardrail_retries,
                    pipeline_cancelled, cancellation_reason, warnings,
                    langsmith_trace_url, completed_at
                )
                VALUES (
                    $1,$2,$3,$4,$5,$6,$7,
                    $8,$9,$10,$11,$12,$13,$14,
                    $15,$16,
                    $17,$18,$19,$20,$21,$22,$23,$24,
                    $25,$26,$27,
                    $28,$29,$30,$31,$32,$33,
                    $34,$35,$36,$37,$38,
                    $39::jsonb,$40,$41,$42,$43,
                    $44,$45
                )
                ON CONFLICT (run_id) DO UPDATE SET
                    direction = EXCLUDED.direction,
                    magnitude_bucket = EXCLUDED.magnitude_bucket,
                    confidence = EXCLUDED.confidence,
                    dominant_drivers = EXCLUDED.dominant_drivers,
                    invalidation_condition = EXCLUDED.invalidation_condition,
                    hold_window_bucket = EXCLUDED.hold_window_bucket,
                    thesis = EXCLUDED.thesis,
                    sided_with = EXCLUDED.sided_with,
                    sided_reasoning = EXCLUDED.sided_reasoning,
                    gate_passed = EXCLUDED.gate_passed,
                    gate_skip_reason = EXCLUDED.gate_skip_reason,
                    entry_price = EXCLUDED.entry_price,
                    target_price = EXCLUDED.target_price,
                    stop_loss = EXCLUDED.stop_loss,
                    hold_window_start = EXCLUDED.hold_window_start,
                    hold_window_end = EXCLUDED.hold_window_end,
                    position_size_pct = EXCLUDED.position_size_pct,
                    kelly_full = EXCLUDED.kelly_full,
                    kelly_fractional = EXCLUDED.kelly_fractional,
                    volatility_used = EXCLUDED.volatility_used,
                    numeric_block_text = EXCLUDED.numeric_block_text,
                    data_gaps = EXCLUDED.data_gaps,
                    sources_used = EXCLUDED.sources_used,
                    rag_chunks_retrieved = EXCLUDED.rag_chunks_retrieved,
                    rag_chunks_used = EXCLUDED.rag_chunks_used,
                    rag_top_chunks = EXCLUDED.rag_top_chunks,
                    persona_bull_output = EXCLUDED.persona_bull_output,
                    persona_bear_output = EXCLUDED.persona_bear_output,
                    persona_macro_output = EXCLUDED.persona_macro_output,
                    persona_technicals_output = EXCLUDED.persona_technicals_output,
                    judge_reasoning = EXCLUDED.judge_reasoning,
                    guardrail_checks = EXCLUDED.guardrail_checks,
                    guardrail_retries = EXCLUDED.guardrail_retries,
                    pipeline_cancelled = EXCLUDED.pipeline_cancelled,
                    cancellation_reason = EXCLUDED.cancellation_reason,
                    warnings = EXCLUDED.warnings,
                    langsmith_trace_url = EXCLUDED.langsmith_trace_url,
                    completed_at = EXCLUDED.completed_at
                """,
                *list(record.values()),
            )
    except Exception as e:
        # Eval store writes must never crash the pipeline
        print(f"[db.store] write_eval_record failed: {e}")


async def get_run(run_id: str) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pipeline_runs WHERE run_id = $1", run_id
        )
    return dict(row) if row else None
