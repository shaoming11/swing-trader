# RAG Pipeline — Design Spec

Covers the full lifecycle from corpus file → vector index → retrieval → rerank → prompt-ready qualitative block. This is Step 2 of the Layer 1 pipeline (PRD Section 5.2).

---

## 1. Pipeline Overview

```
[Corpus Files] → [Indexing Pipeline]
                        ↓
                  [Vector Store]
                        ↓
[Query: ticker + date_window] → [Hard Filter]
                                      ↓
                               [Embedding Search (top-50)]
                                      ↓
                               [Reranker (top-50 → top-10)]
                                      ↓
                               [Sentiment Tag Pass]
                                      ↓
                        [Qualitative Block for Prompt Composition]
```

---

## 2. Indexing Pipeline

Runs after the corpus generator writes new files. Converts markdown files into embedded, filterable chunks in the vector store.

### 2.1 Chunking

Each markdown file is chunked before embedding:

- Strip frontmatter before chunking (frontmatter is stored as metadata, not embedded)
- Chunk size: 400 tokens
- Overlap: 50 tokens
- Minimum chunk size: 50 tokens — discard smaller chunks
- Do NOT split mid-sentence: use sentence-aware splitter (e.g., `langchain_text_splitters.RecursiveCharacterTextSplitter` with sentence boundaries)

Each chunk inherits the full frontmatter of its parent file as metadata.

### 2.2 Embedding Model

| Option | Notes |
|---|---|
| `text-embedding-3-small` (OpenAI) | Good cost/quality tradeoff for financial text |
| `text-embedding-3-large` (OpenAI) | Better for nuanced sentiment; 2× cost |
| `nomic-embed-text` (self-hosted) | Free, runs locally, slightly lower quality |

Default: `text-embedding-3-small`. Switch to `large` only if retrieval eval shows meaningful recall improvement.

Embed the chunk body only — not the frontmatter. The frontmatter is used for hard filtering, not semantic search.

### 2.3 Vector Store Schema

Each stored record:

```json
{
  "id": "uuid",
  "embedding": [float, ...],
  "content": "chunk body text",
  "metadata": {
    "file_path": "corpus/news/2024-03-15_AAPL_apple-beats-q2.md",
    "chunk_index": 0,
    "date": "2024-03-15",
    "tickers": ["AAPL"],
    "source_type": "news",
    "source": "NewsAPI",
    "relevance_tags": ["earnings", "sentiment"],
    "sentiment_label": "bullish",
    "sentiment_reason": "Beat EPS estimate by 12%",
    "url": "https://..."
  }
}
```

### 2.4 Vector Store Options

| Option | Hosting | Notes |
|---|---|---|
| **Qdrant** | Self-hosted or cloud | Best metadata filtering; recommended |
| Pinecone | Managed | Easy to start; less flexible filtering |
| pgvector | Postgres extension | Good if you're already on Postgres; simpler infra |
| Chroma | Local / self-hosted | Good for development and testing |

**Recommended:** Qdrant for production (payload filtering is first-class), Chroma for local development.

Metadata fields that need indexed filter support: `tickers`, `date`, `source_type`, `sentiment_label`.

### 2.5 Incremental Indexing

- On each corpus generator run, index only new files (check by `file_path` — skip if already in store)
- Deleted or rejected files: mark as `active: false` in the vector store rather than deleting (preserves audit trail)
- Re-indexing a file (e.g., after LLM tagging updates frontmatter): delete old chunks by `file_path`, re-embed and insert

---

## 3. Retrieval Flow

### 3.1 Input

```python
retrieve(
    ticker: str,                    # e.g., "AAPL"
    window_start: date,             # start of the analysis window
    window_end: date,               # end of the analysis window
    thesis_hint: str | None,        # optional: "Fed rate decision impact on tech"
    top_k_before_rerank: int = 50,
    top_k_after_rerank: int = 10
)
```

### 3.2 Stage 1 — Hard Filter

Applied before embedding search. Eliminates irrelevant chunks at the metadata level — fast, free, deterministic.

```python
filter = {
    "tickers": {"$contains": ticker},
    "date": {"$gte": window_start, "$lte": window_end},
    "active": True
}
```

This is the primary solution to "not all news is relevant." Most irrelevance is eliminated here before any model is involved.

### 3.3 Stage 2 — Embedding Search

Run a vector similarity search within the filtered subset.

Query vector: embed the query string constructed from the ticker and window context:

```python
query_text = f"{ticker} stock price movement drivers {window_start} to {window_end}"
if thesis_hint:
    query_text += f". Focus: {thesis_hint}"
```

Return top-50 chunks by cosine similarity from the filtered set.

### 3.4 Stage 3 — Reranker

Reduces top-50 → top-10. The embedding search finds semantically similar chunks; the reranker scores relevance to the specific query more precisely.

**Options:**

