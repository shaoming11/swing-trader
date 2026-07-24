# Reasoning Pipeline — Design Spec

Covers Steps 3–5 of the Layer 1 judgment pipeline: prompt composition, multi-persona reasoning, and judge synthesis. This is where hard numbers and qualitative context are combined and handed to LLMs for interpretation. No guardrails in this file — see GUARDRAIL_PIPELINE.md.

---

## 1. Pipeline Overview

```
[NumericBlock] ──┐
                 ├──→ [Prompt Composition] ──→ [Persona Calls ×4 (parallel)]
[QualBlock]   ──┘                                       │
                                              [Judge Synthesis]
                                                        │
                                              [Layer 1 Structured Output]
```

---

## 2. Prompt Composition

### 2.1 Composed Prompt Structure

One prompt is assembled from three parts in this order:

```
[SYSTEM: analyst identity]
[STRUCTURED DATA BLOCK]    ← from DATA_PULL_PIPELINE (deterministic)
[QUALITATIVE CONTEXT BLOCK] ← from RAG_PIPELINE (retrieved + tagged)
[OUTPUT INSTRUCTION]        ← fixed schema instruction
```

The output instruction is the only part that changes based on which persona is being called. The data blocks are identical across all four persona calls.

### 2.2 Token Budget

| Section | Max Tokens |
|---|---|
| Structured numeric block | 800 |
| Qualitative context block | 1,500 |
| Output instruction | 300 |
| **Total input budget** | **2,600** |

If the qualitative block exceeds 1,500 tokens after RAG retrieval, truncate by source priority: analyst > news > macro > social. Never truncate the numeric block — it is always included in full.

If the total composed prompt exceeds 2,600 tokens, log a warning and truncate the qualitative block further. Do not truncate the output instruction.

### 2.3 Composition Function

```python
def compose_prompt(
    numeric_block: NumericBlock,
    qual_block: QualitativeBlock,
    persona: Literal["bull", "bear", "macro", "technicals"]
) -> list[dict]:

    system_prompt = PERSONA_SYSTEM_PROMPTS[persona]

    user_content = f"""
{numeric_block.rendered_text}

{render_qual_block(qual_block, max_tokens=1500)}

{OUTPUT_INSTRUCTIONS[persona]}
""".strip()

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]
```

---

## 3. Persona System Prompts

Each persona receives a distinct system prompt that constrains its perspective. All four receive the same data; they differ only in what they are told to prioritize and argue.

### Bull Case

```
You are a buy-side equity analyst making the bull case for a swing trade position.

Your job is to identify the strongest reasons the stock could move UP meaningfully
in the next few days to one quarter. You must argue this case using only the data
provided — do not invent facts.

Rules:
- Focus on earnings beats, upward guidance revisions, positive macro tailwinds,
  strong technicals, and bullish sentiment signals.
- State your bull case argument in 3–5 bullet points.
- End with: your direction (bullish/neutral/bearish), a magnitude estimate (0-3%,
  3-8%, or 8%+), and the single most important driver from this list:
  fundamental | macro | sentiment | technical
- If the data does not support a bull case, say so. Do not force a bullish conclusion.
```

### Bear Case

```
You are a short-side equity analyst making the bear case for a swing trade position.

Your job is to identify the strongest reasons the stock could move DOWN meaningfully
in the next few days to one quarter. You must argue this case using only the data
provided — do not invent facts.

Rules:
- Focus on earnings misses, guidance cuts, macro headwinds, weak technicals,
  negative sentiment, and valuation risk.
- State your bear case argument in 3–5 bullet points.
- End with: your direction (bullish/neutral/bearish), a magnitude estimate (0-3%,
  3-8%, or 8%+), and the single most important driver from this list:
  fundamental | macro | sentiment | technical
- If the data does not support a bear case, say so. Do not force a bearish conclusion.
```

### Macro-Only View

```
You are a macro strategist evaluating a stock purely through the lens of
macroeconomic conditions. You do not form views on individual company fundamentals.

Your job is to assess whether the macro environment during this window is a
tailwind, headwind, or neutral for this stock's sector.

Rules:
- Use only the macro data provided (rates, CPI, GDP, yield curve, sector-specific
  indicators). Ignore company-level fundamentals and news.
- State your macro assessment in 3–5 bullet points.
- Explicitly call out any FOMC events, CPI prints, or major macro releases in the window.
- End with: your direction (bullish/neutral/bearish), a magnitude estimate (0-3%,
  3-8%, or 8%+), and "macro" as the dominant driver.
```

### Pure-Technicals View

```
You are a technical analyst evaluating a stock purely on price, volume, and
momentum signals. You do not form views on fundamentals or macro.

Your job is to assess whether the technical setup during this window is constructive
or destructive for a swing trade.

Rules:
- Use only the price and volume data provided (price at window start, 52-week
  high/low, volume profile). Ignore all qualitative context and macro data.
- Assess: is price near support or resistance? Is momentum expanding or contracting?
  Is volume confirming or diverging from price?
- State your technical assessment in 3–5 bullet points.
- End with: your direction (bullish/neutral/bearish), a magnitude estimate (0-3%,
  3-8%, or 8%+), and "technical" as the dominant driver.
- If there is insufficient technical data in the input, say so explicitly.
```

