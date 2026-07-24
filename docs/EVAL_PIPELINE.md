# Eval Pipeline — Design Spec

Covers the evaluation framework and self-improvement loop. The eval pipeline runs alongside the main pipeline — it consumes run records from the eval store, measures calibration and accuracy, and drives prompt and corpus updates.

---

## 1. Where Eval Fits

```
[Main Pipeline Runs] ──→ [Eval Store]
                               │
              ┌────────────────┼──────────────────────┐
              │                │                      │
    [Ground Truth       [Regression Suite]   [Self-Improvement Loop]
     Population]        (on prompt change)    (per quarter)
              │                │                      │
    [Calibration        [Deploy Gate]         [Prompt + Corpus Updates]
     Curves]
```

---

## 2. Eval Hierarchy

| Priority | Type | Trigger | Signal |
|---|---|---|---|
| 1 | Golden set | Always | Primary accuracy ground truth |
| 2 | Regression suite | Every prompt/rule/model change | Catch backsliding |
| 3 | Adversarial | On demand / quarterly | Confirm robustness to bad inputs |
| 4 | LLM-as-judge | Every run | Reasoning quality per call |
| 5 | Human eval | Monthly spot-check | Sanity check on edge cases |

Human eval is not the primary signal — it is a sanity check. The calibration curve and regression suite are.

---

## 3. Golden Set

### 3.1 What It Contains

One entry per ticker/quarter combination with known outcome. Minimum viable golden set: 50 entries across at least 5 tickers and 4 quarters.

```python
@dataclass
class GoldenEntry:
    entry_id: str
    ticker: str
    quarter: str                     # e.g., "2023Q4"
    window_start: date
    window_end: date

    # Frozen inputs (no lookahead — only data available before window_end)
    frozen_numeric_block: str        # rendered text of the numeric block
    frozen_rag_chunks: list[dict]    # top-10 chunks after rerank
    frozen_persona_outputs: dict     # optional: cached persona arguments

    # Ground truth (manually verified)
    actual_direction: str            # bullish | bearish | neutral
    actual_magnitude_pct: float      # actual % move in the hold window
    actual_magnitude_bucket: str     # which bucket the actual move falls in
    dominant_driver_label: str       # manually labeled: fundamental | macro | sentiment | technical
    notes: str                       # curator notes on what drove the move
```

### 3.2 Curation Process

1. Select a ticker and quarter
2. Pull all inputs as they were available at `window_start` (use cached API responses, not live re-fetch)
3. Look up actual price at `window_start` and `window_end` from Polygon historical data
4. Compute actual magnitude and bucket
5. Label dominant driver: review the actual news from the period, pick the factor that most explains the move
6. Write notes explaining the outcome

**Lookahead bias rule:** The frozen inputs must only contain data that was publicly available before `window_end`. Run a date filter check on every RAG chunk's `date` field before including it.

### 3.3 Storage

```
golden_set/
  entries/
    {entry_id}.json       # one file per golden entry
  manifest.json           # list of all entry IDs with ticker, quarter, created_at
```

---

## 4. Regression Suite

### 4.1 What Triggers a Regression Run

- Any change to a persona system prompt
- Any change to the judge system prompt
- Any change to output instruction text
- Any change to the guardrail check logic
- Any model upgrade (e.g., sonnet → opus for persona calls)
- Any change to RAG retrieval parameters (top-k, reranker threshold)

Trigger is automated via CI: any commit that modifies `prompts/`, `guardrails/`, or `pipeline_config.py` triggers the regression suite.

### 4.2 Regression Run Process

