# Swing Trader AI — Parallel Implementation Plan

Derived from PRD Section 13 (Build Order). The sequential list in Section 13 describes _logical_ dependencies, not a strict one-at-a-time order. This document identifies what can be built in parallel across independent tracks.

---

## Dependency Analysis

Section 13 sequential order:
1. RAG retrieval + relevance filter
2. Deterministic data pull
3. Prompt composition
4. Multi-persona reasoning + judge
5. Layer 1 guardrail pass
6. Layer 2 position sizing
7. Observability
8. Eval loop
9. Self-improvement loop
10. Multi-tenancy + production hardening

Actual dependency graph:

```
[1: RAG] ──────────┐
                   ├──> [3: Prompt Composition]
[2: Data Pull] ────┘         │
                             ▼
                    [4: Multi-Persona + Judge]
                             │
                             ▼
                    [5: Layer 1 Guardrail]
                             │
                    ┌────────┴────────┐
                    ▼                 ▼
          [6: Layer 2 Sizing]   [7: Observability wiring]
                    │
          [8: Eval Loop running]
                    │
          [9: Self-improvement]
                    │
          [10: Multi-tenancy]

[B: Infrastructure] ─── runs as parallel track from day 1
[C: Test Data / Golden Set] ─── runs as parallel track from day 1
```

---

## Parallel Tracks

### Track A — Core Data Layer (Steps 1 + 2, fully parallel)

These have no dependencies on each other. Start both simultaneously.

**A1: RAG Retrieval + Relevance Filter**
- Corpus directory structure + frontmatter schema
- Chunking pipeline (300–500 token chunks, 50-token overlap)
- Embedding + vector store setup with metadata filter (`ticker`, `date_range`)
- Two-stage filter: hard filter (ticker + date) then soft rerank
- Lightweight sentiment tagger per chunk: `bullish | bearish | neutral` + one-line reason
- Output: condensed, deduped qualitative bullet list

**A2: Deterministic Data Pull**
- EDGAR/FMP client: EPS actual vs. estimate, revenue growth, guidance changes, corporate actions
- FRED client: CPI, rate decisions, unemployment — with sector-relevant filtering logic
- Fixed structured output block (no LLM, no prose — hard numbers only)
- Template renderer that produces the numeric block fed to Step 3

Blocker for Step 3: both A1 and A2 must be complete.

---

### Track B — Infrastructure (parallel from day 1, never blocks other tracks)

These have zero runtime dependencies. Start alongside Track A.

**B1: Pydantic Schemas**
- `Layer1Output` — direction, magnitude_bucket, confidence, dominant_drivers, invalidation_condition, hold_window_bucket, thesis
- `PositionCard` — entry_price, target_price, stop_loss, hold_window, position_size, thesis
- `RAGChunk` — ticker list, date, source_type, sentiment, reason
- Reuse these schemas everywhere: LLM function-call definitions, guardrail validators, eval store serialization

**B2: Postgres Eval Store Schema**
- Define and migrate the `eval_runs` table (full schema in PRD §12.3)
- Add indexes: `ticker`, `date_window_start`, `confidence`, `hit`
- Build the write path (insert on pipeline completion) and the read path (calibration queries, miss drill-down)
- No pipeline needs to exist yet — use fixture data to validate schema and queries

**B3: LangSmith Project Setup**
- Create project, configure environment variables
- Write thin node wrappers for non-LLM steps (EDGAR pull, FRED pull, vector store query) so they appear in traces
- Validate that a stub LangGraph graph produces a complete trace with all expected node names

---

### Track C — Test Data / Golden Set (parallel from day 1)

Fully independent of all code. Run this alongside everything else.

**C1: Golden Set Curation**
- Select historical quarters with known, verifiable outcomes
- For each entry: assemble inputs (fundamentals, macro data, news/sentiment chunks for the window) and ground truth (actual price direction + magnitude bucket after the hold window)
- Manually label dominant driver (fundamental / macro / sentiment / technical)
- Target: enough entries to produce meaningful calibration curves per bucket (90% / 70% / 50%)

**C2: Eval Harness (no pipeline required)**
- Define the eval runner interface: takes a pipeline callable + golden set, returns calibration curve, price target error, timing error, feature attribution table
- Build calibration curve logic and feature attribution analysis before the pipeline exists
- Wire to the pipeline in Step 8

---

## Phase Summary

| Phase | What runs | Parallel work |
|---|---|---|
| Phase 1 | A1 (RAG) + A2 (Data Pull) | B1 + B2 + B3 + C1 + C2 |
| Phase 2 | Step 3 (Prompt Composition) | — |
| Phase 3 | Step 4 (Multi-Persona + Judge) | — |
| Phase 4 | Step 5 (Layer 1 Guardrail) | Step 7 observability wiring begins |
| Phase 5 | Step 6 (Layer 2 Sizing) | Finish Step 7 wiring |
| Phase 6 | Step 8 (Eval Loop, now runnable) | — |
| Phase 7 | Step 9 (Self-improvement) | Step 10 design |
| Phase 8 | Step 10 (Multi-tenancy + hardening) | — |

---

## Critical Path

The critical path through the project is:

```
A1/A2 → Step 3 → Step 4 → Step 5 → Step 6 → Step 8 → Step 9 → Step 10
```

Everything in Tracks B and C is off the critical path. Completing them early means zero idle time once the pipeline unblocks — the eval harness is ready to fire the moment Step 6 is done.

---

## What to Build First (Day 1)

Start these simultaneously:

- [ ] A1: RAG corpus schema + chunking pipeline
- [ ] A2: EDGAR + FRED data pull clients
- [ ] B1: All Pydantic schemas (takes a few hours; unblocks everything downstream)
- [ ] B2: Postgres eval store schema + migrations
- [ ] B3: LangSmith project + stub LangGraph trace
- [ ] C1: Begin golden set curation (ongoing, no code required)
