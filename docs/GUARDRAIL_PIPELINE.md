# Guardrail Pipeline — Design Spec

Covers all guardrail stages across the Layer 1 and Layer 2 pipelines. Guardrails are deterministic checks — no LLM is involved except where explicitly noted. A failed guardrail either triggers a retry, degrades to a fallback, or cancels the pipeline.

---

## 1. Where Guardrails Sit

```
[User Input]
     │
  [INPUT GUARDRAILS]           ← before anything runs
     │
  [data_pull] → [rag_retrieval]
                     │
              [RUNTIME GUARDRAILS]   ← before chunks enter context
                     │
              [prompt_composition] → [persona_reasoning] → [judge_synthesis]
                                                                  │
                                                        [OUTPUT GUARDRAILS]   ← before Layer 1 is accepted
                                                                  │
                                                          (retry judge if fail, max 2)
                                                                  │
                                                        [layer2_gate] → [layer2_sizing]
                                                                              │
                                                                    [PIPELINE CANCELLATION]
                                                                    (if guardrails exhausted)
```

---

## 2. Input Guardrails

Applied deterministically before any data pull or LLM call. Fast and cheap — these run in microseconds.

### 2.1 Ticker Validation

```python
import re

TICKER_PATTERN = re.compile(r'^[A-Z]{1,5}$')

def validate_ticker(ticker: str) -> GuardrailResult:
    ticker = ticker.strip().upper()
    if not TICKER_PATTERN.match(ticker):
        return GuardrailResult.fail("ticker_format", f"Invalid ticker format: {ticker!r}")
    # Existence check via FMP profile endpoint (cached)
    if not ticker_exists_in_market(ticker):
        return GuardrailResult.fail("ticker_existence", f"Ticker not found: {ticker}")
    return GuardrailResult.ok()
```

### 2.2 Date Range Validation

```python
from datetime import date, timedelta

MAX_WINDOW_DAYS = 120   # one quarter + buffer
MIN_WINDOW_DAYS = 5

def validate_date_range(window_start: date, window_end: date) -> GuardrailResult:
    today = date.today()

    if window_end > today:
        return GuardrailResult.fail("future_date", "window_end cannot be in the future")

    if window_start >= window_end:
        return GuardrailResult.fail("inverted_range", "window_start must be before window_end")

    days = (window_end - window_start).days
    if days < MIN_WINDOW_DAYS:
        return GuardrailResult.fail("window_too_short", f"Window is {days} days (min {MIN_WINDOW_DAYS})")
    if days > MAX_WINDOW_DAYS:
        return GuardrailResult.fail("window_too_long", f"Window is {days} days (max {MAX_WINDOW_DAYS})")

    return GuardrailResult.ok()
```

### 2.3 Prompt Injection Detection

Applied to any user-supplied string field (thesis_hint, custom notes). Only these fields are user-controlled — the ticker and dates are validated separately.

```python
INJECTION_PATTERNS = [
    r"ignore (all |previous |prior )?instructions",
    r"you are now",
    r"disregard (the |your )?",
    r"system prompt",
    r"<\|.*?\|>",           # token delimiters
    r"\[INST\]",            # Llama-style injection
    r"jailbreak",
    r"act as (a |an )?",
]

def detect_injection(text: str) -> GuardrailResult:
    text_lower = text.lower()
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, text_lower):
            return GuardrailResult.fail("prompt_injection", f"Injection pattern detected: {pattern!r}")
    return GuardrailResult.ok()
```

### 2.4 Rate Limiting

Per-user, per-hour limit enforced at input stage. Stored in Redis (or Postgres if Redis not available).

```python
MAX_RUNS_PER_USER_PER_HOUR = 10

def check_rate_limit(user_id: str) -> GuardrailResult:
    count = get_run_count_last_hour(user_id)
    if count >= MAX_RUNS_PER_USER_PER_HOUR:
        return GuardrailResult.fail("rate_limit", f"Rate limit exceeded: {count} runs in the last hour")
    return GuardrailResult.ok()
```

### 2.5 Input Guardrail Node