```python
async def run_regression_suite(
    golden_set: list[GoldenEntry],
    pipeline_version: str
) -> RegressionReport:

    results = []
    async for entry in batch_run(golden_set, concurrency=5):
        # Run the pipeline with frozen inputs (bypass live data pull)
        output = await run_pipeline_with_frozen_inputs(
            numeric_block_text=entry.frozen_numeric_block,
            rag_chunks=entry.frozen_rag_chunks,
            pipeline_version=pipeline_version
        )
        results.append(RegressionResult(
            entry_id=entry.entry_id,
            ticker=entry.ticker,
            quarter=entry.quarter,
            predicted_direction=output.direction if output else None,
            predicted_bucket=output.magnitude_bucket if output else None,
            actual_direction=entry.actual_direction,
            actual_bucket=entry.actual_magnitude_bucket,
            hit=output and output.direction == entry.actual_direction
                and output.magnitude_bucket == entry.actual_magnitude_bucket,
            confidence=output.confidence if output else None,
            cancelled=output is None
        ))

    return RegressionReport(
        pipeline_version=pipeline_version,
        total=len(results),
        hit_rate=sum(r.hit for r in results) / len(results),
        cancellation_rate=sum(r.cancelled for r in results) / len(results),
        avg_confidence=sum(r.confidence for r in results if r.confidence) / len(results),
        results=results
    )
```

### 4.3 Deploy Gate

A deploy is blocked if any of the following regress vs. the previous passing version:

| Metric | Regression Threshold |
|---|---|
| Hit rate | Drops > 5 percentage points |
| Cancellation rate | Increases > 10 percentage points |
| Avg confidence | Changes > 0.10 (up or down — both are suspect) |
| Calibration error | Increases > 10 percentage points in any bucket |

```python
def check_regression(current: RegressionReport, baseline: RegressionReport) -> list[str]:
    failures = []
    if baseline.hit_rate - current.hit_rate > 0.05:
        failures.append(f"Hit rate regressed: {baseline.hit_rate:.1%} → {current.hit_rate:.1%}")
    if current.cancellation_rate - baseline.cancellation_rate > 0.10:
        failures.append(f"Cancellation rate increased: {baseline.cancellation_rate:.1%} → {current.cancellation_rate:.1%}")
    if abs(current.avg_confidence - baseline.avg_confidence) > 0.10:
        failures.append(f"Avg confidence drifted: {baseline.avg_confidence:.2f} → {current.avg_confidence:.2f}")
    return failures
```

---

## 5. Adversarial Eval

### 5.1 Scenarios

| Scenario | Description | Expected Behavior |
|---|---|---|
| Conflicting signals | Bull news + miss on earnings in same window | Should not be 90%+ confident in either direction |
| Misleading headline | Positive headline but negative body text | Should weight body over headline |
| Social pump | 90% bullish StockTwits with no fundamental basis | Should weight sentiment < fundamental |
| Macro noise during earnings | FOMC rate cut + earnings miss | Should surface the conflict, not ignore one |
| No news | Retrieval returns empty block | Should produce a verdict based on fundamentals alone, with lower confidence |
| Stale data | Fundamentals from 2 quarters ago | Should flag data staleness and reduce confidence |

### 5.2 Adversarial Entry Construction

```python
@dataclass
class AdversarialEntry:
    scenario: str
    description: str
    injected_numeric_block: str      # may contain altered or stale data
    injected_rag_chunks: list[dict]  # may contain misleading content
    expected_max_confidence: float   # pipeline should not exceed this
    expected_direction: str | None   # None = any direction is acceptable
    pass_condition: str              # human-readable description of what "pass" means
```

### 5.3 Pass Conditions

- Confidence does not exceed `expected_max_confidence`
- Thesis does not uncritically echo misleading information without hedging
- Guardrail checks catch injected vague invalidation conditions
- Pipeline does not cancel on empty RAG block (degraded run is acceptable)

---

## 6. LLM-as-Judge (Reasoning Quality)

### 6.1 What Is Evaluated

Per-run reasoning quality, independent of outcome. A call can be wrong (bearish when price went up) but have high-quality reasoning (the bear case was logical given available data). The LLM-as-judge scores reasoning, not outcomes.

### 6.2 Judge Prompt

