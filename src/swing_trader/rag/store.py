"""Vector store abstraction — wraps Qdrant (prod) or Chroma (dev).

Exposes a minimal interface used by the indexer and retriever:
  - upsert(records)
  - search(embedding, filter, top_k) -> list[dict]
  - file_is_indexed(file_path) -> bool
  - delete_by_file_path(file_path)

Select via env var VECTOR_STORE=chroma|qdrant (default: chroma).
"""
from __future__ import annotations

import os
from typing import Any, Protocol


class VectorStore(Protocol):
    def upsert(self, records: list[dict]) -> None: ...
    def search(self, embedding: list[float], filter: dict, top_k: int) -> list[dict]: ...
    def file_is_indexed(self, file_path: str) -> bool: ...
    def delete_by_file_path(self, file_path: str) -> None: ...


def get_vector_store() -> VectorStore:
    backend = os.getenv("VECTOR_STORE", "chroma").lower()
    if backend == "qdrant":
        return _QdrantStore()
    return _ChromaStore()


# ── Chroma (development) ──────────────────────────────────────────────────────

class _ChromaStore:
    _COLLECTION = "swing_trader_corpus"

    def __init__(self):
        import chromadb
        persist_dir = os.getenv("CHROMA_PERSIST_DIR", ".chroma")
        self._client = chromadb.PersistentClient(path=persist_dir)
        self._col = self._client.get_or_create_collection(
            name=self._COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, records: list[dict]) -> None:
        self._col.upsert(
            ids=[r["id"] for r in records],
            embeddings=[r["embedding"] for r in records],
            documents=[r["content"] for r in records],
            metadatas=[r["metadata"] for r in records],
        )

    def search(
        self, embedding: list[float], filter: dict, top_k: int
    ) -> list[dict]:
        where = _build_chroma_where(filter)
        results = self._col.query(
            query_embeddings=[embedding],
            n_results=min(top_k, self._col.count() or 1),
            where=where if where else None,
            include=["documents", "metadatas", "distances"],
        )
        out = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            out.append({
                "content": doc,
                "metadata": meta,
                "score": 1.0 - float(dist),  # cosine distance → similarity
            })
        # Post-filter by date (ISO strings sort lexicographically — no numeric cast needed)
        date_gte = filter.get("date_gte")
        date_lte = filter.get("date_lte")
        if date_gte or date_lte:
            filtered = []
            for r in out:
                d = r["metadata"].get("date", "")
                if date_gte and d < date_gte:
                    continue
                if date_lte and d > date_lte:
                    continue
                filtered.append(r)
            return filtered
        return out

    def file_is_indexed(self, file_path: str) -> bool:
        results = self._col.get(where={"file_path": file_path}, limit=1)
        return len(results["ids"]) > 0

    def delete_by_file_path(self, file_path: str) -> None:
        results = self._col.get(where={"file_path": file_path})
        if results["ids"]:
            self._col.delete(ids=results["ids"])


# ── Qdrant (production) ───────────────────────────────────────────────────────

_EMBED_VECTOR_SIZES = {
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}


def _vector_size() -> int:
    import os
    model = os.getenv("EMBED_MODEL", "nomic-embed-text")
    return _EMBED_VECTOR_SIZES.get(model, 768)


class _QdrantStore:
    _COLLECTION = "swing_trader_corpus"

    def __init__(self):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        url = os.getenv("QDRANT_URL", "http://localhost:6333")
        api_key = os.getenv("QDRANT_API_KEY") or None
        self._client = QdrantClient(url=url, api_key=api_key)

        if not self._client.collection_exists(self._COLLECTION):
            self._client.create_collection(
                collection_name=self._COLLECTION,
                vectors_config=VectorParams(
                    size=_vector_size(), distance=Distance.COSINE
                ),
            )

    def upsert(self, records: list[dict]) -> None:
        from qdrant_client.models import PointStruct
        points = [
            PointStruct(id=r["id"], vector=r["embedding"], payload={
                "content": r["content"],
                **r["metadata"],
            })
            for r in records
        ]
        self._client.upsert(collection_name=self._COLLECTION, points=points)

    def search(
        self, embedding: list[float], filter: dict, top_k: int
    ) -> list[dict]:
        from qdrant_client.models import Filter
        qdrant_filter = _build_qdrant_filter(filter)
        results = self._client.search(
            collection_name=self._COLLECTION,
            query_vector=embedding,
            query_filter=qdrant_filter,
            limit=top_k,
            with_payload=True,
        )
        return [
            {
                "content": r.payload.get("content", ""),
                "metadata": {k: v for k, v in r.payload.items() if k != "content"},
                "score": r.score,
            }
            for r in results
        ]

    def file_is_indexed(self, file_path: str) -> bool:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        results = self._client.scroll(
            collection_name=self._COLLECTION,
            scroll_filter=Filter(must=[
                FieldCondition(key="file_path", match=MatchValue(value=file_path))
            ]),
            limit=1,
        )
        return len(results[0]) > 0

    def delete_by_file_path(self, file_path: str) -> None:
        from qdrant_client.models import FieldCondition, Filter, MatchValue
        self._client.delete(
            collection_name=self._COLLECTION,
            points_selector=Filter(must=[
                FieldCondition(key="file_path", match=MatchValue(value=file_path))
            ]),
        )


# ── Filter builders ───────────────────────────────────────────────────────────

def _build_chroma_where(filter: dict) -> dict:
    """Convert our generic filter dict to Chroma's $and/$or where format.

    Date range filters are intentionally excluded here — ChromaDB's $gte/$lte
    operators require numeric values, but dates are stored as ISO strings.
    Date filtering is done in Python after retrieval instead.
    """
    conditions = []
    if "ticker" in filter:
        conditions.append({"tickers": {"$contains": filter["ticker"]}})
    if "active" in filter:
        conditions.append({"active": {"$eq": filter["active"]}})

    if len(conditions) == 1:
        return conditions[0]
    if len(conditions) > 1:
        return {"$and": conditions}
    return {}


def _build_qdrant_filter(filter: dict):
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        MatchValue,
        Range,
    )
    must = []
    if "ticker" in filter:
        must.append(
            FieldCondition(key="tickers", match=MatchValue(value=filter["ticker"]))
        )
    if "date_gte" in filter:
        must.append(
            FieldCondition(key="date", range=Range(gte=filter["date_gte"]))
        )
    if "date_lte" in filter:
        must.append(
            FieldCondition(key="date", range=Range(lte=filter["date_lte"]))
        )
    if "active" in filter:
        must.append(
            FieldCondition(key="active", match=MatchValue(value=filter["active"]))
        )
    return Filter(must=must) if must else None
