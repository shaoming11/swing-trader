# Layer 2 Pipeline — Design Spec

Covers the position sizing pipeline: gate, sizing, and position card generation. Takes Layer 1 output and produces a concrete, actionable trade. No LLM is involved — this is fully deterministic math.

---

## 1. Pipeline Overview

```
[Layer 1 Output]
       │
  [GATE CHECK]
  confidence > threshold AND magnitude_bucket != negligible
       │
   ┌───┴───┐
  FAIL    PASS
   │       │
  END  [VOLATILITY FETCH]
           │
       [KELLY SIZING]
           │
       [PRICE INPUTS]
           │
       [POSITION CARD]
```

---

## 2. Gate Logic

### 2.1 Gate Conditions

Both conditions must be true to pass:

```python
CONFIDENCE_THRESHOLD = 0.60       # minimum confidence to act
NEGLIGIBLE_BUCKETS = {"0-3%"}     # magnitude buckets too small to trade

def evaluate_gate(output: Layer1Output) -> GateResult:
    if output.confidence < CONFIDENCE_THRESHOLD:
        return GateResult.skip(
            f"Confidence {output.confidence:.2f} below threshold {CONFIDENCE_THRESHOLD}"
        )
    if output.magnitude_bucket in NEGLIGIBLE_BUCKETS:
        return GateResult.skip(
            f"Magnitude bucket {output.magnitude_bucket!r} is too small to justify a position"
        )
    return GateResult.act()
```

### 2.2 Threshold Rationale

`0.60` is the default threshold. Below 0.60, the system has not demonstrated sufficient calibration to act. This value should be updated based on the calibration curve from the eval store: if 60%-confidence calls actually land at 55%, raise the threshold.

Skipped runs are written to the eval store with `gate_passed=False` and `gate_skip_reason`. They are not errors — they are valid "no trade" decisions.

---

## 3. Volatility Input

Volatility is the primary risk input to the sizing formula. Fetch 20-day realized volatility (annualized) for the ticker at `window_start`.

```python
import numpy as np

async def fetch_realized_volatility(ticker: str, as_of: date) -> float:
    """
    20-day realized volatility, annualized, as of `as_of`.
    Uses daily close prices from Polygon.
    """
    end = as_of
    start = as_of - timedelta(days=30)  # fetch extra days to get 20 trading days

    prices = await fetch_daily_closes(ticker, start, end)  # from Polygon cache
    prices = prices[-20:]  # last 20 trading days

    if len(prices) < 10:
        # Not enough price history — use sector median as fallback
        return get_sector_median_volatility(ticker)

    log_returns = np.log(np.array(prices[1:]) / np.array(prices[:-1]))
    daily_vol = np.std(log_returns, ddof=1)
    annualized_vol = daily_vol * np.sqrt(252)
    return annualized_vol
```

Typical ranges for reference:

| Volatility Range | Description |
|---|---|
| < 0.20 | Low vol (e.g., utilities, staples) |
| 0.20 – 0.40 | Moderate vol (most large-cap names) |
| 0.40 – 0.70 | High vol (growth, biotech, small-cap) |
| > 0.70 | Very high vol — size down aggressively |

---

## 4. Fractional Kelly Sizing

### 4.1 Kelly Criterion Background

The Kelly criterion gives the optimal fraction of capital to bet to maximize long-run growth:

```
f* = (p * b - q) / b
```

Where:
- `p` = probability of winning
- `q` = 1 - p (probability of losing)
- `b` = net odds (win amount / loss amount)

Full Kelly is too aggressive for real markets (it assumes perfect probability estimates and no transaction costs). Fractional Kelly (typically 25–50% of full Kelly) reduces variance while preserving most of the long-run growth.

### 4.2 Mapping Layer 1 Output to Kelly Inputs

```python
MAGNITUDE_BUCKET_MIDPOINTS = {
    "0-3%":  0.015,   # 1.5%
    "3-8%":  0.055,   # 5.5%
    "8%+":   0.12,    # 12% (conservative estimate for the open-ended bucket)
}

KELLY_FRACTION = 0.25   # 25% of full Kelly — conservative, reduces variance

def compute_position_size(
    confidence: float,
    magnitude_bucket: str,
    volatility: float,
    direction: str
) -> float:
    """
    Returns position size as a fraction of portfolio (0.0 to MAX_POSITION_SIZE).
    """
    # Win probability estimate: confidence is the model's stated probability of being right
    p = confidence
    q = 1.0 - p

    # Expected gain: the magnitude bucket midpoint (if direction correct)
    expected_gain = MAGNITUDE_BUCKET_MIDPOINTS[magnitude_bucket]

    # Expected loss: assume stop-loss limits downside to half the expected gain
    # (this is a simplification — actual stop-loss is event-based from invalidation_condition)
    expected_loss = expected_gain * 0.5

    if expected_loss == 0:
        return 0.0

    b = expected_gain / expected_loss

    # Full Kelly
    f_full = (p * b - q) / b
    f_full = max(f_full, 0.0)   # never go negative (no shorting via Kelly)

    # Fractional Kelly
    f_kelly = f_full * KELLY_FRACTION

    # Volatility adjustment: scale down for high-vol names
    # vol_scalar approaches 0 as vol approaches 1.0 (100% annualized)
    vol_scalar = max(0.2, 1.0 - volatility)

    # Final size
    position_size = f_kelly * vol_scalar

    return round(min(position_size, MAX_POSITION_SIZE), 4)
```