```python
def input_guardrail_node(state: PipelineState) -> PipelineState:
    checks = [
        ("ticker_validation", validate_ticker(state.ticker)),
        ("date_range", validate_date_range(state.window_start, state.window_end)),
        ("rate_limit", check_rate_limit(state.user_id)),
    ]
    if state.thesis_hint:
        checks.append(("injection", detect_injection(state.thesis_hint)))

    for name, result in checks:
        state.guardrail_checks.append({"name": name, "passed": result.passed, "reason": result.reason})
        if not result.passed:
            state.pipeline_cancelled = True
            state.cancellation_reason = f"Input guardrail failed: {name} — {result.reason}"
            return state

    return state
```

Conditional edge: if `state.pipeline_cancelled` → end. Otherwise → `data_pull`.

---

## 3. Runtime Guardrails

Applied after RAG retrieval, before chunks enter the composed prompt.

### 3.1 Chunk Scope Validation

Verify that each chunk returned by the retriever actually passes the ticker and date filters. Defends against vector store bugs or filter bypass.

```python
def validate_chunks(
    chunks: list[QualItem],
    ticker: str,
    window_start: date,
    window_end: date
) -> tuple[list[QualItem], list[str]]:
    valid = []
    violations = []

    for chunk in chunks:
        chunk_date = date.fromisoformat(chunk.date)
        if ticker not in chunk.tickers:
            violations.append(f"Chunk {chunk.id}: ticker {ticker!r} not in {chunk.tickers}")
            continue
        if not (window_start <= chunk_date <= window_end):
            violations.append(f"Chunk {chunk.id}: date {chunk.date} outside window")
            continue
        valid.append(chunk)

    return valid, violations
```

Violations are logged as warnings. The pipeline continues with the clean subset — it does not cancel on scope violations unless all chunks are rejected.

### 3.2 Context Size Enforcement

After composition, verify the total prompt is within the token budget before firing any LLM call.

```python
MAX_COMPOSED_PROMPT_TOKENS = 3000

def enforce_context_size(messages: list[dict]) -> GuardrailResult:
    total = sum(estimate_tokens(m["content"]) for m in messages)
    if total > MAX_COMPOSED_PROMPT_TOKENS:
        return GuardrailResult.fail(
            "context_too_large",
            f"Composed prompt is {total} tokens (max {MAX_COMPOSED_PROMPT_TOKENS})"
        )
    return GuardrailResult.ok()
```

On failure: truncate qualitative block further and retry composition. If still over budget after truncation, log warning and proceed (model will handle gracefully with its own context window).

---

## 4. Output Guardrails

Applied after judge synthesis, before Layer 1 output is accepted. This is the most important guardrail stage.

### 4.1 Schema Validation

Pydantic validation runs on the raw tool call input from the judge. Any field type error, enum violation, or missing field is caught here.

```python
def validate_schema(raw_output: dict) -> GuardrailResult:
    try:
        Layer1Output(**raw_output)
        return GuardrailResult.ok()
    except ValidationError as e:
        return GuardrailResult.fail("schema_validation", str(e))
```

### 4.2 Citation Grounding Check

`dominant_drivers` must only contain drivers that actually appear in the input data provided to the judge. Prevents hallucinated citations.

```python
def check_citation_grounding(
    output: Layer1Output,
    numeric_block: NumericBlock,
    qual_block: QualitativeBlock
) -> GuardrailResult:
    available_drivers = set()

    # Fundamental data present?
    if numeric_block.fundamentals_report_date:
        available_drivers.add("fundamental")

    # Macro data present?
    if numeric_block.macro_series_pulled:
        available_drivers.add("macro")

    # Qualitative chunks present?
    if qual_block and qual_block.chunks_used > 0:
        source_types = {item.source_type for item in qual_block.items}
        if "news" in source_types or "analyst" in source_types:
            available_drivers.add("sentiment")
        if "social" in source_types:
            available_drivers.add("sentiment")

    # Price data present?
    if "Polygon" in numeric_block.sources_used or "price" in numeric_block.rendered_text.lower():
        available_drivers.add("technical")

    hallucinated = set(output.dominant_drivers) - available_drivers
    if hallucinated:
        return GuardrailResult.fail(
            "citation_grounding",
            f"dominant_drivers contains drivers not in input: {hallucinated}. "
            f"Available: {available_drivers}"
        )
    return GuardrailResult.ok()
```

### 4.3 Confidence Consistency Check

Confidence must be consistent with the degree of persona agreement. A 50/50 bull/bear split with the judge siding weakly cannot produce 0.90 confidence.

