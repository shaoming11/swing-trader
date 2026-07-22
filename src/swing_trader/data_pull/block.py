"""NumericBlock builder — merges FundamentalsResult + MacroResult into plain text.

The output is a fixed-template string injected directly into the composed prompt.
No LLM, no summarization, no prose beyond what the template provides.
"""
from __future__ import annotations

from datetime import date

from swing_trader.schemas.pipeline import FundamentalsResult, MacroResult, NumericBlock


def build_numeric_block(
    ticker: str,
    window_start: date,
    window_end: date,
    fundamentals: FundamentalsResult,
    macro: MacroResult,
) -> NumericBlock:
    f = fundamentals
    m = macro
    lines: list[str] = []

    lines.append(f"=== STRUCTURED DATA: {ticker} ({window_start} to {window_end}) ===\n")

    # ── Fundamentals ──────────────────────────────────────────────────────────
    lines.append("--- FUNDAMENTALS ---")
    report_label = str(f.report_date) if f.report_date else "N/A"
    lines.append(f"Earnings (most recent report: {report_label})")

    lines.append(_field("EPS Actual",      _fmt_dollar(f.eps_actual)))
    lines.append(_field("EPS Estimate",    _fmt_dollar(f.eps_estimate)))
    lines.append(_field("EPS Surprise",    _fmt_pct(f.eps_surprise_pct, signed=True)))
    lines.append(_field("Revenue Actual",  _fmt_billions(f.revenue_actual_b)))
    lines.append(_field("Revenue Estimate",_fmt_billions(f.revenue_estimate_b)))
    lines.append(_field("Revenue Surprise",_fmt_pct(f.revenue_surprise_pct, signed=True)))
    lines.append(_field("Revenue YoY",     _fmt_pct(f.revenue_yoy_pct, signed=True)))
    lines.append(
        _field(
            "Gross Margin",
            f"{f.gross_margin_pct:.1f}% (prior quarter: {f.prior_gross_margin_pct:.1f}%)"
            if f.gross_margin_pct is not None and f.prior_gross_margin_pct is not None
            else _na(f.gross_margin_pct),
        )
    )
    guidance_str = f.guidance_direction or "not provided"
    if f.guidance_note:
        guidance_str += f"  [{f.guidance_note}]"
    lines.append(_field("Forward Guidance", guidance_str))

    lines.append("")
    lines.append(f"Valuation (as of {window_start})")
    lines.append(_field("P/E Trailing",   _fmt_x(f.pe_trailing)))
    lines.append(_field("P/E Forward",    _fmt_x(f.pe_forward)))
    lines.append(_field("Price",          _fmt_dollar(f.price_at_start)))
    lines.append(_field("52-Week High",   _fmt_dollar(f.fifty_two_high)))
    lines.append(_field("52-Week Low",    _fmt_dollar(f.fifty_two_low)))

    lines.append("")
    lines.append("Corporate Actions in Window")
    if f.corporate_actions:
        for action in f.corporate_actions:
            lines.append(f"  • {action}")
    else:
        lines.append("  None identified")

    # ── Macro ─────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"--- MACRO ({m.sector.upper()}) ---")
    for dp in m.series:
        if dp.value_start is not None and dp.value_end is not None and dp.value_start != dp.value_end:
            val_str = (
                f"{dp.value_end:.2f}{dp.unit}  "
                f"(window start: {dp.value_start:.2f}{dp.unit})"
            )
        elif dp.value_end is not None:
            val_str = f"{dp.value_end:.2f}{dp.unit}  [date: {dp.date_end}]"
        else:
            val_str = "N/A"
        lines.append(_field(dp.label, val_str))

    if m.fomc_in_window and m.fomc_event:
        lines.append(
            _field("FOMC in Window", f"YES  [{m.fomc_event.decision}]")
        )
    else:
        lines.append(_field("FOMC in Window", "No"))

    # ── Notes / data gaps ─────────────────────────────────────────────────────
    all_gaps = list(f.data_gaps) + list(m.data_gaps)
    if all_gaps:
        lines.append("")
        lines.append("--- NOTES (Data Gaps) ---")
        for gap in all_gaps:
            lines.append(f"  • {gap}")

    rendered = "\n".join(lines)

    return NumericBlock(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        rendered_text=rendered,
        data_gaps=all_gaps,
        sources_used=_sources_used(fundamentals, macro),
        fundamentals_report_date=f.report_date,
        macro_series_pulled=[dp.series_id for dp in m.series],
    )


# ── Formatting helpers ────────────────────────────────────────────────────────

def _field(label: str, value: str) -> str:
    return f"  {label:<22}{value}"


def _na(v) -> str:
    return "N/A" if v is None else str(v)


def _fmt_dollar(v: float | None) -> str:
    return f"${v:.2f}" if v is not None else "N/A"


def _fmt_billions(v: float | None) -> str:
    return f"${v:.2f}B" if v is not None else "N/A"


def _fmt_pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _fmt_x(v: float | None) -> str:
    return f"{v:.1f}x" if v is not None else "N/A"


def _sources_used(f: FundamentalsResult, m: MacroResult) -> list[str]:
    sources = []
    if f.eps_actual is not None or f.revenue_actual_b is not None:
        sources.append("FMP")
    if f.price_at_start is not None:
        sources.append("Polygon")
    if m.series:
        sources.append("FRED")
    return list(dict.fromkeys(sources))