### 4.3 Position Size Constraints

```python
MAX_POSITION_SIZE = 0.15     # never more than 15% of portfolio in one name
MIN_POSITION_SIZE = 0.02     # never open a position smaller than 2% (too small to matter)
MAX_TOTAL_EXPOSURE = 0.60    # never more than 60% of portfolio deployed at once
```

If `position_size < MIN_POSITION_SIZE` after the formula, return `GateResult.skip("position_size_too_small")` — do not open a negligible position.

### 4.4 Worked Example

```
Ticker: AAPL
confidence = 0.78
magnitude_bucket = "3-8%"
direction = bullish
volatility = 0.28 (annualized)

expected_gain = 0.055
expected_loss = 0.0275
b = 2.0

f_full = (0.78 * 2.0 - 0.22) / 2.0 = (1.56 - 0.22) / 2.0 = 0.67
f_kelly = 0.67 * 0.25 = 0.168
vol_scalar = max(0.2, 1.0 - 0.28) = 0.72
position_size = 0.168 * 0.72 = 0.121 → 12.1% of portfolio

Capped at MAX_POSITION_SIZE = 15% → final size = 12.1%
```

---

## 5. Price Inputs

```python
async def fetch_price_inputs(ticker: str, as_of: date) -> PriceInputs:
    # Use prior close (T-1) as the "current" price to avoid lookahead bias
    close = await fetch_daily_closes(ticker, as_of - timedelta(days=5), as_of)
    current_price = close[-1]

    # Entry zone: ±0.5% of current price
    entry_low  = round(current_price * 0.995, 2)
    entry_high = round(current_price * 1.005, 2)

    return PriceInputs(
        current_price=current_price,
        entry_low=entry_low,
        entry_high=entry_high
    )
```

---

## 6. Target Price Calculation

Apply the magnitude bucket midpoint to the entry price:

```python
def compute_target_price(
    entry_price: float,
    magnitude_bucket: str,
    direction: str
) -> float:
    midpoint = MAGNITUDE_BUCKET_MIDPOINTS[magnitude_bucket]
    if direction == "bullish":
        return round(entry_price * (1 + midpoint), 2)
    elif direction == "bearish":
        return round(entry_price * (1 - midpoint), 2)
    else:
        return entry_price   # neutral — no target
```

For the `"8%+"` bucket, the midpoint (12%) is a conservative floor. If a quant regression layer is added later, its output feeds this instead.

---

## 7. Stop-Loss Translation

The stop-loss originates from Layer 1's `invalidation_condition`. Two types:

### Price-based invalidation
Pattern: contains `$\d+` or `below \$`, `above \$`, `breaks \$`

```python
PRICE_PATTERN = re.compile(r'\$(\d+(?:\.\d+)?)')

def extract_price_stop(invalidation_condition: str, entry_price: float) -> str:
    match = PRICE_PATTERN.search(invalidation_condition)
    if match:
        stop_price = float(match.group(1))
        return f"${stop_price:.2f} (exit if price breaches this level)"
    return None
```

### Event-based invalidation
Pattern: contains a named event, percentage, or indicator

```python
def classify_stop(invalidation_condition: str, entry_price: float) -> StopLoss:
    price_stop = extract_price_stop(invalidation_condition, entry_price)
    if price_stop:
        return StopLoss(type="price", trigger=price_stop, raw=invalidation_condition)

    # Event-based: pass through as-is — it is a monitoring condition, not a price
    return StopLoss(type="event", trigger=invalidation_condition, raw=invalidation_condition)
```

Event-based stops require the monitoring layer to watch for the named event (e.g., a CPI print). That monitoring is out of scope for this pipeline — the stop-loss field communicates the condition to the human or downstream system.

---

## 8. Hold Window Translation

```python
from datetime import date, timedelta

HOLD_WINDOW_DAYS = {
    "days":    (3, 10),
    "weeks":   (10, 30),
    "quarter": (30, 90),
}

def compute_hold_window(
    hold_window_bucket: str,
    from_date: date
) -> tuple[date, date]:
    min_days, max_days = HOLD_WINDOW_DAYS[hold_window_bucket]
    # Use midpoint of the range
    mid_days = (min_days + max_days) // 2
    window_start = from_date
    window_end = from_date + timedelta(days=mid_days)
    return window_start, window_end
```