```python
def check_confidence_consistency(
    output: Layer1Output,
    persona_outputs: dict[str, str]
) -> GuardrailResult:
    # Parse direction from each persona output
    directions = []
    for persona, text in persona_outputs.items():
        if "ERROR" in text:
            continue
        if "bullish" in text.lower():
            directions.append("bullish")
        elif "bearish" in text.lower():
            directions.append("bearish")
        else:
            directions.append("neutral")

    if not directions:
        return GuardrailResult.ok()  # can't check without persona outputs

    # Agreement ratio: fraction of personas matching the judge's direction
    agreement = sum(1 for d in directions if d == output.direction) / len(directions)

    # Maximum allowed confidence by agreement level
    if agreement < 0.26:     # 0/4 or 1/4 agree
        max_confidence = 0.50
    elif agreement < 0.51:   # 2/4 agree
        max_confidence = 0.65
    elif agreement < 0.76:   # 3/4 agree
        max_confidence = 0.80
    else:                    # 4/4 agree
        max_confidence = 0.95

    if output.confidence > max_confidence:
        return GuardrailResult.fail(
            "confidence_consistency",
            f"Confidence {output.confidence:.2f} too high for persona agreement {agreement:.0%}. "
            f"Max allowed: {max_confidence:.2f}. "
            f"Persona directions: {directions}, judge direction: {output.direction}"
        )
    return GuardrailResult.ok()
```

### 4.4 Invalidation Condition Concreteness Check

The invalidation condition must be specific and checkable, not a vague statement.

```python
VAGUE_PATTERNS = [
    r"if (the |)fundamentals? (deteriorate|worsen|change)",
    r"if (the |)market (turns|shifts|worsens)",
    r"if (conditions|sentiment|outlook) (change|shift|deteriorate)",
    r"if (things|situation) (change|worsen)",
    r"in case of (uncertainty|volatility)",
    r"if (macro|economic) (environment|conditions) (change|worsen)",
]

CONCRETE_INDICATORS = [
    r"\$\d+",               # price level: $142
    r"\d+(\.\d+)?%",        # percentage: 3.5%
    r"(CPI|GDP|PCE|FOMC|earnings|revenue|EPS)",   # named event
    r"(above|below|exceeds?|drops? below|falls? below|breaks? (above|below))",
]

def check_invalidation_concreteness(output: Layer1Output) -> GuardrailResult:
    text = output.invalidation_condition.lower()

    for pattern in VAGUE_PATTERNS:
        if re.search(pattern, text):
            return GuardrailResult.fail(
                "invalidation_vague",
                f"invalidation_condition is too vague: {output.invalidation_condition!r}. "
                "Must reference a specific price level, percentage, or named event."
            )

    has_concrete = any(re.search(p, output.invalidation_condition) for p in CONCRETE_INDICATORS)
    if not has_concrete:
        return GuardrailResult.fail(
            "invalidation_not_concrete",
            f"invalidation_condition lacks a concrete, checkable reference: {output.invalidation_condition!r}"
        )

    return GuardrailResult.ok()
```

**Examples:**

| Condition | Result |
|---|---|
| "If fundamentals deteriorate" | FAIL — vague |
| "If sentiment shifts negative" | FAIL — vague |
| "If price breaks below $142" | PASS |
| "If next CPI print exceeds 3.5%" | PASS |
| "If Q3 earnings miss estimates by more than 5%" | PASS |
| "If FOMC raises rates unexpectedly" | PASS |

### 4.5 Output Guardrail Node

```python
def output_guardrail_node(state: PipelineState) -> PipelineState:
    if state.layer1_output is None:
        state.guardrail_checks.append({
            "name": "judge_output_present", "passed": False,
            "reason": f"Judge returned no output: {state.judge_error}"
        })
        state.guardrail_retries += 1
        state.guardrail_retry_note = f"Your previous response produced no tool call. Error: {state.judge_error}. Please call submit_verdict."
        return state

    checks = [
        ("schema_validation",      validate_schema(state.layer1_output.model_dump())),
        ("citation_grounding",     check_citation_grounding(state.layer1_output, state.numeric_block, state.qualitative_block)),
        ("confidence_consistency", check_confidence_consistency(state.layer1_output, state.persona_outputs)),
        ("invalidation_concrete",  check_invalidation_concreteness(state.layer1_output)),
    ]

    failed = []
    for name, result in checks:
        state.guardrail_checks.append({"name": name, "passed": result.passed, "reason": result.reason})
        if not result.passed:
            failed.append(f"{name}: {result.reason}")

    if failed:
        state.guardrail_retries += 1
        state.guardrail_retry_note = (
            "Your previous verdict failed the following checks. Fix these issues and resubmit:\n"
            + "\n".join(f"- {f}" for f in failed)
        )
        state.layer1_output = None  # clear so judge re-runs

    return state
```

