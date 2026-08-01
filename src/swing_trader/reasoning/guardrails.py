"""Guardrail node — validates judge output before it reaches Layer 2.

Checks:
  1. confidence_floor     — confidence >= threshold (default 0.40)
  2. invalidation_quality — invalidation condition must be concrete
  3. data_gap_threshold   — too many data gaps degrade reliability
  4. persona_agreement    — extreme disagreement at high confidence is suspect
  5. neutral_high_conf    — neutral direction shouldn't have very high confidence

On failure the guardrail can retry the judge up to MAX_RETRIES times,
injecting the failure reasons into the retry prompt. If all retries exhaust,
the pipeline is cancelled.
"""
from __future__ import annotations

import logging
import os
import re

from swing_trader.observability.decorators import observe_node
from swing_trader.observability.metrics import (
    GUARDRAIL_CHECKS_TOTAL,
    GUARDRAIL_RETRIES_TOTAL,
    PIPELINE_CANCELLATIONS_TOTAL,
)
from swing_trader.schemas.pipeline import GuardrailCheck, Layer1Output, PersonaOutputs
from swing_trader.state import PipelineState

log = logging.getLogger(__name__)

MAX_RETRIES = int(os.getenv("GUARDRAIL_MAX_RETRIES", "2"))
CONFIDENCE_FLOOR = float(os.getenv("GUARDRAIL_CONFIDENCE_FLOOR", "0.40"))
DATA_GAP_THRESHOLD = int(os.getenv("GUARDRAIL_DATA_GAP_THRESHOLD", "4"))
AGREEMENT_FLOOR = float(os.getenv("GUARDRAIL_AGREEMENT_FLOOR", "0.25"))

_CONCRETE_PATTERN = re.compile(
    r"(\$[\d,.]+|[\d,.]+%|[\d,.]+\s*(billion|million|B|M)|"
    r"CPI|GDP|FOMC|earnings|Fed|rate cut|rate hike|[A-Z]{2,5}\s+report|"
    r"support|resistance|moving average|MA\s?\d|EMA|SMA|RSI|MACD)",
    re.IGNORECASE,
)


def _check_confidence_floor(layer1: Layer1Output) -> GuardrailCheck:
    passed = layer1.confidence >= CONFIDENCE_FLOOR
    reason = "" if passed else (
        f"Confidence {layer1.confidence:.2f} is below floor {CONFIDENCE_FLOOR:.2f}. "
        "Either raise confidence with stronger evidence or switch direction to neutral."
    )
    return GuardrailCheck(name="confidence_floor", passed=passed, reason=reason)


def _check_invalidation_quality(layer1: Layer1Output) -> GuardrailCheck:
    text = layer1.invalidation_condition
    has_concrete = bool(_CONCRETE_PATTERN.search(text))
    passed = has_concrete and len(text) >= 20
    reason = "" if passed else (
        f"Invalidation condition is too vague: '{text}'. "
        "Must reference a specific price level, percentage, technical level, or named event."
    )
    return GuardrailCheck(name="invalidation_quality", passed=passed, reason=reason)


def _check_data_gaps(state: PipelineState) -> GuardrailCheck:
    numeric = state.get("numeric_block")
    gaps = list(numeric.data_gaps) if numeric else []
    passed = len(gaps) <= DATA_GAP_THRESHOLD
    reason = "" if passed else (
        f"Too many data gaps ({len(gaps)}): {', '.join(gaps[:3])}... "
        "Lower confidence or flag uncertainty in the thesis."
    )
    return GuardrailCheck(name="data_gap_threshold", passed=passed, reason=reason)


def _check_persona_agreement(layer1: Layer1Output, persona_outputs: PersonaOutputs) -> GuardrailCheck:
    if layer1.direction == "neutral":
        return GuardrailCheck(name="persona_agreement", passed=True)

    ratio = persona_outputs.agreement_ratio(layer1.direction)
    high_conf = layer1.confidence >= 0.70

    if high_conf and ratio < AGREEMENT_FLOOR:
        return GuardrailCheck(
            name="persona_agreement",
            passed=False,
            reason=(
                f"High confidence ({layer1.confidence:.2f}) but only {ratio:.0%} persona agreement "
                f"with {layer1.direction} direction. Either lower confidence or re-evaluate direction."
            ),
        )
    return GuardrailCheck(name="persona_agreement", passed=True)