---

## 9. Position Card Schema

```python
from pydantic import BaseModel
from datetime import date
from typing import Literal

class StopLoss(BaseModel):
    type: Literal["price", "event"]
    trigger: str
    raw: str                        # original invalidation_condition from Layer 1

class PositionCard(BaseModel):
    ticker: str
    direction: Literal["bullish", "bearish", "neutral"]
    entry_price: float
    entry_low: float
    entry_high: float
    target_price: float
    stop_loss: StopLoss
    hold_window_start: date
    hold_window_end: date
    position_size_pct: float        # fraction of portfolio (e.g., 0.121 = 12.1%)
    confidence: float               # from Layer 1
    magnitude_bucket: str           # from Layer 1
    dominant_drivers: list[str]     # from Layer 1
    thesis: str                     # from Layer 1
    volatility_used: float          # annualized vol used in sizing formula
    kelly_full: float               # full Kelly value (for audit)
    kelly_fractional: float         # fractional Kelly before vol adjustment
```

---

## 10. LangGraph Integration

```python
async def layer2_gate_node(state: PipelineState) -> PipelineState:
    gate = evaluate_gate(state.layer1_output)
    state.gate_passed = gate.acted
    state.gate_skip_reason = gate.reason

    log_node_output("layer2_gate", {
        "passed": gate.acted,
        "reason": gate.reason,
        "confidence": state.layer1_output.confidence,
        "magnitude_bucket": state.layer1_output.magnitude_bucket
    })
    LAYER2_GATE_TOTAL.labels(decision="acted" if gate.acted else "skipped").inc()
    return state

async def layer2_sizing_node(state: PipelineState) -> PipelineState:
    l1 = state.layer1_output

    volatility = await fetch_realized_volatility(state.ticker, state.window_start)
    price_inputs = await fetch_price_inputs(state.ticker, state.window_start)
    position_size = compute_position_size(l1.confidence, l1.magnitude_bucket, volatility, l1.direction)

    if position_size < MIN_POSITION_SIZE:
        state.gate_passed = False
        state.gate_skip_reason = f"Position size {position_size:.1%} below minimum {MIN_POSITION_SIZE:.1%}"
        return state

    target = compute_target_price(price_inputs.current_price, l1.magnitude_bucket, l1.direction)
    stop = classify_stop(l1.invalidation_condition, price_inputs.current_price)
    hold_start, hold_end = compute_hold_window(l1.hold_window_bucket, state.window_start)

    state.layer2_output = PositionCard(
        ticker=state.ticker,
        direction=l1.direction,
        entry_price=price_inputs.current_price,
        entry_low=price_inputs.entry_low,
        entry_high=price_inputs.entry_high,
        target_price=target,
        stop_loss=stop,
        hold_window_start=hold_start,
        hold_window_end=hold_end,
        position_size_pct=position_size,
        confidence=l1.confidence,
        magnitude_bucket=l1.magnitude_bucket,
        dominant_drivers=l1.dominant_drivers,
        thesis=l1.thesis,
        volatility_used=volatility,
        kelly_full=compute_full_kelly(l1.confidence, l1.magnitude_bucket),
        kelly_fractional=compute_full_kelly(l1.confidence, l1.magnitude_bucket) * KELLY_FRACTION
    )

    log_node_output("layer2_sizing", {
        "position_size_pct": position_size,
        "target_price": target,
        "stop_loss_type": stop.type,
        "volatility": volatility
    })
    return state

# Conditional edge from gate
def gate_router(state: PipelineState) -> str:
    return "layer2_sizing" if state.gate_passed else "end"

graph.add_node("layer2_gate", layer2_gate_node)
graph.add_node("layer2_sizing", layer2_sizing_node)
graph.add_conditional_edges("layer2_gate", gate_router)
```

---

## 11. Implementation Notes

- **No LLM anywhere in Layer 2** — all steps are deterministic math and API lookups
- **Volatility fallback:** if Polygon returns fewer than 10 trading days of data, use sector median vol (precomputed table, updated monthly)
- **Short positions:** current sizing formula handles bearish direction by applying the magnitude bucket to the downside. Actual short mechanics (borrowing, margin) are out of scope — the position card communicates intent
- **Portfolio-level guardrail:** before finalizing position size, check `MAX_TOTAL_EXPOSURE` against open positions in the portfolio state; reduce size proportionally if needed
- **Kelly fraction tuning:** `KELLY_FRACTION = 0.25` is the starting point. Adjust based on the eval loop's timing error metric — if positions consistently overshoot, reduce the fraction
- **Price source:** Polygon delayed data is sufficient for swing trades; no need for real-time tick data