---

## 5. Retry Logic

```python
MAX_GUARDRAIL_RETRIES = 2

def should_retry(state: PipelineState) -> str:
    if state.layer1_output is not None:
        return "layer2_gate"             # guardrails passed → continue
    if state.guardrail_retries <= MAX_GUARDRAIL_RETRIES:
        return "judge_synthesis"         # retry judge with note
    return "pipeline_cancel"             # exhausted retries → cancel
```

On retry, `state.guardrail_retry_note` is passed into `compose_judge_prompt` as the retry note, which appends it to the judge's user message. The judge sees its specific failure reason and must correct it.

Retry 1: append failure reason to prompt.
Retry 2: append failure reason + "This is your final attempt. If you cannot produce a valid verdict, output direction=neutral, confidence=0.30."
After retry 2: if still failing, cancel.

---

## 6. Pipeline Cancellation

```python
def pipeline_cancel_node(state: PipelineState) -> PipelineState:
    state.pipeline_cancelled = True
    if not state.cancellation_reason:
        state.cancellation_reason = (
            f"Guardrail exhausted after {state.guardrail_retries} retries. "
            f"Last checks: {[c for c in state.guardrail_checks if not c['passed']]}"
        )

    # Write cancellation record to eval store immediately
    # This is a background write — do not block
    asyncio.create_task(write_eval_record(state, trace_url=get_current_trace_url()))

    # Increment Prometheus cancellation counter
    PIPELINE_CANCELLATIONS_TOTAL.labels(
        reason=state.guardrail_checks[-1]["name"] if state.guardrail_checks else "unknown"
    ).inc()

    return state
```

Layer 2 never receives control after cancellation. The conditional edge from `output_guardrail` to `pipeline_cancel` short-circuits the graph.

---

## 7. Graceful Fallback vs. Cancel

Not all failures should cancel. Use this decision table:

| Scenario | Action |
|---|---|
| Schema validation fails | Retry judge (max 2) → cancel if exhausted |
| Citation grounding fails | Retry judge with grounding note |
| Confidence too high | Retry judge with consistency note |
| Invalidation vague | Retry judge with concreteness note |
| All 3 retries fail any check | Cancel — write record, do not proceed to Layer 2 |
| Persona call fails (1–2 of 4) | Degraded run — continue, note missing personas |
| All 4 persona calls fail | Cancel — judge has no inputs |
| RAG returns empty block | Continue with warning — fundamentals alone are sufficient |
| Data pull partially fails | Continue with data_gaps noted in numeric block |
| Input guardrail fails | Cancel immediately — do not run any downstream steps |

---

## 8. GuardrailResult Type

```python
from dataclasses import dataclass

@dataclass
class GuardrailResult:
    passed: bool
    check_name: str = ""
    reason: str = ""

    @classmethod
    def ok(cls) -> "GuardrailResult":
        return cls(passed=True)

    @classmethod
    def fail(cls, name: str, reason: str) -> "GuardrailResult":
        return cls(passed=False, check_name=name, reason=reason)
```

---

## 9. Implementation Notes

- All guardrail checks are synchronous and CPU-bound — no async needed
- Injection patterns: start with the list above; extend based on observed attempts in production logs
- `tiktoken` for token estimation in context size check — not exact for Claude but sufficient
- All `guardrail_checks` entries are written to the eval store's `guardrail_checks JSONB` column for analysis
- Do not raise exceptions from guardrail nodes — return state with `pipeline_cancelled=True` and let the graph router handle it
- The `sided_with` and `sided_reasoning` fields from the judge output are used in the confidence consistency check — the judge's own stated alignment is cross-checked against the computed agreement ratio
