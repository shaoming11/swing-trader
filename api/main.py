"""FastAPI bridge — exposes pipeline trigger, SSE streaming, eval harness, and run history.

Start:
    uvicorn api.main:app --reload --port 8000

Loads .env automatically via python-dotenv so the server works without manually
exporting variables in the shell.
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.middleware import APIKeyMiddleware, RateLimitMiddleware
from api.routes import corpus, eval_routes, runs

log = logging.getLogger("swing_trader.api")

# ── Startup config validation ─────────────────────────────────────────────────

_REQUIRED_VARS = [
    "GROQ_API_KEY",
    "JINA_API_KEY",
]
_RECOMMENDED_VARS = [
    "DATABASE_URL",
    "FMP_API_KEY",
    "FRED_API_KEY",
    "POLYGON_API_KEY",
]


def _validate_config() -> None:
    """Check required env vars at startup. Fail fast on missing critical keys."""
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in the values."
        )
    for v in _RECOMMENDED_VARS:
        if not os.getenv(v):
            log.warning("Recommended env var %s is not set — some features may not work", v)


# ── CORS origins ──────────────────────────────────────────────────────────────

def _get_cors_origins() -> list[str]:
    """Parse CORS_ORIGINS env var. Defaults to localhost for dev."""
    raw = os.getenv("CORS_ORIGINS", "")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # Dev defaults
    return [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
    ]


# ── Health check helpers ──────────────────────────────────────────────────────

async def _check_db() -> dict:
    """Ping the database and return status."""
    try:
        from swing_trader.db.pool import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


async def _check_vector_store() -> dict:
    """Ping the vector store and return status."""
    try:
        from swing_trader.rag.store import get_vector_store
        store = get_vector_store()
        count = store._col.count()
        return {"status": "ok", "chunks": count}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _validate_config()
    log.info("Config validated — all required env vars present")
    yield
    # Tear down the DB pool on shutdown so connections drain cleanly.
    try:
        from swing_trader.db.pool import close_pool
        await close_pool()
    except Exception:
        pass


app = FastAPI(title="Swing Trader API", version="0.1.0", lifespan=lifespan)

# Security middleware (order matters — rate limit first, then auth)
app.add_middleware(APIKeyMiddleware)
app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(runs.router, prefix="/runs", tags=["runs"])
app.include_router(eval_routes.router, prefix="/eval", tags=["eval"])
app.include_router(corpus.router, prefix="/corpus", tags=["corpus"])


@app.get("/health")
async def health():
    """Liveness + readiness probe. Checks DB and vector store connectivity."""
    db = await _check_db()
    vector = await _check_vector_store()
    all_ok = db["status"] == "ok" and vector["status"] == "ok"
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": {
            "database": db,
            "vector_store": vector,
        },
    }
