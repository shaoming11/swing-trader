"""Corpus file writer — markdown files with YAML frontmatter.

Handles:
- File naming: {date}_{ticker}_{slug}.md
- Deduplication by URL (or content hash for URL-less items)
- Quality check: reject files below minimum word count
- Rejected files written to corpus/_rejected/ for audit
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import frontmatter

_CORPUS_ROOT = Path("corpus")
_URL_INDEX_PATH = _CORPUS_ROOT / ".url_index.json"

# Source-type minimums — descriptions/summaries are shorter than full articles
_MIN_WORDS: dict[str, int] = {
    "news":     20,
    "analyst":  8,
    "macro":    10,
    "social":   15,
}
_MIN_WORDS_DEFAULT = 20


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(text: str) -> str:
    s = _HTMLStripper()
    s.feed(text)
    return " ".join(s.parts)


def _slugify(text: str, n_words: int = 5) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "-".join(words[:n_words]) or "untitled"


def _dedup_key(url: str, date_str: str, ticker: str, headline: str) -> str:
    if url:
        return url
    raw = f"{date_str}|{ticker}|{headline}"
    return "nk:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _load_url_index() -> set[str]:
    if _URL_INDEX_PATH.exists():
        try:
            return set(json.loads(_URL_INDEX_PATH.read_text()))
        except Exception:
            return set()
    return set()


def _save_url_index(index: set[str]) -> None:
    _URL_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    _URL_INDEX_PATH.write_text(json.dumps(sorted(index)))


def write_corpus_file(
    *,
    source_type: str,
    source: str,
    article_date: date | str,
    tickers: list[str],
    headline: str,
    body: str,
    url: str = "",
    relevance_tags: list[str] | None = None,
    sentiment_label: str = "",
    sentiment_reason: str = "",
    extra_meta: dict[str, Any] | None = None,
    corpus_root: Path | None = None,
) -> Path | None:
    """Write one corpus markdown file. Returns the path written, or None if skipped (duplicate)."""
    root = corpus_root or _CORPUS_ROOT
    date_str = str(article_date)
    ticker_upper = [t.upper() for t in tickers] if tickers else []
    ticker_prefix = ticker_upper[0] if ticker_upper else "MACRO"

    # Dedup check
    dedup_key = _dedup_key(url, date_str, ticker_prefix, headline)
    url_index = _load_url_index()
    if dedup_key in url_index:
        return None

    body = _strip_html(body).strip()

    # Quality check
    min_words = _MIN_WORDS.get(source_type, _MIN_WORDS_DEFAULT)
    if len(body.split()) < min_words:
        return _write_rejected(root, source_type, date_str, ticker_upper, headline, body, url)

    slug = _slugify(headline)
    meta: dict[str, Any] = {
        "date": date_str,
        "tickers": ticker_upper,
        "source_type": source_type,
        "source": source,
        "relevance_tags": relevance_tags or [],
        "sentiment_label": sentiment_label,
        "sentiment_reason": sentiment_reason,
        "url": url,
    }
    if extra_meta:
        meta.update(extra_meta)

    outdir = root / source_type
    outdir.mkdir(parents=True, exist_ok=True)
    filepath = outdir / f"{date_str}_{ticker_prefix}_{slug}.md"

    # Resolve collisions
    if filepath.exists():
        i = 1
        while filepath.exists():
            filepath = outdir / f"{date_str}_{ticker_prefix}_{slug}-{i}.md"
            i += 1

    post = frontmatter.Post(f"# {headline}\n\n{body}", **meta)
    filepath.write_text(frontmatter.dumps(post))

    # Record in dedup index
    url_index.add(dedup_key)
    _save_url_index(url_index)

    return filepath


def _write_rejected(
    root: Path,
    source_type: str,
    date_str: str,
    tickers: list[str],
    headline: str,
    body: str,
    url: str,
) -> Path:
    rejected_dir = root / "_rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    ticker_prefix = tickers[0] if tickers else "MACRO"
    slug = _slugify(headline)
    filepath = rejected_dir / f"{date_str}_{ticker_prefix}_{slug}.md"
    meta = {
        "date": date_str,
        "tickers": tickers,
        "source_type": source_type,
        "source": "",
        "rejection_reason": f"body < {_MIN_WORDS.get(source_type, _MIN_WORDS_DEFAULT)} words",
        "url": url,
    }
    post = frontmatter.Post(f"# {headline}\n\n{body}", **meta)
    filepath.write_text(frontmatter.dumps(post))
    return filepath
