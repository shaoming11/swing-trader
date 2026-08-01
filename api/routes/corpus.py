"""Corpus management routes.

POST  /corpus/backfill  — trigger corpus backfill for a list of tickers
POST  /corpus/index     — trigger RAG indexer on existing corpus files
GET   /corpus/status    — current corpus stats (file counts, index status)
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter()

_CORPUS_ROOT = Path("corpus")


class BackfillRequest(BaseModel):
    tickers: list[str]
    start_date: date
    end_date: date
    run_tagger: bool = True
    run_indexer: bool = True


@router.post("/backfill")
async def backfill_corpus(req: BackfillRequest):
    """Stream corpus backfill progress via SSE."""

    async def event_stream() -> AsyncIterator[str]:
        from swing_trader.corpus.generator import backfill
        from swing_trader.rag.indexer import index_new_files

        tickers = [t.strip().upper() for t in req.tickers if t.strip()]

        yield _sse({"event": "start", "tickers": tickers, "ts": time.time()})

        try:
            result = await backfill(
                tickers=tickers,
                start_date=req.start_date,
                end_date=req.end_date,
                run_tagger=req.run_tagger,
                corpus_root=_CORPUS_ROOT,
            )
            yield _sse({
                "event": "backfill_done",
                "written": result["written"],
                "skipped": result["skipped"],
                "ts": time.time(),
            })
        except Exception as exc:
            yield _sse({"event": "error", "message": str(exc), "ts": time.time()})
            yield _sse({"event": "done", "ts": time.time()})
            return

        if req.run_indexer:
            yield _sse({"event": "indexing_start", "ts": time.time()})
            try:
                indexed = await index_new_files(corpus_root=_CORPUS_ROOT)
                yield _sse({
                    "event": "indexing_done",
                    "indexed": indexed,
                    "ts": time.time(),
                })
            except Exception as exc:
                yield _sse({"event": "error", "message": f"Indexer: {exc}", "ts": time.time()})

        yield _sse({"event": "done", "ts": time.time()})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/index")
async def index_corpus():
    """Run the RAG indexer on all unindexed corpus files."""
    from swing_trader.rag.indexer import index_new_files

    indexed = await index_new_files(corpus_root=_CORPUS_ROOT)
    return {"indexed": indexed}


@router.get("/status")
async def corpus_status():
    """Return corpus file counts and manifest summary."""
    manifest_path = _CORPUS_ROOT / "backfill_manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except Exception:
            pass

    # Count files by source type
    counts: dict[str, int] = {}
    total = 0
    for subdir in ["news", "analyst", "macro", "social"]:
        p = _CORPUS_ROOT / subdir
        if p.exists():
            n = len(list(p.glob("*.md")))
            counts[subdir] = n
            total += n

    rejected = 0
    rej_dir = _CORPUS_ROOT / "_rejected"
    if rej_dir.exists():
        rejected = len(list(rej_dir.glob("*.md")))

    # Unique tickers from manifest
    tickers_done = set()
    for key in manifest:
        parts = key.split("_")
        if parts[0] not in ("macro",):
            tickers_done.add(parts[0])

    # Vector store count
    vector_count = 0
    try:
        from swing_trader.rag.store import get_vector_store
        store = get_vector_store()
        vector_count = store._col.count()
    except Exception:
        pass

    return {
        "total_files": total,
        "by_source_type": counts,
        "rejected_files": rejected,
        "tickers_in_manifest": sorted(tickers_done),
        "manifest_entries": len(manifest),
        "vector_store_chunks": vector_count,
    }


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"
