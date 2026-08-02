"""LLM tagging pass — adds relevance_tags, sentiment_label, sentiment_reason.

Run as a batch job after raw pulls (not inline during pull) to keep cost isolated.
Batches up to 20 articles per LLM call.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter

from swing_trader.clients import LLM_JUDGE_MODEL, get_judge_client

_BATCH_SIZE = 20
_CORPUS_ROOT = Path("corpus")

_VALID_TAGS = frozenset(
    {
        "earnings",
        "macro",
        "sentiment",
        "technical",
        "geopolitics",
        "corporate_action",
        "analyst_rating",
    }
)


async def tag_untagged_files(corpus_root: Path | None = None) -> int:
    """Tag all corpus files missing relevance_tags. Returns count of files tagged."""
    root = corpus_root or _CORPUS_ROOT
    untagged = _find_untagged(root)
    if not untagged:
        return 0

    tagged = 0
    for i in range(0, len(untagged), _BATCH_SIZE):
        batch = untagged[i : i + _BATCH_SIZE]
        await _tag_batch(batch)
        tagged += len(batch)

    return tagged


def _find_untagged(root: Path) -> list[Path]:
    result = []
    for fp in root.rglob("*.md"):
        if "_rejected" in fp.parts:
            continue
        try:
            post = frontmatter.load(fp)
            if not post.get("relevance_tags"):
                result.append(fp)
        except Exception:
            continue
    return result


async def _tag_batch(filepaths: list[Path]) -> None:
    items = []
    for fp in filepaths:
        try:
            post = frontmatter.load(fp)
            ticker = (post.get("tickers") or [""])[0]
            body = post.content[:600]
        except Exception:
            ticker, body = "", ""
        items.append({"ticker": ticker, "text": body, "path": str(fp)})

    texts = [{"ticker": it["ticker"], "text": it["text"]} for it in items]

    prompt = (
        "You are tagging financial news items for a swing trading RAG corpus.\n\n"
        "For each item return exactly:\n"
        '{"relevance_tags": [...], "sentiment_label": "bullish|bearish|neutral", "sentiment_reason": "one sentence"}\n\n'
        f"Valid relevance_tags values: {sorted(_VALID_TAGS)}\n"
        "Set relevance_tags to [] if the item is not meaningfully about the ticker.\n\n"
        f"Items:\n{json.dumps(texts, ensure_ascii=False)}\n\n"
        "Return a JSON array in the same order, one object per item."
    )

    try:
        client = get_judge_client()
        response = await client.chat.completions.create(
            model=LLM_JUDGE_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.choices[0].message.content.strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        tags_list = json.loads(raw[start:end])
    except Exception:
        tags_list = [
            {"relevance_tags": [], "sentiment_label": "neutral", "sentiment_reason": ""}
        ] * len(items)

    for item, tags in zip(items, tags_list):
        try:
            fp = Path(item["path"])
            post = frontmatter.load(fp)
            post["relevance_tags"] = [
                t for t in (tags.get("relevance_tags") or []) if t in _VALID_TAGS
            ]
            post["sentiment_label"] = tags.get("sentiment_label", "neutral")
            post["sentiment_reason"] = tags.get("sentiment_reason", "")
            fp.write_text(frontmatter.dumps(post))
        except Exception:
            continue
