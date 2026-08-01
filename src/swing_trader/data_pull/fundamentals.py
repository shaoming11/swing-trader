"""Fundamentals pull — yfinance (free, no API key required).

Pulls EPS, revenue, margins, valuation, corporate actions, and price data
for a given ticker and date window. No LLM involved anywhere in this module.

yfinance uses the `requests` library (synchronous, blocking I/O). To avoid
freezing the asyncio event loop — which would corrupt concurrent httpx
connections (e.g. FRED) — the actual network calls run inside a thread via
asyncio.to_thread. Cache hits bypass the thread entirely.
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import yfinance as yf

from swing_trader.data_pull import cache as disk_cache
from swing_trader.schemas.pipeline import FundamentalsResult

_CORPORATE_ACTION_KEYWORDS = {
    "buyback", "repurchase", "acquisition", "merger", "acquired",
    "dividend", "split", "ceo", "cfo", "launch", "recall", "settlement",
}


async def pull_fundamentals(
    ticker: str,
    window_start: date,
    window_end: date,
) -> FundamentalsResult:
    """Pull all fundamentals fields for the ticker within the date window.

    Cache hit → instant return.
    Cache miss → runs _fetch_from_yfinance in a thread pool so the event loop
    stays free for concurrent async work (e.g. FRED via httpx).
    """
    quarter = _date_to_quarter(window_start)
    cached = disk_cache.get_fundamentals(ticker, quarter)
    if cached:
        return FundamentalsResult.model_validate(cached)

    result = await asyncio.to_thread(_fetch_from_yfinance, ticker, window_start, window_end)
    disk_cache.set_fundamentals(ticker, quarter, result.model_dump(mode="json"))
    return result


def _fetch_from_yfinance(
    ticker: str,
    window_start: date,
    window_end: date,
) -> FundamentalsResult:
    yf_ticker = yf.Ticker(ticker)
    gaps: list[str] = []

    # ── Earnings (EPS actual vs estimate) ─────────────────────────────────
    eps_actual = eps_estimate = eps_surprise = None
    report_date = None
    try:
        earnings_hist = yf_ticker.earnings_history
        if earnings_hist is not None and not earnings_hist.empty:
            # Find earnings report closest to window (up to 90 days before start)
            lookback = window_start - timedelta(days=90)
            mask = (earnings_hist.index.date >= lookback) & (earnings_hist.index.date <= window_end)
            in_window = earnings_hist[mask]
            if not in_window.empty:
                row = in_window.iloc[-1]
                report_date = in_window.index[-1].date()
                eps_actual = _safe_float(row.get("epsActual"))
                eps_estimate = _safe_float(row.get("epsEstimate"))
                if eps_actual is not None and eps_estimate:
                    eps_surprise = (eps_actual - eps_estimate) / abs(eps_estimate) * 100
            else:
                gaps.append("No earnings report found within or before the window")
    except Exception as e:
        gaps.append(f"yfinance earnings_history: {e}")

    # ── Revenue + gross margin (quarterly financials) ──────────────────────
    rev_actual = rev_estimate = rev_surprise = rev_yoy = None
    gross_margin = prior_gross_margin = None
    try:
        qf = yf_ticker.quarterly_financials
        if qf is not None and not qf.empty:
            # Columns are dates (most recent first); find the one closest to report_date
            ref = report_date or window_start
            col = _closest_col(qf.columns, ref)
            if col is not None:
                total_rev = _df_val(qf, "Total Revenue", col)
                gross_profit = _df_val(qf, "Gross Profit", col)
                rev_actual = _to_billions(total_rev)
                if total_rev and gross_profit:
                    gross_margin = gross_profit / total_rev * 100

                # Prior quarter (offset -1)
                prior_col = _nth_col(qf.columns, col, offset=1)
                if prior_col is not None:
                    prior_rev = _df_val(qf, "Total Revenue", prior_col)
                    prior_gp = _df_val(qf, "Gross Profit", prior_col)
                    if prior_rev and prior_gp:
                        prior_gross_margin = prior_gp / prior_rev * 100

                # YoY: 4 quarters back
                yoy_col = _nth_col(qf.columns, col, offset=4)
                if yoy_col is not None:
                    yoy_rev = _df_val(qf, "Total Revenue", yoy_col)
                    if total_rev and yoy_rev:
                        rev_yoy = (total_rev - yoy_rev) / abs(yoy_rev) * 100
    except Exception as e:
        gaps.append(f"yfinance quarterly_financials: {e}")

    # ── Revenue estimate ───────────────────────────────────────────────────
    try:
        rev_est_df = yf_ticker.revenue_estimate
        if rev_est_df is not None and not rev_est_df.empty and "0q" in rev_est_df.index:
            avg = _safe_float(rev_est_df.loc["0q", "avg"])
            rev_estimate = _to_billions(avg)
            if rev_actual and rev_estimate:
                rev_surprise = (rev_actual - rev_estimate) / abs(rev_estimate) * 100
    except Exception as e:
        gaps.append(f"yfinance revenue_estimate: {e}")

    # ── P/E ratios ─────────────────────────────────────────────────────────
    pe_trailing = pe_forward = None
    try:
        info = yf_ticker.info
        pe_trailing = _safe_float(info.get("trailingPE"))
        pe_forward = _safe_float(info.get("forwardPE"))
    except Exception as e:
        gaps.append(f"yfinance info (PE): {e}")

    # ── Price data (history) ───────────────────────────────────────────────
    price_start = high_52 = low_52 = None
    try:
        cached_price = disk_cache.get_price(ticker, window_start, window_end)
        if cached_price:
            price_start = cached_price["start"]
            high_52 = cached_price["high_52"]
            low_52 = cached_price["low_52"]
        else:
            year_start = window_start - timedelta(days=365)
            hist = yf_ticker.history(
                start=str(year_start),
                end=str(window_end + timedelta(days=1)),
                auto_adjust=True,
            )
            if not hist.empty:
                all_closes = hist["Close"]
                high_52 = round(float(all_closes.max()), 2)
                low_52 = round(float(all_closes.min()), 2)
                # First close on or after window_start
                in_window = hist[hist.index.date >= window_start]
                price_start = round(float(in_window["Close"].iloc[0]), 2) if not in_window.empty else None
            disk_cache.set_price(ticker, window_start, window_end, {
                "start": price_start, "high_52": high_52, "low_52": low_52,
            })
    except Exception as e:
        gaps.append(f"yfinance price history: {e}")

    # ── Corporate actions (from news) ──────────────────────────────────────
    corporate_actions: list[str] = []
    try:
        news = yf_ticker.news or []
        for article in news:
            title = (article.get("title") or "").lower()
            content = (article.get("summary") or "").lower()
            combined = title + " " + content
            if any(kw in combined for kw in _CORPORATE_ACTION_KEYWORDS):
                corporate_actions.append(article.get("title", "").strip())
    except Exception as e:
        gaps.append(f"yfinance news (corporate actions): {e}")

    # ── Forward guidance (proxy: EPS estimate trend) ───────────────────────
    guidance_direction = "not provided"
    guidance_note = ""
    try:
        eps_est_df = yf_ticker.earnings_estimate
        if eps_est_df is not None and not eps_est_df.empty:
            rows = [r for r in ["0q", "+1q"] if r in eps_est_df.index]
            if len(rows) >= 2:
                cur_eps = _safe_float(eps_est_df.loc[rows[0], "avg"])
                nxt_eps = _safe_float(eps_est_df.loc[rows[1], "avg"])
                if cur_eps and nxt_eps:
                    if nxt_eps > cur_eps * 1.02:
                        guidance_direction = "raised"
                    elif nxt_eps < cur_eps * 0.98:
                        guidance_direction = "lowered"
                    else:
                        guidance_direction = "maintained"
                    guidance_note = f"Forward EPS estimate: ${cur_eps:.2f}"
    except Exception as e:
        gaps.append(f"yfinance earnings_estimate (guidance): {e}")

    return FundamentalsResult(
        ticker=ticker,
        report_date=report_date,
        eps_actual=eps_actual,
        eps_estimate=eps_estimate,
        eps_surprise_pct=round(eps_surprise, 1) if eps_surprise is not None else None,
        revenue_actual_b=round(rev_actual, 2) if rev_actual is not None else None,
        revenue_estimate_b=round(rev_estimate, 2) if rev_estimate is not None else None,
        revenue_surprise_pct=round(rev_surprise, 1) if rev_surprise is not None else None,
        revenue_yoy_pct=round(rev_yoy, 1) if rev_yoy is not None else None,
        gross_margin_pct=round(gross_margin, 1) if gross_margin is not None else None,
        prior_gross_margin_pct=round(prior_gross_margin, 1) if prior_gross_margin is not None else None,
        guidance_direction=guidance_direction,
        guidance_note=guidance_note,
        pe_trailing=round(pe_trailing, 1) if pe_trailing else None,
        pe_forward=round(pe_forward, 1) if pe_forward else None,
        price_at_start=price_start,
        fifty_two_high=high_52,
        fifty_two_low=low_52,
        corporate_actions=corporate_actions[:5],
        data_gaps=gaps,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_float(val) -> float | None:
    try:
        f = float(val)
        return None if f != f else f  # NaN check
    except (TypeError, ValueError):
        return None


def _to_billions(val: float | None) -> float | None:
    return round(val / 1e9, 3) if val else None


def _date_to_quarter(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


def _closest_col(columns, ref: date):
    """Return the column (Timestamp) closest to ref date."""
    best = None
    best_delta = float("inf")
    for col in columns:
        try:
            delta = abs((col.date() - ref).days)
            if delta < best_delta:
                best_delta = delta
                best = col
        except Exception:
            continue
    return best if best_delta < 120 else None


def _nth_col(columns, ref_col, offset: int):
    """Return the column that is `offset` positions after ref_col (older = higher index)."""
    cols = list(columns)
    try:
        idx = cols.index(ref_col)
        target = idx + offset
        return cols[target] if 0 <= target < len(cols) else None
    except ValueError:
        return None


def _df_val(df, row_label: str, col):
    """Safely extract a scalar value from a DataFrame by row label and column."""
    try:
        val = df.loc[row_label, col]
        return _safe_float(val)
    except (KeyError, TypeError):
        return None
