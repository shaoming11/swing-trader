"""Source pullers — one async function per data source.

Each function returns a list of RawArticle dicts with keys:
    headline, body, url, date (YYYY-MM-DD), tickers, source, source_type, extra_meta

Sources:
    Polygon.io   — news articles (ticker-specific)
    GDELT        — geopolitics / macro-adjacent news (free, no key)
    FMP          — analyst ratings + upgrades/downgrades
    FRED         — macro indicators as corpus entries
    StockTwits   — social sentiment (unauthenticated, recent only)
"""

from __future__ import annotations

import asyncio
import os
import urllib.parse
from datetime import date
from typing import Any

import httpx

_POLYGON_BASE = "https://api.polygon.io"
_GDELT_BASE = "https://api.gdeltproject.org/api/v2"
_FMP_BASE = "https://financialmodelingprep.com/api/v3"
_STOCKTWITS_BASE = "https://api.stocktwits.com/api/2"
_FRED_BASE = "https://api.stlouisfed.org/fred"

_TIMEOUT = httpx.Timeout(20.0)
_RETRY_DELAYS = [1.0, 3.0]  # seconds between retries (2 retries total)

RawArticle = dict[str, Any]


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """GET with simple retry on transient errors."""
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0.0] + _RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


# ── Polygon.io News ────────────────────────────────────────────────────────────


async def pull_polygon_news(
    ticker: str,
    window_start: date,
    window_end: date,
) -> list[RawArticle]:
    """Ticker-specific news from Polygon.io — returns title + description."""
    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        return []

    articles: list[RawArticle] = []
    cursor: str | None = None

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while len(articles) < 200:
            params: dict[str, Any] = {
                "ticker": ticker.upper(),
                "published_utc.gte": f"{window_start}T00:00:00Z",
                "published_utc.lte": f"{window_end}T23:59:59Z",
                "limit": 100,
                "order": "desc",
                "apiKey": api_key,
            }
            if cursor:
                params["cursor"] = cursor

            try:
                data = await _get(client, f"{_POLYGON_BASE}/v2/reference/news", params)
            except Exception:
                break

            for item in data.get("results", []):
                pub_date = (item.get("published_utc") or "")[:10]
                description = (item.get("description") or "").strip()
                publisher = (item.get("publisher") or {}).get("name", "Polygon")

                # Body: description + keywords context
                keywords = item.get("keywords") or []
                body_parts = []
                if description:
                    body_parts.append(description)
                if keywords:
                    body_parts.append(f"Keywords: {', '.join(keywords[:8])}")
                body_parts.append(f"Published by {publisher}.")
                body = "\n\n".join(body_parts)

                articles.append(
                    {
                        "headline": item.get("title", ""),
                        "body": body,
                        "url": item.get("article_url", ""),
                        "date": pub_date,
                        "tickers": [ticker.upper()],
                        "source": publisher,
                        "source_type": "news",
                        "extra_meta": {},
                    }
                )

            next_url = data.get("next_url") or ""
            if not next_url:
                break
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(next_url).query)
            cursor = (qs.get("cursor") or [None])[0]
            if not cursor:
                break

    return articles


# ── GDELT ──────────────────────────────────────────────────────────────────────


async def pull_gdelt_news(
    ticker: str,
    company_name: str,
    window_start: date,
    window_end: date,
) -> list[RawArticle]:
    """Geopolitics and macro-adjacent news from GDELT (free, no API key)."""
    start_str = window_start.strftime("%Y%m%d") + "000000"
    end_str = window_end.strftime("%Y%m%d") + "235959"
    query = f'"{ticker}" OR "{company_name}"'

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": 25,
        "startdatetime": start_str,
        "enddatetime": end_str,
        "format": "json",
        "sourcelang": "english",
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            data = await _get(client, f"{_GDELT_BASE}/doc/doc", params)
        except Exception:
            return []

    articles = []
    for item in data.get("articles", []):
        seen = item.get("seendate", "")
        if len(seen) >= 8:
            art_date = f"{seen[:4]}-{seen[4:6]}-{seen[6:8]}"
        else:
            art_date = str(window_start)

        title = item.get("title", "").strip()
        url = item.get("url", "")
        domain = item.get("domain", "unknown source")
        country = item.get("sourcecountry", "")

        body_parts = [
            "**GDELT Indexed Article**",
            "",
            title,
            "",
            f"Source domain: {domain}",
        ]
        if country:
            body_parts.append(f"Source country: {country}")
        body_parts.append(f"Published: {art_date}")
        body_parts.append(f"Full article: {url}")
        body_parts.append("Indexed by the GDELT Project — global events and geopolitics monitor.")

        articles.append(
            {
                "headline": title,
                "body": "\n".join(body_parts),
                "url": url,
                "date": art_date,
                "tickers": [ticker.upper()],
                "source": "GDELT",
                "source_type": "news",
                "extra_meta": {},
            }
        )

    return articles


