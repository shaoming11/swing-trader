"""LangGraph pipeline state.

PipelineState is a TypedDict consumed by every node in the graph.
All fields are optional except the inputs (ticker, window_start, window_end).
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages

from swing_trader.schemas.pipeline import (
    GuardrailCheck,
    Layer1Output,
    NumericBlock,
    PersonaOutputs,
    PositionCard,
    QualitativeBlock,
)


class PipelineState(TypedDict, total=False):
    # ── Inputs ────────────────────────────────────────────────────────────────
    run_id: str
    ticker: str
    window_start: date
    window_end: date
    user_id: str | None
    run_type: str           # live | backfill | eval
    thesis_hint: str | None

    # ── Intermediate: data pull ───────────────────────────────────────────────
    numeric_block: NumericBlock | None

    # ── Intermediate: RAG ─────────────────────────────────────────────────────
    qualitative_block: QualitativeBlock | None

    # ── Intermediate: reasoning ───────────────────────────────────────────────
    composed_prompt: str
    persona_outputs: PersonaOutputs | None
    judge_reasoning: str

    # ── Layer 1 output ────────────────────────────────────────────────────────
    layer1_output: Layer1Output | None

    # ── Guardrail ─────────────────────────────────────────────────────────────
    guardrail_checks: list[GuardrailCheck]
    guardrail_retries: int
    guardrail_retry_notes: list[str]

    # ── Layer 2 output ────────────────────────────────────────────────────────
    layer2_output: PositionCard | None

    # ── Pipeline health ───────────────────────────────────────────────────────
    warnings: list[str]
    pipeline_cancelled: bool
    cancellation_reason: str | None

    # ── Observability ─────────────────────────────────────────────────────────
    langsmith_trace_url: str | None


def initial_state(
    ticker: str,
    window_start: date,
    window_end: date,
    user_id: str | None = None,
    run_type: str = "live",
    thesis_hint: str | None = None,
) -> PipelineState:
    return PipelineState(
        run_id=str(uuid.uuid4()),
        ticker=ticker.upper(),
        window_start=window_start,
        window_end=window_end,
        user_id=user_id,
        run_type=run_type,
        thesis_hint=thesis_hint,
        numeric_block=None,
        qualitative_block=None,
        composed_prompt="",
        persona_outputs=None,
        judge_reasoning="",
        layer1_output=None,
        guardrail_checks=[],
        guardrail_retries=0,
        guardrail_retry_notes=[],
        layer2_output=None,
        warnings=[],
        pipeline_cancelled=False,
        cancellation_reason=None,
        langsmith_trace_url=None,
    )
