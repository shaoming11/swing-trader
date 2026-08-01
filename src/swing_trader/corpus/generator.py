"""Corpus generator — orchestrates all source pulls and writes corpus files.

Two modes:
    backfill(tickers, start_date, end_date)  — historical build, quarter by quarter
    live_run(tickers, target_date)           — daily job for yesterday's content

Usage:
    import asyncio
    from swing_trader.corpus.generator import backfill, live_run

    # Build historical corpus
    asyncio.run(backfill(["AAPL", "MSFT"], "2023-01-01", "2024-12-31"))

    # Daily live run (defaults to yesterday)
    asyncio.run(live_run(["AAPL", "MSFT"]))
"""
from __future__ import annotations

import asyncio
import calendar
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from swing_trader.corpus.sources import (
    pull_fmp_analyst,
    pull_fred_macro_corpus,
    pull_gdelt_news,
    pull_polygon_news,
    pull_stocktwits,
)
from swing_trader.corpus.tagger import tag_untagged_files
from swing_trader.corpus.writer import write_corpus_file

_CORPUS_ROOT = Path(os.getenv("CORPUS_DIR", "corpus"))
_MANIFEST_PATH = _CORPUS_ROOT / "backfill_manifest.json"
_INTER_TICKER_DELAY = 0.5  # seconds between tickers — be polite to APIs


def _load_manifest() -> dict:
    if _MANIFEST_PATH.exists():
        try:
            return json.loads(_MANIFEST_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_manifest(manifest: dict) -> None:
    _MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str))