```
You are evaluating the reasoning quality of a trading AI's verdict.

INPUT DATA:
{numeric_block_text}

QUALITATIVE CONTEXT (top chunks):
{top_3_chunks}

VERDICT PRODUCED:
Direction: {direction}
Magnitude: {magnitude_bucket}
Confidence: {confidence}
Dominant drivers: {dominant_drivers}
Invalidation condition: {invalidation_condition}
Thesis: {thesis}

PERSONA ARGUMENTS:
Bull: {persona_bull}
Bear: {persona_bear}
Macro: {persona_macro}
Technicals: {persona_technicals}

Rate the verdict on each dimension from 1–5:
1. Evidence grounding: Does the thesis follow from the data provided?
2. Driver attribution: Do the dominant_drivers actually explain the stated direction?
3. Confidence calibration: Is the confidence consistent with how split the personas were?
4. Invalidation quality: Is the invalidation_condition specific and checkable?
5. Thesis clarity: Is the thesis a clear, coherent 2–4 sentence argument?

Return JSON:
{
  "scores": {"evidence_grounding": int, "driver_attribution": int,
             "confidence_calibration": int, "invalidation_quality": int,
             "thesis_clarity": int},
  "overall": float,    // average of the 5 scores
  "flags": [...]       // list of specific issues found, if any
}
```

### 6.3 Storing Judge Scores

LLM-as-judge scores are written to a separate table (not the main `pipeline_runs` table) to keep schema clean:

```sql
CREATE TABLE reasoning_scores (
    run_id          UUID REFERENCES pipeline_runs(run_id),
    evidence_grounding      INT,
    driver_attribution      INT,
    confidence_calibration  INT,
    invalidation_quality    INT,
    thesis_clarity          INT,
    overall                 FLOAT,
    flags                   TEXT[],
    scored_at               TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 7. Calibration Curve

### 7.1 Computation

Group completed runs by stated confidence bucket, measure actual hit rate per bucket:

```python
CONFIDENCE_BUCKETS = [
    ("90%+",  0.85, 1.00),
    ("70-85%", 0.65, 0.85),
    ("50-65%", 0.45, 0.65),
    ("<50%",   0.00, 0.45),
]

def compute_calibration_curve(runs: list[dict]) -> list[CalibrationPoint]:
    points = []
    for label, low, high in CONFIDENCE_BUCKETS:
        bucket = [r for r in runs if low <= r["confidence"] < high and r["ground_truth_populated"]]
        if len(bucket) < 10:
            continue   # not enough samples for a reliable estimate
        hit_rate = sum(r["hit"] for r in bucket) / len(bucket)
        points.append(CalibrationPoint(
            label=label,
            stated_confidence_midpoint=(low + high) / 2,
            actual_hit_rate=hit_rate,
            sample_size=len(bucket),
            calibration_error=abs((low + high) / 2 - hit_rate)
        ))
    return points
```

### 7.2 Interpreting the Calibration Curve

| Pattern | Interpretation | Action |
|---|---|---|
| 90% bucket lands at 60% | Model is systematically overconfident | Add "be less certain" instruction to judge prompt |
| 50% bucket lands at 80% | Model is underconfident | Raise confidence threshold or relax constraint |
| All buckets clustered near 0.70 | Model isn't differentiating — everything feels like 70% | Strengthen the confidence consistency guardrail |
| Calibration error < 0.10 across buckets | Well-calibrated | No action needed |

Minimum 10 samples per bucket before the metric is acted on. Below 10 samples, flag as "insufficient data."

---

## 8. Feature Attribution on Misses

```python
def compute_feature_attribution(runs: list[dict]) -> list[FeatureAttribution]:
    driver_stats = defaultdict(lambda: {"total": 0, "misses": 0})

    for run in runs:
        if not run["ground_truth_populated"]:
            continue
        for driver in run["dominant_drivers"]:
            driver_stats[driver]["total"] += 1
            if not run["hit"]:
                driver_stats[driver]["misses"] += 1

    return [
        FeatureAttribution(
            driver=driver,
            total_calls=stats["total"],
            miss_count=stats["misses"],
            miss_rate=stats["misses"] / stats["total"] if stats["total"] > 0 else 0
        )
        for driver, stats in driver_stats.items()
    ]