### Output Instruction (appended to all four)

```
Format your response as:

ARGUMENT:
[3–5 bullet points]

VERDICT:
Direction: bullish | bearish | neutral
Magnitude: 0-3% | 3-8% | 8%+
Dominant driver: fundamental | macro | sentiment | technical
```

---

## 4. Parallel Persona Execution

All four persona calls fire concurrently against the same composed data blocks.

```python
import asyncio
from anthropic import AsyncAnthropic

client = AsyncAnthropic()
PERSONA_MODEL = "claude-sonnet-4-6"

async def run_persona(
    persona: str,
    messages: list[dict]
) -> tuple[str, str]:
    response = await client.messages.create(
        model=PERSONA_MODEL,
        max_tokens=600,
        messages=messages
    )
    return persona, response.content[0].text

async def run_all_personas(
    numeric_block: NumericBlock,
    qual_block: QualitativeBlock
) -> dict[str, str]:
    tasks = [
        run_persona(p, compose_prompt(numeric_block, qual_block, p))
        for p in ["bull", "bear", "macro", "technicals"]
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    outputs = {}
    for result in results:
        if isinstance(result, Exception):
            # Log but don't fail — a missing persona is a degraded run, not a cancelled one
            persona = result  # recover persona name from task if possible
            outputs[persona] = f"ERROR: {result}"
        else:
            persona, text = result
            outputs[persona] = text

    return outputs
```

If a persona call fails, its output is recorded as an error string. The judge receives all four slots — it is told to weight available arguments and note any missing persona.

---

## 5. Judge Synthesis

### 5.1 Judge Role

The judge receives all four persona outputs and is the only call that produces the Layer 1 output. Persona outputs are inputs to the judge, not independent outputs.

### 5.2 Judge System Prompt

```
You are a senior portfolio manager synthesizing four analyst perspectives into
one trade verdict.

You will receive four arguments: bull case, bear case, macro view, and technicals view.
Your job is to:
1. Weigh all arguments against the underlying data
2. Side with the argument(s) best supported by the evidence
3. Name which argument(s) you are siding with and exactly why
4. Produce one structured verdict using the tool provided

Rules:
- Your confidence must reflect how much the arguments agree. If bull and bear are
  evenly matched, confidence cannot exceed 0.65. If three of four agree, confidence
  can be 0.80+. If all four agree, confidence can reach 0.90.
- The invalidation_condition must be a specific, checkable event or price level.
  "If fundamentals deteriorate" is not acceptable. "If next CPI print exceeds 3.5%"
  or "if price breaks below $142" are acceptable.
- dominant_drivers must only list drivers that appear in the provided input data.
  Do not cite information not present in the structured data or qualitative context.
- The thesis should be 2–4 sentences suitable for a trade journal entry.
```

### 5.3 Judge Input Format

```python
def compose_judge_prompt(
    numeric_block: NumericBlock,
    qual_block: QualitativeBlock,
    persona_outputs: dict[str, str],
    retry_note: str | None = None
) -> list[dict]:

    personas_text = "\n\n".join([
        f"=== {name.upper()} CASE ===\n{text}"
        for name, text in persona_outputs.items()
    ])

    user_content = f"""
{numeric_block.rendered_text}

{render_qual_block(qual_block, max_tokens=800)}

=== ANALYST ARGUMENTS ===
{personas_text}

{"=== RETRY NOTE ===\n" + retry_note if retry_note else ""}

Produce your verdict using the verdict tool.
""".strip()

    return [
        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_content}
    ]
```

### 5.4 Judge Tool Schema (Function Calling)

```python
VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Submit the final trade verdict",
    "input_schema": {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["bullish", "bearish", "neutral"]
            },
            "magnitude_bucket": {
                "type": "string",
                "enum": ["0-3%", "3-8%", "8%+"]
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Calibrated confidence (0.0–1.0). Must reflect persona agreement level."
            },
            "dominant_drivers": {
                "type": "array",
                "items": {"type": "string", "enum": ["fundamental", "macro", "sentiment", "technical"]},
                "minItems": 1,
                "maxItems": 4
            },
            "invalidation_condition": {
                "type": "string",
                "description": "Specific, checkable event or price level that would invalidate this thesis."
            },
            "hold_window_bucket": {
                "type": "string",
                "enum": ["days", "weeks", "quarter"]
            },
            "thesis": {
                "type": "string",
                "description": "2–4 sentence natural language thesis for the trade journal."
            },
            "sided_with": {
                "type": "array",
                "items": {"type": "string", "enum": ["bull", "bear", "macro", "technicals"]},
                "description": "Which persona arguments the verdict aligns with."
            },
            "sided_reasoning": {
                "type": "string",
                "description": "One sentence explaining why those arguments were weighted more heavily."
            }
        },
        "required": [
            "direction", "magnitude_bucket", "confidence", "dominant_drivers",
            "invalidation_condition", "hold_window_bucket", "thesis",
            "sided_with", "sided_reasoning"
        ]
    }
}
```