def _quarter_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] into non-overlapping calendar quarter windows."""
    windows = []
    # Align to quarter start
    q_start_month = ((start.month - 1) // 3) * 3 + 1
    current = date(start.year, q_start_month, 1)

    while current <= end:
        # Last month of this quarter
        q_last_month = current.month + 2
        q_year = current.year
        if q_last_month > 12:
            q_last_month -= 12
            q_year += 1
        q_end_day = calendar.monthrange(q_year, q_last_month)[1]
        q_end = date(q_year, q_last_month, q_end_day)

        windows.append((max(current, start), min(q_end, end)))

        # Advance to next quarter
        next_month = current.month + 3
        if next_month > 12:
            current = date(current.year + 1, next_month - 12, 1)
        else:
            current = date(current.year, next_month, 1)

    return windows


async def _write_articles(articles: list[dict], corpus_root: Path) -> int:
    written = 0
    for article in articles:
        path = write_corpus_file(
            source_type=article["source_type"],
            source=article["source"],
            article_date=article["date"],
            tickers=article.get("tickers") or [],
            headline=article["headline"],
            body=article["body"],
            url=article.get("url", ""),
            extra_meta=article.get("extra_meta"),
            corpus_root=corpus_root,
        )
        if path is not None and "_rejected" not in path.parts:
            written += 1
    return written


async def _pull_ticker(
    ticker: str,
    window_start: date,
    window_end: date,
    corpus_root: Path,
) -> int:
    """Pull all ticker-specific sources in parallel and write files. Returns count written."""
    results = await asyncio.gather(
        pull_polygon_news(ticker, window_start, window_end),
        pull_gdelt_news(ticker, ticker, window_start, window_end),
        pull_fmp_analyst(ticker, window_start, window_end),
        pull_stocktwits(ticker, window_start, window_end),
        return_exceptions=True,
    )

    all_articles: list[dict] = []
    for r in results:
        if isinstance(r, list):
            all_articles.extend(r)
        # swallow individual source failures

    return await _write_articles(all_articles, corpus_root)


async def _pull_macro(window_start: date, window_end: date, corpus_root: Path) -> int:
    """Pull FRED macro data and write files."""
    articles = await pull_fred_macro_corpus(window_start, window_end)
    return await _write_articles(articles, corpus_root)


async def backfill(
    tickers: Sequence[str],
    start_date: str | date,
    end_date: str | date,
    run_tagger: bool = True,
    corpus_root: Path | None = None,
) -> dict:
    """Build the historical corpus for a set of tickers over a date range.

    Iterates quarter by quarter; skips already-completed ticker/quarter combos
    (idempotent — safe to re-run after partial failures).

    Args:
        tickers: Stock tickers to pull, e.g. ["AAPL", "MSFT"].
        start_date: Inclusive start date (str "YYYY-MM-DD" or date object).
        end_date: Inclusive end date.
        run_tagger: Run the LLM tagging pass after all files are written.
        corpus_root: Override corpus directory (default: $CORPUS_DIR or "corpus/").

    Returns:
        Summary dict with written/skipped counts.
    """
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)
    if isinstance(end_date, str):
        end_date = date.fromisoformat(end_date)

    root = corpus_root or _CORPUS_ROOT
    manifest = _load_manifest()
    quarters = _quarter_windows(start_date, end_date)
    total_written = 0
    total_skipped = 0

    for q_start, q_end in quarters:
        q_key = f"{q_start}_{q_end}"

        # Macro: one pull per quarter (shared across tickers)
        macro_key = f"macro_{q_key}"
        if macro_key not in manifest:
            written = await _pull_macro(q_start, q_end, root)
            total_written += written
            manifest[macro_key] = {"status": "done", "written": written}
            _save_manifest(manifest)
        else:
            total_skipped += 1

        for ticker in tickers:
            tk_key = f"{ticker.upper()}_{q_key}"
            if tk_key in manifest:
                total_skipped += 1
                continue

            print(f"[corpus] backfill {ticker.upper()}  {q_start} → {q_end}")
            written = await _pull_ticker(ticker, q_start, q_end, root)
            total_written += written
            manifest[tk_key] = {"status": "done", "written": written}
            _save_manifest(manifest)

            await asyncio.sleep(_INTER_TICKER_DELAY)

    if run_tagger:
        print("[corpus] running LLM tagger pass...")
        tagged = await tag_untagged_files(root)
        print(f"[corpus] tagged {tagged} files")

    print(f"[corpus] backfill complete — wrote {total_written}, skipped {total_skipped}")
    return {"written": total_written, "skipped": total_skipped}


async def live_run(
    tickers: Sequence[str],
    target_date: date | None = None,
    run_tagger: bool = True,
    corpus_root: Path | None = None,
) -> dict:
    """Pull content for a single date (defaults to yesterday) for all tracked tickers.

    Designed to run as a daily scheduled job at ~06:00 UTC.
    On source failure: logs and continues — missing one day is recoverable.

    Args:
        tickers: Stock tickers to pull.
        target_date: Date to pull for. Defaults to yesterday (UTC).
        run_tagger: Run the LLM tagging pass after writing.
        corpus_root: Override corpus directory.

    Returns:
        Summary dict with date and written count.
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc).date() - timedelta(days=1)

    root = corpus_root or _CORPUS_ROOT
    total_written = 0

    # Macro
    try:
        written = await _pull_macro(target_date, target_date, root)
        total_written += written
    except Exception as exc:
        print(f"[corpus] live_run macro error: {exc}")

    # Tickers
    for ticker in tickers:
        print(f"[corpus] live_run {ticker.upper()} {target_date}")
        try:
            written = await _pull_ticker(ticker, target_date, target_date, root)
            total_written += written
        except Exception as exc:
            print(f"[corpus] live_run {ticker} error: {exc}")
        await asyncio.sleep(_INTER_TICKER_DELAY)

    if run_tagger:
        try:
            tagged = await tag_untagged_files(root)
            print(f"[corpus] tagged {tagged} files")
        except Exception as exc:
            print(f"[corpus] tagger error: {exc}")

    print(f"[corpus] live_run {target_date} complete — wrote {total_written}")
    return {"date": str(target_date), "written": total_written}
