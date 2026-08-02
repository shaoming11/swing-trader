"""LangGraph node: judge_synthesis.

Synthesizes the four persona outputs into a single Layer1Output trade
recommendation using the 70B judge model.
"""

from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from swing_trader.clients import get_judge_client, LLM_JUDGE_MODEL
from swing_trader.observability.decorators import observe_node
from swing_trader.observability.langsmith_config import add_node_metadata
from swing_trader.observability.metrics import (
    CONFIDENCE_DISTRIBUTION,
    DIRECTION_TOTAL,
    record_llm_usage,
)
from swing_trader.reasoning.prompts import (
    JUDGE_SYSTEM_PROMPT,
    JUDGE_USER_PROMPT_TEMPLATE,
)
from swing_trader.schemas.pipeline import Layer1Output, PersonaOutputs
from swing_trader.state import PipelineState

log = logging.getLogger(__name__)


@observe_node("judge_synthesis")
async def judge_synthesis_node(state: PipelineState) -> PipelineState:
    composed_prompt = state.get("composed_prompt", "")
    persona_outputs: PersonaOutputs = state.get("persona_outputs") or PersonaOutputs()
    thesis_hint = state.get("thesis_hint")
    warnings = list(state.get("warnings", []))

    thesis_hint_section = ""
    if thesis_hint:
        thesis_hint_section = (
            f"=== USER THESIS HINT ===\n{thesis_hint}\n"
            "Consider this thesis hint but do not blindly follow it "
            "— evaluate it against the evidence.\n\n"
        )

    user_message = JUDGE_USER_PROMPT_TEMPLATE.format(
        composed_prompt=composed_prompt,
        bull=persona_outputs.bull or "(no output)",
        bear=persona_outputs.bear or "(no output)",
        macro=persona_outputs.macro or "(no output)",
        technicals=persona_outputs.technicals or "(no output)",
        thesis_hint_section=thesis_hint_section,
    )

    client = get_judge_client()
    response = await client.chat.completions.create(
        model=LLM_JUDGE_MODEL,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        temperature=0.3,
        max_tokens=600,
    )
    record_llm_usage("judge_synthesis", LLM_JUDGE_MODEL, response)
    raw = (response.choices[0].message.content or "").strip()

    layer1: Layer1Output | None = None
    try:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON object found in judge response")
        parsed = json.loads(raw[start:end])
        layer1 = Layer1Output(**parsed)

        CONFIDENCE_DISTRIBUTION.observe(layer1.confidence)
        DIRECTION_TOTAL.labels(direction=layer1.direction).inc()

        add_node_metadata(
            {
                "direction": layer1.direction,
                "confidence": layer1.confidence,
                "magnitude_bucket": layer1.magnitude_bucket,
                "sided_with": layer1.sided_with,
                "agreement_ratio": persona_outputs.agreement_ratio(layer1.direction),
            }
        )

        log.info(
            "direction=%s  confidence=%.2f  magnitude=%s  drivers=%s",
            layer1.direction,
            layer1.confidence,
            layer1.magnitude_bucket,
            layer1.dominant_drivers,
        )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        warnings.append(f"Judge synthesis failed: {exc}")
        log.warning("judge synthesis parse/validation error: %s", exc)
    except Exception as exc:
        warnings.append(f"Judge synthesis failed: {exc}")
        log.warning("judge synthesis unexpected error: %s", exc)

    return {
        **state,
        "judge_reasoning": raw,
        "layer1_output": layer1,
        "warnings": warnings,
    }