# ── Analyst Ratings (yfinance) ─────────────────────────────────────────────────


async def pull_analyst(
    ticker: str,
    window_start: date,
    window_end: date,
) -> list[RawArticle]:
    """Analyst upgrades/downgrades from yfinance (replaces deprecated FMP endpoints)."""
    import yfinance as yf

    ticker_up = ticker.upper()

    def _fetch() -> list[RawArticle]:
        t = yf.Ticker(ticker_up)
        articles: list[RawArticle] = []

        # ── Upgrades / Downgrades ──────────────────────────────────────────
        ud = t.upgrades_downgrades
        if ud is not None and not ud.empty:
            for grade_date, row in ud.iterrows():
                try:
                    ud_date = str(grade_date)[:10]
                except Exception:
                    continue
                if not (str(window_start) <= ud_date <= str(window_end)):
                    continue

                firm = row.get("Firm", "Unknown firm")
                action = row.get("Action", "rating change")
                to_grade = row.get("ToGrade", "")
                from_grade = row.get("FromGrade", "")
                pt_action = row.get("priceTargetAction", "")
                current_pt = row.get("currentPriceTarget")
                prior_pt = row.get("priorPriceTarget")

                body_parts = [
                    f"**{firm}** — **{action}** on {ticker_up}",
                    "",
                ]
                if from_grade and to_grade:
                    body_parts.append(f"Rating change: {from_grade} -> {to_grade}")
                elif to_grade:
                    body_parts.append(f"New rating: {to_grade}")
                if current_pt and current_pt > 0:
                    body_parts.append(f"Price target: ${current_pt:.0f}")
                    if prior_pt and prior_pt > 0:
                        body_parts.append(f"Prior target: ${prior_pt:.0f}")
                    if pt_action:
                        body_parts.append(f"Target action: {pt_action}")
                body_parts.append("Source: yfinance analyst upgrades/downgrades.")

                articles.append(
                    {
                        "headline": f"{firm}: {action} {ticker_up}"
                        + (f" — {from_grade}->{to_grade}" if from_grade and to_grade else ""),
                        "body": "\n".join(body_parts),
                        "url": "",
                        "date": ud_date,
                        "tickers": [ticker_up],
                        "source": firm,
                        "source_type": "analyst",
                        "extra_meta": {},
                    }
                )

        # ── Consensus recommendations (last 4 months) ─────────────────────
        recs = t.recommendations
        if recs is not None and not recs.empty:
            for _, row in recs.iterrows():
                period = row.get("period", "")
                strong_buy = int(row.get("strongBuy", 0) or 0)
                buy = int(row.get("buy", 0) or 0)
                hold = int(row.get("hold", 0) or 0)
                sell = int(row.get("sell", 0) or 0)
                strong_sell = int(row.get("strongSell", 0) or 0)

                body_parts = [
                    f"**Analyst Consensus — {ticker_up} — period {period}**",
                    "",
                    f"Strong Buy: {strong_buy} | Buy: {buy} | Hold: {hold} | Sell: {sell} | Strong Sell: {strong_sell}",
                    "Source: yfinance analyst recommendations.",
                ]
                articles.append(
                    {
                        "headline": f"Analyst consensus {ticker_up}: {buy} buy / {hold} hold / {sell} sell (period {period})",
                        "body": "\n".join(body_parts),
                        "url": "",
                        "date": str(date.today()),
                        "tickers": [ticker_up],
                        "source": "yfinance",
                        "source_type": "analyst",
                        "extra_meta": {},
                    }
                )

        return articles

    return await asyncio.to_thread(_fetch)


# Keep old name as alias for backward compatibility in generator.py
pull_fmp_analyst = pull_analyst


# ── FRED Macro Corpus ──────────────────────────────────────────────────────────

_FRED_SERIES = [
    ("CPIAUCSL", "CPI (Consumer Price Index)"),
    ("FEDFUNDS", "Fed Funds Rate"),
    ("UNRATE", "Unemployment Rate"),
    ("GDP", "GDP"),
    ("DGS10", "10-Year Treasury Yield"),
    ("T10YIE", "10-Year Breakeven Inflation Rate"),
]


