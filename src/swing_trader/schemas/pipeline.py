"""All Pydantic schemas for the swing trader pipeline.

These are the canonical data contracts shared across every module.
Import from here — never redefine these elsewhere.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Layer 1 output ────────────────────────────────────────────────────────────

Direction = Literal["bullish", "bearish", "neutral"]
MagnitudeBucket = Literal["0-3%", "3-8%", "8%+"]
HoldWindowBucket = Literal["days", "weeks", "quarter"]
Driver = Literal["fundamental", "macro", "sentiment", "technical"]

_VAGUE_PHRASES = re.compile(
    r"\b(if things go wrong|if it breaks down|uncertainty|market conditions|"
    r"negative sentiment|deteriorates|worsens|unclear|significant|broadly)\b",
    re.IGNORECASE,
)
_CONCRETE_PATTERN = re.compile(
    r"(\$[\d,.]+|[\d,.]+%|[\d,.]+\s*(billion|million|B|M)|"
    r"CPI|GDP|FOMC|earnings|Fed|rate cut|rate hike|[A-Z]{2,5}\s+report)",
    re.IGNORECASE,
)


class Layer1Output(BaseModel):
    direction: Direction
    magnitude_bucket: MagnitudeBucket
    confidence: float = Field(ge=0.0, le=1.0)
    dominant_drivers: list[Driver] = Field(min_length=1)
    invalidation_condition: str = Field(min_length=20)
    hold_window_bucket: HoldWindowBucket
    thesis: str = Field(min_length=30)

    # Stored in eval store for feature attribution; not in the prompt schema
    sided_with: list[str] = Field(default_factory=list)
    sided_reasoning: str = ""

    @field_validator("invalidation_condition")
    @classmethod
    def invalidation_must_be_concrete(cls, v: str) -> str:
        if _VAGUE_PHRASES.search(v) and not _CONCRETE_PATTERN.search(v):
            raise ValueError(
                f"invalidation_condition is too vague: '{v}'. "
                "Must reference a price level, percentage, or named event."
            )
        return v

    @field_validator("dominant_drivers")
    @classmethod
    def drivers_must_be_unique(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("dominant_drivers must not contain duplicates")
        return v


# ── Layer 2 output ────────────────────────────────────────────────────────────

class PositionCard(BaseModel):
    gate_passed: bool
    gate_skip_reason: str | None = None

    # Populated only when gate_passed=True
    ticker: str = ""
    entry_price: float | None = None
    target_price: float | None = None
    stop_loss: str | None = None       # price string or event trigger description
    hold_window_start: date | None = None
    hold_window_end: date | None = None
    position_size_pct: float | None = None   # fraction of portfolio, e.g. 0.05 = 5%

    # Sizing audit trail
    kelly_full: float | None = None
    kelly_fractional: float | None = None
    volatility_used: float | None = None

    thesis: str = ""


# ── RAG / qualitative block ───────────────────────────────────────────────────

SentimentLabel = Literal["bullish", "bearish", "neutral"]
SourceType = Literal["news", "analyst", "social", "macro"]


class QualItem(BaseModel):
    source_type: SourceType
    date: str
    source: str
    sentiment_label: SentimentLabel
    sentiment_reason: str
    summary: str        # chunk body, max 200 tokens
    relevance_score: float


class QualitativeBlock(BaseModel):
    ticker: str
    window_start: date
    window_end: date
    chunks_retrieved: int
    chunks_used: int
    items: list[QualItem] = Field(default_factory=list)

    def render(self) -> str:
        """Render to the condensed bullet format injected into the composed prompt."""
        if not self.items:
            return (
                f"=== QUALITATIVE CONTEXT: {self.ticker} "
                f"({self.window_start} to {self.window_end}) ===\n\n"
                "[No relevant qualitative context found for this window.]\n"
            )

        lines = [
            f"=== QUALITATIVE CONTEXT: {self.ticker} "
            f"({self.window_start} to {self.window_end}) ===\n"
        ]

        by_type: dict[str, list[QualItem]] = {}
        priority = ["analyst", "news", "macro", "social"]
        for item in self.items:
            by_type.setdefault(item.source_type, []).append(item)

        for st in priority:
            if st not in by_type:
                continue
            lines.append(f"\n[{st.upper()}]")
            for item in by_type[st]:
                lines.append(
                    f"• {item.date} | {item.source} | {item.sentiment_label.upper()} "
                    f"— {item.sentiment_reason}"
                )
                lines.append(f'  "{item.summary}"')

        return "\n".join(lines)


# ── Numeric block (data pull output) ─────────────────────────────────────────

class FundamentalsResult(BaseModel):
    ticker: str
    report_date: date | None = None
    eps_actual: float | None = None
    eps_estimate: float | None = None
    eps_surprise_pct: float | None = None
    revenue_actual_b: float | None = None      # billions
    revenue_estimate_b: float | None = None
    revenue_surprise_pct: float | None = None
    revenue_yoy_pct: float | None = None
    gross_margin_pct: float | None = None
    prior_gross_margin_pct: float | None = None
    guidance_direction: str | None = None      # raised | lowered | maintained | not provided
    guidance_note: str = ""
    pe_trailing: float | None = None
    pe_forward: float | None = None
    price_at_start: float | None = None
    fifty_two_high: float | None = None
    fifty_two_low: float | None = None
    corporate_actions: list[str] = Field(default_factory=list)
    data_gaps: list[str] = Field(default_factory=list)

    @classmethod
    def empty(cls, ticker: str) -> "FundamentalsResult":
        return cls(
            ticker=ticker,
            data_gaps=["All fundamentals unavailable — API call failed"],
        )


class MacroDataPoint(BaseModel):
    series_id: str
    label: str
    value_start: float | None = None
    value_end: float | None = None
    date_start: str | None = None
    date_end: str | None = None
    unit: str = ""


class FOMCEvent(BaseModel):
    meeting_date: str
    decision: str     # e.g. "held at 5.25-5.50%" or "cut 25bps to 5.00-5.25%"


class MacroResult(BaseModel):
    sector: str
    series: list[MacroDataPoint] = Field(default_factory=list)
    fomc_in_window: bool = False
    fomc_event: FOMCEvent | None = None
    data_gaps: list[str] = Field(default_factory=list)

    @classmethod
    def empty(cls) -> "MacroResult":
        return cls(
            sector="unknown",
            data_gaps=["All macro data unavailable — API call failed"],
        )


class NumericBlock(BaseModel):
    ticker: str
    window_start: date
    window_end: date
    rendered_text: str
    data_gaps: list[str] = Field(default_factory=list)
    sources_used: list[str] = Field(default_factory=list)
    fundamentals_report_date: date | None = None
    macro_series_pulled: list[str] = Field(default_factory=list)


# ── Guardrail ─────────────────────────────────────────────────────────────────

class GuardrailCheck(BaseModel):
    name: str
    passed: bool
    reason: str = ""


# ── Persona outputs ───────────────────────────────────────────────────────────

class PersonaOutputs(BaseModel):
    bull: str = ""
    bear: str = ""
    macro: str = ""
    technicals: str = ""

    def agreement_ratio(self, direction: Direction) -> float:
        """Fraction of personas that agree with the judge's direction."""
        sentiment_map = {
            "bullish": ["bullish", "bull", "upside", "long"],
            "bearish": ["bearish", "bear", "downside", "short"],
            "neutral": ["neutral", "mixed", "uncertain"],
        }
        keywords = sentiment_map.get(direction, [])
        texts = [self.bull, self.bear, self.macro, self.technicals]
        filled = [t for t in texts if t]
        if not filled:
            return 0.5
        matches = sum(
            any(kw in t.lower() for kw in keywords) for t in filled
        )
        return matches / len(filled)
