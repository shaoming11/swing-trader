"""RAG corpus indexer.

Converts markdown corpus files into embedded, filterable chunks in the vector store.
Run after each corpus generator run to index new files incrementally.

Usage:
    from swing_trader.rag.indexer import index_new_files
    await index_new_files()
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import frontmatter
from langchain_text_splitters import RecursiveCharacterTextSplitter

from swing_trader.clients import EMBED_MODEL, get_embed_client
from swing_trader.rag.store import get_vector_store

_CORPUS_ROOT = Path(os.getenv("CORPUS_DIR", "corpus"))
_CHUNK_SIZE = 400       # tokens ≈ characters * 0.75; using chars here
_CHUNK_OVERLAP = 50
_MIN_CHUNK_CHARS = 100  # discard chunks shorter than this

_splitter = RecursiveCharacterTextSplitter(
    chunk_size=int(_CHUNK_SIZE * 4),    # rough char estimate: 1 token ≈ 4 chars
    chunk_overlap=int(_CHUNK_OVERLAP * 4),
    length_function=len,
    separators=["\n\n", "\n", ". ", " ", ""],
)


async def index_new_files(corpus_root: Path | None = None) -> int:
    """Index all corpus files not yet in the vector store.

    Returns the number of new files indexed.
    """
    root = corpus_root or _CORPUS_ROOT
    store = get_vector_store()

    md_files = list(root.rglob("*.md"))
    # Never index rejected files
    md_files = [f for f in md_files if "_rejected" not in str(f)]

    indexed = 0
    for filepath in md_files:
        if store.file_is_indexed(str(filepath)):
            continue
        try:
            await _index_file(filepath, store)
            indexed += 1
        except Exception as e:
            print(f"[indexer] failed to index {filepath}: {e}")

    return indexed


async def reindex_file(filepath: Path) -> None:
    """Re-index a single file — deletes old chunks first, then re-embeds."""
    store = get_vector_store()
    store.delete_by_file_path(str(filepath))
    await _index_file(filepath, store)


async def _index_file(filepath: Path, store) -> None:
    post = frontmatter.load(filepath)
    meta = dict(post.metadata)
    body = post.content.strip()

    if not body:
        return

    chunks = _splitter.split_text(body)
    chunks = [c for c in chunks if len(c) >= _MIN_CHUNK_CHARS]

    if not chunks:
        return

    embeddings = await _embed_batch(chunks)

    records = []
    for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
        tickers = _normalize_tickers(meta.get("tickers", []))
        relevance_tags = meta.get("relevance_tags", [])
        records.append({
            "id": _chunk_id(str(filepath), i),
            "embedding": embedding,
            "content": chunk,
            "metadata": {
                "file_path": str(filepath),
                "chunk_index": i,
                "date": str(meta.get("date", "")),
                "tickers": tickers if tickers else ["MACRO"],
                "source_type": meta.get("source_type", "news"),
                "source": meta.get("source", ""),
                "relevance_tags": relevance_tags if relevance_tags else ["untagged"],
                "sentiment_label": meta.get("sentiment_label", "") or "neutral",
                "sentiment_reason": meta.get("sentiment_reason", "") or "",
                "url": meta.get("url", ""),
                "active": True,
            },
        })

    store.upsert(records)


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts via the configured embedding client."""
    client = get_embed_client()
    response = await client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in response.data]


def _chunk_id(file_path: str, chunk_index: int) -> str:
    raw = f"{file_path}:{chunk_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _normalize_tickers(tickers) -> list[str]:
    if isinstance(tickers, str):
        return [t.strip().upper() for t in tickers.split(",")]
    return [str(t).strip().upper() for t in (tickers or [])]