| Option | Notes |
|---|---|
| Cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`) | Free, fast, runs locally, good quality |
| Cohere Rerank API | Managed, slightly better quality, per-call cost |
| LLM rerank (Haiku/flash) | Most flexible, highest cost — use only if cross-encoder underperforms |

**Recommended:** Cross-encoder locally for cost efficiency. Reranker runs on (query, chunk) pairs and returns a relevance score. Sort by score, take top-10.

If the top-10 after reranking all have relevance score below a threshold (e.g., < 0.3), return an empty qualitative block and log a warning — do not inject low-quality context into the prompt.

### 3.5 Stage 4 — Sentiment Tag Pass

If `sentiment_label` is already populated in the chunk's metadata (set by the corpus generator's LLM tagging pass), skip this step.

If not populated (e.g., legacy chunks or failed tagging), run a batch sentiment call:

```python
# Batch all untagged chunks in one LLM call
prompt = """
Tag each item. Return a JSON array in the same order.
Items: {json.dumps([c.content for c in untagged_chunks])}
Ticker: {ticker}

For each item return:
{"sentiment_label": "bullish|bearish|neutral", "sentiment_reason": "one sentence"}
"""
```

Use the cheapest available model (Claude Haiku or equivalent). This should be rare in production if the corpus generator is running the tagging pass correctly.

---

## 4. Output — Qualitative Block

The retrieval pipeline returns a structured qualitative block, not raw chunks. This is what gets injected into the composed prompt in Layer 1.

### 4.1 Deduplication Before Output

Before building the block, deduplicate the top-10 chunks:
- If two chunks are from the same file (same `file_path`), keep only the highest-scoring one
- If two chunks have near-identical content (cosine similarity > 0.95 between them), keep only the highest-scoring one

### 4.2 Output Format

```python
@dataclass
class QualitativeBlock:
    ticker: str
    window_start: date
    window_end: date
    chunks_retrieved: int       # before rerank
    chunks_used: int            # after dedup (logged to observability)
    items: list[QualItem]

@dataclass
class QualItem:
    source_type: str            # news | analyst | social | macro
    date: str
    source: str
    sentiment_label: str
    sentiment_reason: str
    summary: str                # the chunk body, truncated to 200 tokens max
    relevance_score: float      # reranker score
```

### 4.3 Rendered Prompt Block

The qualitative block is rendered into the composed prompt as condensed bullets, grouped by source type:

```
=== QUALITATIVE CONTEXT: AAPL (2024-03-01 to 2024-03-15) ===

[NEWS]
• 2024-03-15 | NewsAPI | BULLISH — Beat EPS estimate by 12%
  "Apple reported Q2 earnings of $2.18 per share, beating the $1.94 consensus estimate..."

• 2024-03-10 | GDELT | BEARISH — Trade policy risk flagged
  "New tariff proposals could increase Apple's manufacturing costs by an estimated..."

[ANALYST]
• 2024-03-10 | Morgan Stanley | BULLISH — Upgraded to Overweight, PT $220
  "Raised on strong services revenue trajectory and iPhone 16 cycle expectations..."

[SOCIAL]
• 2024-03-14 | StockTwits | BULLISH — 68% bullish (340 messages)
  Top signal: earnings beat driving call volume spike in options chain

[MACRO]
• 2024-03-12 | FRED | NEUTRAL — CPI 3.2% (prior: 3.1%)
  "Inflation ticked up slightly, reducing probability of March rate cut..."
```

Max total tokens for the qualitative block: 1,500 tokens. If truncation is needed, prioritize by: analyst > news > macro > social.

---

## 5. Retrieval Evaluation

Build retrieval eval in isolation before wiring it to the reasoning pipeline. A bad retriever silently degrades every downstream output.

### 5.1 Eval Dataset

For each ticker/quarter in the golden set (PRD Section 8), manually label:
- Which chunks from the corpus were actually relevant to the price movement
- Which chunks were irrelevant noise

This gives you a ground truth relevance set.

### 5.2 Metrics

| Metric | Target | What it tells you |
|---|---|---|
| Recall@10 | > 0.80 | Are the manually-labeled relevant chunks in the top-10? |
| Precision@10 | > 0.60 | Of the top-10 returned, what fraction are actually relevant? |
| MRR (Mean Reciprocal Rank) | > 0.70 | Is the most relevant chunk near the top? |
| Empty block rate | < 5% | How often does retrieval return nothing? |
| Reranker score threshold hit rate | Track only | How often do all top-10 chunks fall below the score threshold? |

### 5.3 Ablation Tests

Run these to tune the pipeline before full integration:

1. **Hard filter only vs. hard filter + embedding search** — measures value of semantic search on top of metadata filtering
2. **Embedding search only vs. + reranker** — measures reranker lift (expect +10-20% precision)
3. **top-k = 10 vs. 20 vs. 50 before rerank** — find the right recall/cost tradeoff
4. **With thesis_hint vs. without** — measures whether a guided query improves precision

---

## 6. Integration with LangGraph

The retrieval pipeline runs as a single LangGraph node: `rag_retrieval`.

```python
def rag_retrieval_node(state: PipelineState) -> PipelineState:
    block = retrieve(
        ticker=state.ticker,
        window_start=state.window_start,
        window_end=state.window_end,
        thesis_hint=state.thesis_hint
    )

    # Log to observability
    log_node_output("rag_retrieval", {
        "chunks_retrieved": block.chunks_retrieved,
        "chunks_used": block.chunks_used,
        "top_scores": [i.relevance_score for i in block.items]
    })

    # Guardrail: if block is empty, flag it — do not silently pass empty context
    if block.chunks_used == 0:
        state.warnings.append("rag_retrieval: no relevant chunks found")

    state.qualitative_block = block
    return state
```

The node does not raise an exception on empty results — it flags a warning and continues. The prompt composition node is responsible for handling the case where the qualitative block is empty (it can still run on fundamentals + macro alone, but should note the absence of news context in the prompt).

---

## 7. Implementation Notes

- **Vector store client:** `qdrant-client` (Python) for production; `chromadb` for local dev
- **Embedding:** `openai` SDK with `text-embedding-3-small`; cache embeddings by content hash to avoid re-embedding identical text
- **Chunking:** `langchain_text_splitters.RecursiveCharacterTextSplitter`
- **Reranker:** `sentence-transformers` library with `cross-encoder/ms-marco-MiniLM-L-6-v2`
- **Frontmatter parsing:** `python-frontmatter` (consistent with corpus generator)
- **Indexing trigger:** call `index_new_files()` at the end of each corpus generator run
- **Do not embed rejected files** (`corpus/_rejected/` is never indexed)