def _check_neutral_high_confidence(layer1: Layer1Output) -> GuardrailCheck:
    if layer1.direction == "neutral" and layer1.confidence > 0.80:
        return GuardrailCheck(
            name="neutral_high_confidence",
            passed=False,
            reason=(
                f"Neutral direction with {layer1.confidence:.2f} confidence is contradictory. "
                "High confidence should imply a directional conviction."
            ),
        )
    return GuardrailCheck(name="neutral_high_confidence", passed=True)


def run_guardrail_checks(state: PipelineState) -> list[GuardrailCheck]:
    """Run all guardrail checks and return results."""
    layer1 = state.get("layer1_output")
    if layer1 is None:
        return [GuardrailCheck(name="output_exists", passed=False, reason="No Layer 1 output to validate")]

    persona_outputs = state.get("persona_outputs") or PersonaOutputs()

    checks = [
        _check_confidence_floor(layer1),
        _check_invalidation_quality(layer1),
        _check_data_gaps(state),
        _check_persona_agreement(layer1, persona_outputs),
        _check_neutral_high_confidence(layer1),
    ]

    for c in checks:
        result = "pass" if c.passed else "fail"
        GUARDRAIL_CHECKS_TOTAL.labels(check_name=c.name, result=result).inc()

    return checks


@observe_node("guardrails")
async def guardrail_node(state: PipelineState) -> PipelineState:
    """Run guardrail checks. On failure, retry the judge up to MAX_RETRIES times."""
    from swing_trader.reasoning.judge import judge_synthesis_node

    warnings = list(state.get("warnings", []))
    retry_notes = list(state.get("guardrail_retry_notes", []))
    retries = state.get("guardrail_retries", 0)

    checks = run_guardrail_checks(state)
    failures = [c for c in checks if not c.passed]

    if not failures:
        log.info("All guardrail checks passed")
        return {**state, "guardrail_checks": checks}

    failure_reasons = "; ".join(f"{c.name}: {c.reason}" for c in failures)
    log.warning("Guardrail failures: %s", failure_reasons)

    # Retry loop
    while failures and retries < MAX_RETRIES:
        retries += 1
        GUARDRAIL_RETRIES_TOTAL.labels(reason=failures[0].name).inc()

        retry_note = (
            f"[Guardrail retry {retries}/{MAX_RETRIES}] Fix these issues: {failure_reasons}"
        )
        retry_notes.append(retry_note)
        warnings.append(f"Guardrail retry {retries}: {failure_reasons}")

        # Inject retry feedback into state for the judge
        retry_state = {
            **state,
            "warnings": warnings,
            "guardrail_retry_notes": retry_notes,
            "guardrail_retries": retries,
        }

        # Re-run judge synthesis
        retry_state = await judge_synthesis_node(retry_state)

        # Re-check
        checks = run_guardrail_checks(retry_state)
        failures = [c for c in checks if not c.passed]
        state = retry_state

        if not failures:
            log.info("Guardrail checks passed after %d retries", retries)

    if failures:
        # Exhausted retries — cancel pipeline
        cancel_reason = f"Guardrail checks failed after {retries} retries: {failure_reasons}"
        PIPELINE_CANCELLATIONS_TOTAL.labels(reason="guardrail_exhaustion").inc()
        log.warning("Pipeline cancelled: %s", cancel_reason)
        warnings.append(cancel_reason)

        return {
            **state,
            "guardrail_checks": checks,
            "guardrail_retries": retries,
            "guardrail_retry_notes": retry_notes,
            "warnings": warnings,
            "pipeline_cancelled": True,
            "cancellation_reason": cancel_reason,
        }

    return {
        **state,
        "guardrail_checks": checks,
        "guardrail_retries": retries,
        "guardrail_retry_notes": retry_notes,
        "warnings": warnings,
    }