```

High miss rate on a specific driver (e.g., `sentiment` miss rate > 60%) → reduce the weight of sentiment chunks in prompt composition, or add a skepticism instruction to the bull/bear personas about social sentiment.

---

## 9. Self-Improvement Loop

### 9.1 Loop Structure

```
FOR each quarter in [oldest → most recent]:
    1. Load frozen inputs for all golden entries in this quarter
    2. Run pipeline in eval mode (no live data fetch)
    3. Compare predictions to ground truth
    4. Compute: calibration error, miss rate by driver, timing error
    5. Generate improvement actions (see 9.2)
    6. Apply actions (see 9.3)
    7. Run regression suite to confirm no backsliding
    8. If regression passes → commit changes, move to next quarter
    9. If regression fails → revert actions, log conflict for human review
```

### 9.2 Improvement Actions Generated

| Observation | Action |
|---|---|
| Confidence bucket X over-confident by > 15% | Add `"When {X}% of personas agree, cap confidence at {lower_bound}"` to judge prompt |
| `sentiment` driver miss rate > 60% | Add skepticism note to bull/bear persona prompts: "Social sentiment alone is not sufficient justification" |
| `macro` driver miss rate > 50% | Check whether FOMC/CPI events are being correctly flagged in data pull |
| Timing consistently early (price hits before hold window) | Shorten `HOLD_WINDOW_DAYS["days"]` min/max by 20% |
| Timing consistently late | Extend hold window buckets |
| Reasoning score < 3.0 on `invalidation_quality` | Strengthen invalidation concreteness check patterns |
| Corpus chunk flagged as misleading on multiple misses | Mark chunk as `active=false` in vector store; rewrite source file |

### 9.3 Applying Actions

**Prompt changes:** prompts are stored as text files in `prompts/`. Changes are made as commits — every prompt version is tracked in git.

```
prompts/
  persona_bull.txt
  persona_bear.txt
  persona_macro.txt
  persona_technicals.txt
  judge_system.txt
  output_instruction.txt
```

**Corpus changes:** misleading chunks are soft-deleted in the vector store (`active=false`) and their source files are moved to `corpus/_retired/` with a note explaining why they were retired.

**Parameter changes:** `KELLY_FRACTION`, `HOLD_WINDOW_DAYS`, `CONFIDENCE_THRESHOLD` are stored in `pipeline_config.py` — changes are committed and tagged with the quarter that motivated them.

### 9.4 Loop Scheduling

- Runs after each calendar quarter closes (February, May, August, November)
- Requires minimum 20 golden entries with ground truth before the loop is meaningful
- Each loop iteration processes one quarter's data; improvement actions from multiple quarters compound
- All changes are committed under a version tag: `eval-loop-{YYYY}Q{N}`

---

## 10. Prompt Versioning

```python
PIPELINE_VERSION = "1.3.0"   # major.minor.patch

# Major: model change or fundamental architecture change
# Minor: prompt change (persona, judge, output instruction)
# Patch: parameter change (thresholds, window sizes)
```

Every run record in the eval store includes `pipeline_version`. Regression comparisons always compare current version against the last version that passed regression. This makes it possible to bisect which change caused a regression.

---

## 11. Implementation Notes

- **Batch processing:** eval runs use `asyncio.Semaphore(5)` to cap concurrent pipeline runs — avoid hammering LLM APIs
- **Cost control:** persona calls in eval mode use the same models as production. Budget approximately $0.05–$0.15 per golden entry per eval run. 50 entries × $0.10 = $5 per full regression run
- **Frozen inputs:** the eval pipeline bypasses `data_pull` and `rag_retrieval` nodes and injects frozen data directly into `PipelineState`. This ensures reproducibility — the same entry produces the same inputs every run
- **Ground truth timing:** ground truth is populated by a separate daily job (see OBSERVABILITY_PIPELINE.md Section 4.3). The eval loop only runs on entries where `ground_truth_populated = true`
- **Human eval workflow:** reviewers use the human eval UI (OBSERVABILITY_PIPELINE.md Section 6) to inspect entries marked for spot-check. Spot-check selection: 5 random entries per quarter + all entries where LLM-as-judge `overall < 3.0`
