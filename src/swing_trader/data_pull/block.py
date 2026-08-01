"""NumericBlock builder — merges FundamentalsResult + MacroResult into markdown.

Accepts one or more (label, qstart, qend, FundamentalsResult, MacroResult) tuples,
renders each as a markdown section, and joins them with a horizontal rule.
No LLM, no summarization.
"""
from __future__ import annotations

from datetime import date

from swing_trader.schemas.pipeline import FundamentalsResult, MacroResult, NumericBlock


def build_numeric_block(
    ticker: str,
    window_start: date,
    window_end: date,
    quarters: list[tuple[str, date, date, FundamentalsResult, MacroResult]],
) -> NumericBlock:
    sections: list[str] = []
    all_gaps: list[str] = []
    sources: list[str] = []
    macro_series: list[str] = []
    first_report_date = None

    for qlabel, qstart, qend, f, m in quarters:
        sections.append(_render_quarter(ticker, qlabel, qstart, qend, f, m))
        all_gaps.extend(f.data_gaps)
        all_gaps.extend(m.data_gaps)

        if first_report_date is None and f.report_date is not None:
            first_report_date = f.report_date
        if (f.eps_actual is not None or f.price_at_start is not None) and "yfinance" not in sources:
            sources.append("yfinance")
        if m.series and "FRED" not in sources:
            sources.append("FRED")
        for dp in m.series:
            if dp.series_id not in macro_series:
                macro_series.append(dp.series_id)

    return NumericBlock(
        ticker=ticker,
        window_start=window_start,
        window_end=window_end,
        rendered_text="\n\n---\n\n".join(sections),
        data_gaps=all_gaps,
        sources_used=sources,
        fundamentals_report_date=first_report_date,
        macro_series_pulled=macro_series,
    )


# ── Quarter renderer ──────────────────────────────────────────────────────────

def _render_quarter(
    ticker: str,
    label: str,
    qstart: date,
    qend: date,
    f: FundamentalsResult,
    m: MacroResult,
) -> str:
    lines: list[str] = []

    lines.append(f"# {ticker} — {label} ({qstart} to {qend})\n")

    # ── Earnings ──────────────────────────────────────────────────────────────
    lines.append("## Earnings\n")
    report_str = str(f.report_date) if f.report_date else "N/A"
    lines.append(f"**Report date:** {report_str}\n")

    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| EPS actual | {_fmt_dollar(f.eps_actual)} |")
    lines.append(f"| EPS estimate | {_fmt_dollar(f.eps_estimate)} |")
    lines.append(f"| EPS surprise | {_fmt_pct(f.eps_surprise_pct, signed=True)} |")
    lines.append(f"| Revenue | {_fmt_billions(f.revenue_actual_b)} |")
    lines.append(f"| Revenue vs estimate | {_fmt_pct(f.revenue_surprise_pct, signed=True)} |")
    lines.append(f"| Revenue YoY | {_fmt_pct(f.revenue_yoy_pct, signed=True)} |")

    if f.gross_margin_pct is not None and f.prior_gross_margin_pct is not None:
        gm_str = f"{f.gross_margin_pct:.1f}% (prior: {f.prior_gross_margin_pct:.1f}%)"
    else:
        gm_str = _fmt_pct(f.gross_margin_pct)
    lines.append(f"| Gross margin | {gm_str} |")

    guidance = f.guidance_direction or "not provided"
    if f.guidance_note:
        guidance += f" — {f.guidance_note}"
    lines.append(f"| Guidance | {guidance} |")

    # ── Valuation ─────────────────────────────────────────────────────────────
    lines.append("")
    lines.append("## Valuation\n")
    lines.append(f"_As of {qstart}_\n")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Price | {_fmt_dollar(f.price_at_start)} |")
    lines.append(f"| P/E trailing | {_fmt_x(f.pe_trailing)} |")
    lines.append(f"| P/E forward | {_fmt_x(f.pe_forward)} |")

    if f.fifty_two_high is not None and f.fifty_two_low is not None:
        range_str = f"${f.fifty_two_low:.2f} – ${f.fifty_two_high:.2f}"
    else:
        range_str = "N/A"
    lines.append(f"| 52-week range | {range_str} |")

    # ── Corporate actions ─────────────────────────────────────────────────────
    lines.append("")
    lines.append("## Corporate Actions\n")
    if f.corporate_actions:
        for action in f.corporate_actions:
            lines.append(f"- {action}")
    else:
        lines.append("- None identified")

    # ── Macro ─────────────────────────────────────────────────────────────────
    lines.append("")
    lines.append(f"## Macro — {m.sector.title()}\n")

    if m.series:
        lines.append("| Indicator | Start | End | Δ |")
        lines.append("|-----------|-------|-----|---|")
        for dp in m.series:
            start_val = f"{dp.value_start:.2f} {dp.unit}".strip() if dp.value_start is not None else "—"
            end_val = f"{dp.value_end:.2f} {dp.unit}".strip() if dp.value_end is not None else "—"
            if dp.value_start is not None and dp.value_end is not None:
                delta = dp.value_end - dp.value_start
                sign = "+" if delta >= 0 else ""
                delta_str = f"{sign}{delta:.2f} {dp.unit}".strip()
            else:
                delta_str = "—"
            lines.append(f"| {dp.label} | {start_val} | {end_val} | {delta_str} |")
    else:
        lines.append("_No macro series available._")

    lines.append("")
    if m.fomc_in_window and m.fomc_event:
        lines.append(f"**FOMC:** YES — {m.fomc_event.decision}")
    else:
        lines.append("**FOMC:** No meeting in window")

    # ── Data gaps ─────────────────────────────────────────────────────────────
    all_gaps = list(f.data_gaps) + list(m.data_gaps)
    if all_gaps:
        lines.append("")
        lines.append("## Data Gaps\n")
        for gap in all_gaps:
            lines.append(f"> {gap}")

    return "\n".join(lines)


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_dollar(v: float | None) -> str:
    return f"${v:.2f}" if v is not None else "N/A"


def _fmt_billions(v: float | None) -> str:
    return f"${v:.2f}B" if v is not None else "N/A"


def _fmt_pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "N/A"
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def _fmt_x(v: float | None) -> str:
    return f"{v:.1f}×" if v is not None else "N/A"
