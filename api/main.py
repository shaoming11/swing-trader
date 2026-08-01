"""FastAPI bridge — exposes pipeline trigger, SSE streaming, eval harness, and run history.

Start:
    uvicorn api.main:app --reload --port 8000

Loads .env automatically via python-dotenv so the server works without manually
exporting variables in the shell.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
from fastapi.middleware.cors import CORSMiddleware

from api.routes import corpus, eval_routes, runs


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Tear down the DB pool on shutdown so connections drain cleanly.
    try:
        from swing_trader.db.pool import close_pool
        await close_pool()
    except Exception:
        pass


app = FastAPI(title="Swing Trader API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs.router, prefix="/runs", tags=["runs"])
app.include_router(eval_routes.router, prefix="/eval", tags=["eval"])
app.include_router(corpus.router, prefix="/corpus", tags=["corpus"])


@app.get("/health")
async def health():
    return {"status": "ok"}