async def pull_fred_macro_corpus(
    window_start: date,
    window_end: date,
) -> list[RawArticle]:
    """Macro indicator releases from FRED — formatted as corpus entries."""
    api_key = os.getenv("FRED_API_KEY", "")
    if not api_key:
        return []

    articles: list[RawArticle] = []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for series_id, label in _FRED_SERIES:
            try:
                data = await _get(
                    client,
                    f"{_FRED_BASE}/series/observations",
                    {
                        "series_id": series_id,
                        "observation_start": str(window_start),
                        "observation_end": str(window_end),
                        "api_key": api_key,
                        "file_type": "json",
                        "sort_order": "asc",
                        "limit": "10",
                    },
                )
            except Exception:
                continue

            obs = [
                o for o in (data.get("observations") or []) if o.get("value") not in (".", None, "")
            ]
            if not obs:
                continue

            latest = obs[-1]
            prior = obs[0] if len(obs) > 1 else None
            obs_date = latest.get("date", str(window_start))
            value = latest.get("value", "N/A")

            body_parts = [
                f"**Macro Indicator: {label}**",
                "",
                f"Latest value: {value} (as of {obs_date})",
            ]
            if prior and prior["date"] != obs_date:
                body_parts.append(f"Prior reading: {prior['value']} (as of {prior['date']})")
            body_parts.append(f"Period: {window_start} to {window_end}")
            body_parts.append("")
            body_parts.append(
                f"Series: {series_id} — Federal Reserve Economic Data (FRED), St. Louis Fed."
            )

            articles.append(
                {
                    "headline": f"Macro: {label} at {value} ({obs_date})",
                    "body": "\n".join(body_parts),
                    "url": f"https://fred.stlouisfed.org/series/{series_id}",
                    "date": obs_date,
                    "tickers": [],
                    "source": "FRED",
                    "source_type": "macro",
                    "extra_meta": {"series_id": series_id},
                }
            )

    return articles


# ── StockTwits ─────────────────────────────────────────────────────────────────


async def pull_stocktwits(
    ticker: str,
    window_start: date,
    window_end: date,
) -> list[RawArticle]:
    """StockTwits social sentiment — unauthenticated API, recent data only.

    The unauthenticated endpoint returns only the latest ~30 messages.
    Useful for live_run (yesterday's data); skip for old backfill windows.
    """
    from datetime import datetime, timezone

    cutoff = datetime.now(timezone.utc).date()
    # Skip if window is older than 7 days — unauthenticated API won't have the data
    if window_end < cutoff.replace(day=max(1, cutoff.day - 7)):
        return []

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            data = await _get(
                client,
                f"{_STOCKTWITS_BASE}/streams/symbol/{ticker.upper()}.json",
                {},
            )
        except Exception:
            return []

    messages = data.get("messages") or []

    # Aggregate into daily buckets
    from collections import defaultdict

    daily: dict[str, list[dict]] = defaultdict(list)
    for msg in messages:
        created = str(msg.get("created_at", ""))[:10]
        if created and str(window_start) <= created <= str(window_end):
            daily[created].append(msg)

    articles = []
    for day, msgs in sorted(daily.items()):
        bullish = sum(
            1
            for m in msgs
            if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bullish"
        )
        bearish = sum(
            1
            for m in msgs
            if ((m.get("entities") or {}).get("sentiment") or {}).get("basic") == "Bearish"
        )
        total = len(msgs)

        top_texts = [m.get("body", "").strip() for m in msgs[:5] if m.get("body")]

        body_parts = [
            f"**StockTwits Sentiment Summary — {ticker.upper()} — {day}**",
            "",
            f"Total messages: {total} | Bullish: {bullish} | Bearish: {bearish} | Neutral: {total - bullish - bearish}",
            "",
            "Top messages:",
        ]
        for text in top_texts:
            body_parts.append(f"- {text}")
        body_parts.append("")
        body_parts.append(f"Source: StockTwits social sentiment stream for {ticker.upper()}.")

        articles.append(
            {
                "headline": f"StockTwits {ticker.upper()} {day}: {bullish} bullish / {bearish} bearish / {total} total",
                "body": "\n".join(body_parts),
                "url": f"https://stocktwits.com/symbol/{ticker.upper()}",
                "date": day,
                "tickers": [ticker.upper()],
                "source": "StockTwits",
                "source_type": "social",
                "extra_meta": {
                    "bullish_count": bullish,
                    "bearish_count": bearish,
                    "total_messages": total,
                },
            }
        )

    return articles