### 5.5 Judge Call

```python
JUDGE_MODEL = "claude-opus-4-6"

async def run_judge(
    messages: list[dict],
) -> Layer1Output:
    response = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1000,
        tools=[VERDICT_TOOL],
        tool_choice={"type": "tool", "name": "submit_verdict"},
        messages=messages
    )

    tool_use = next(
        block for block in response.content
        if block.type == "tool_use" and block.name == "submit_verdict"
    )
    return Layer1Output(**tool_use.input)
```

`tool_choice={"type": "tool", "name": "submit_verdict"}` forces the model to call the tool — it cannot respond in plain text. If the response contains no tool use block, treat it as a malformed output and trigger the retry flow (see GUARDRAIL_PIPELINE.md).

---

## 6. Structured Output Schema

```python
from pydantic import BaseModel, Field, field_validator
from typing import Literal

class Layer1Output(BaseModel):
    direction: Literal["bullish", "bearish", "neutral"]
    magnitude_bucket: Literal["0-3%", "3-8%", "8%+"]
    confidence: float = Field(ge=0.0, le=1.0)
    dominant_drivers: list[Literal["fundamental", "macro", "sentiment", "technical"]]
    invalidation_condition: str
    hold_window_bucket: Literal["days", "weeks", "quarter"]
    thesis: str
    sided_with: list[Literal["bull", "bear", "macro", "technicals"]]
    sided_reasoning: str

    @field_validator("dominant_drivers")
    @classmethod
    def at_least_one_driver(cls, v):
        if not v:
            raise ValueError("dominant_drivers must have at least one entry")
        return v

    @field_validator("invalidation_condition")
    @classmethod
    def not_vague(cls, v):
        vague_phrases = ["if fundamentals deteriorate", "if conditions change",
                         "if sentiment shifts", "if macro worsens"]
        for phrase in vague_phrases:
            if phrase.lower() in v.lower():
                raise ValueError(f"invalidation_condition is too vague: contains '{phrase}'")
        return v
```

Pydantic validation runs immediately on the tool call input. Validation failure → retry (handled in GUARDRAIL_PIPELINE.md).

---

## 7. LangGraph Integration

```python
# Node definitions
async def prompt_composition_node(state: PipelineState) -> PipelineState:
    state.composed_prompts = {
        persona: compose_prompt(state.numeric_block, state.qualitative_block, persona)
        for persona in ["bull", "bear", "macro", "technicals"]
    }
    log_node_output("prompt_composition", {
        "total_tokens_estimate": estimate_tokens(state.composed_prompts["bull"])
    })
    return state

async def persona_reasoning_node(state: PipelineState) -> PipelineState:
    state.persona_outputs = await run_all_personas(
        state.numeric_block, state.qualitative_block
    )
    log_node_output("persona_reasoning", {
        "personas_succeeded": [k for k, v in state.persona_outputs.items() if not v.startswith("ERROR")]
    })
    return state

async def judge_synthesis_node(state: PipelineState) -> PipelineState:
    messages = compose_judge_prompt(
        state.numeric_block,
        state.qualitative_block,
        state.persona_outputs,
        retry_note=state.guardrail_retry_note  # set by guardrail node on retry
    )
    try:
        state.layer1_output = await run_judge(messages)
    except Exception as e:
        state.judge_error = str(e)
        state.layer1_output = None
    log_node_output("judge_synthesis", {
        "success": state.layer1_output is not None,
        "error": state.judge_error
    })
    return state

# Graph edges
graph.add_node("prompt_composition", prompt_composition_node)
graph.add_node("persona_reasoning", persona_reasoning_node)
graph.add_node("judge_synthesis", judge_synthesis_node)

graph.add_edge("prompt_composition", "persona_reasoning")
graph.add_edge("persona_reasoning", "judge_synthesis")
graph.add_edge("judge_synthesis", "guardrail_pass")   # → see GUARDRAIL_PIPELINE.md
```

---

## 8. Implementation Notes

- **Async:** All LLM calls use `AsyncAnthropic`. Persona calls run via `asyncio.gather` — 4 concurrent requests, each capped at 600 output tokens
- **Models:** Persona calls → `claude-sonnet-4-6`. Judge → `claude-opus-4-6`
- **tool_choice forced:** Without `tool_choice={"type":"tool","name":"submit_verdict"}`, the judge may respond in prose. Always force it
- **sided_with / sided_reasoning:** Stored in the eval store for feature attribution — which persona the judge most often sides with per sector/regime is a useful signal
- **Token estimation:** Use `tiktoken` with `cl100k_base` for rough token counting before API calls. Not exact for Claude but within 10%
- **Retry context:** On guardrail-triggered retry, the `retry_note` is appended to the judge prompt with the specific inconsistency. This means the judge sees its prior mistake on the retry call
