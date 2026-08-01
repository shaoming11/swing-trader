"""LangGraph node: persona_reasoning.

Runs four LLM calls in parallel (bull, bear, macro, technicals), each with a
distinct system prompt that biases its analysis perspective.  All four share
the same composed prompt built from the numeric + qualitative blocks.
"""
from __future__ import annotations

import asyncio
import logging

from swing_trader.clients import get_llm_client, LLM_MID_MODEL
from swing_trader.observability.decorators import observe_node
from swing_trader.observability.langsmith_config import add_node_metadata
from swing_trader.observability.metrics import record_llm_usage
from swing_trader.reasoning.prompts import (
    PERSONA_SYSTEM_PROMPTS,
    USER_PROMPT_TEMPLATE,
    build_composed_prompt,
)
from swing_trader.schemas.pipeline import PersonaOutputs
from swing_trader.state import PipelineState

log = logging.getLogger(__name__)


@observe_node("persona_reasoning")
async def persona_reasoning_node(state: PipelineState) -> PipelineState:
    composed_prompt = build_composed_prompt(
        state.get("numeric_block"),
        state.get("qualitative_block"),
        state.get("thesis_hint"),
    )

    client = get_llm_client()
    user_message = USER_PROMPT_TEMPLATE.format(composed_prompt=composed_prompt)
    warnings = list(state.get("warnings", []))

    async def _call_persona(key: str, system_prompt: str) -> tuple[str, str]:
        response = await client.chat.completions.create(
            model=LLM_MID_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.7,
            max_tokens=300,
        )
        record_llm_usage("persona_reasoning", LLM_MID_MODEL, response)
        text = response.choices[0].message.content or ""
        return key, text.strip()

    results = await asyncio.gather(
        *[_call_persona(k, v) for k, v in PERSONA_SYSTEM_PROMPTS.items()]
    )

    outputs_dict = dict(results)
    persona_outputs = PersonaOutputs(**outputs_dict)

    for key in ("bull", "bear", "macro", "technicals"):
        if not getattr(persona_outputs, key):
            warnings.append(f"Persona '{key}' returned empty output")

    personas_filled = sum(1 for k in ("bull", "bear", "macro", "technicals")
                          if getattr(persona_outputs, k))

    add_node_metadata({
        "personas_filled": personas_filled,
        "composed_prompt_length": len(composed_prompt),
    })

    log.info("personas_filled=%d  prompt_len=%d", personas_filled, len(composed_prompt))

    return {
        **state,
        "composed_prompt": composed_prompt,
        "persona_outputs": persona_outputs,
        "warnings": warnings,
    }
