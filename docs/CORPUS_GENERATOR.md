# Corpus Generator — Design Spec

Generates and maintains the markdown corpus consumed by the RAG pipeline. This is the bootstrapping problem: without a corpus, retrieval returns nothing; without good retrieval, the reasoning pipeline has no qualitative context.

---

## 1. What the Corpus Generator Does

For a given ticker and date window, it:

1. Pulls raw content from each source (news, analyst, social, macro)
2. Cleans and normalizes the text
3. Writes one markdown file per article/item with required frontmatter
4. Optionally runs a lightweight LLM pass to add `relevance_tags`

It runs in two modes:
- **Backfill mode** — historical range, used to build the training dataset
- **Live mode** — daily scheduled job, used in production to keep the corpus current

---

## 2. Output File Format

Every generated file follows the structure defined in the RAG corpus spec (PRD Section 6).

### File naming
```
corpus/{source_type}/{YYYY-MM-DD}_{TICKER}_{slug}.md
```

- `slug` = first 5 words of the headline, lowercased, hyphenated
- For macro files (no ticker): `corpus/macro/{YYYY-MM-DD}_{indicator}.md`

### File structure
```markdown
---
date: YYYY-MM-DD
tickers: [AAPL]
source_type: news | analyst | social | macro
source: NewsAPI | GDELT | StockTwits | Reddit | FMP | FRED
relevance_tags: [earnings, macro, sentiment, technical, geopolitics]
sentiment_label: bullish | bearish | neutral          # added by LLM pass
sentiment_reason: "one-line reason"                   # added by LLM pass
url: https://...                                      # original source, if available
---

# {Headline or Title}

{Body text — cleaned, no ads, no boilerplate, no HTML}
```

---

## 3. Sources and Pull Logic

### 3.1 News — NewsAPI

```
GET /v2/everything
  q         = "{ticker} OR {company_name}"
  from      = window_start
  to        = window_end
  language  = en
  sortBy    = relevancy
  pageSize  = 100
```

- Filter out articles where ticker/company name appears only in passing (headline must contain ticker or company name)
- Drop duplicates by URL
- Output: `corpus/news/`

### 3.2 News — GDELT (geopolitics and macro-adjacent events)

Use the GDELT 2.0 Event API or the DOC API for full-text search:

```
https://api.gdeltproject.org/api/v2/doc/doc
  query     = "{ticker} OR {sector_keywords}"
  mode      = artlist
  maxrecords= 50
  startdatetime = {window_start}HHMMSS
  enddatetime   = {window_end}HHMMSS
  format    = json
```

- Preferred for: regulatory actions, trade policy, geopolitical events that affect sector
- Not for individual company sentiment — use NewsAPI for that
- Output: `corpus/news/` with `source: GDELT`

### 3.3 Analyst Ratings — Financial Modeling Prep

```
GET /v3/analyst-stock-recommendations/{ticker}?apikey=KEY
GET /v3/upgrades-downgrades/{ticker}?apikey=KEY
```

- Filter to `date within window`
- One file per rating action: firm name, action (upgrade/downgrade/initiation), price target, prior rating → new rating
- Output: `corpus/analyst/`

### 3.4 Social Sentiment — StockTwits

```
GET https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json
  since  = {first_id_in_window}
  max    = {last_id_in_window}
```

- Pull up to 200 messages per day for the window
- Aggregate into daily summary files (not one file per tweet — too granular)
- Each daily file: bullish count, bearish count, top 5 messages by engagement
- Output: `corpus/social/` with `source: StockTwits`

### 3.5 Social Sentiment — Reddit

Use Pushshift API or Reddit API:
```
subreddits: wallstreetbets, investing, stocks, options
q         = "{ticker}"
after     = {window_start_epoch}
before    = {window_end_epoch}
sort      = score
limit     = 100
```

- Filter to posts with score > 10 to remove noise
- Aggregate into daily summary files same as StockTwits
- Output: `corpus/social/` with `source: Reddit`

### 3.6 Macro — FRED

Pull the following series on their release dates:

| Series ID | Indicator |
|---|---|
| `CPIAUCSL` | CPI (monthly) |
| `FEDFUNDS` | Fed Funds Rate |
| `UNRATE` | Unemployment Rate |
| `GDP` | GDP (quarterly) |
| `T10YIE` | 10-Year Breakeven Inflation |
| `DGS10` | 10-Year Treasury Yield |

```
GET https://api.stlouisfed.org/fred/series/observations
  series_id       = {SERIES_ID}
  observation_start = {window_start}
  observation_end   = {window_end}
  api_key         = KEY
  file_type       = json
```

- One file per release event (not one file per series), tagged with the print value and prior value
- Output: `corpus/macro/`

---

## 4. LLM Tagging Pass (Optional, Recommended)

After writing the raw file, run a cheap LLM call (Haiku or equivalent) to add `relevance_tags`, `sentiment_label`, and `sentiment_reason` to the frontmatter.

### Prompt
```
You are tagging a financial news item for a swing trading RAG corpus.

Article:
{body_text}

Ticker in focus: {ticker}

Return JSON only:
{
  "relevance_tags": [...],   // subset of: earnings, macro, sentiment, technical, geopolitics, corporate_action, analyst_rating
  "sentiment_label": "bullish | bearish | neutral",
  "sentiment_reason": "one sentence"
}
```

- If the article is not meaningfully about `ticker` (i.e., ticker only mentioned in passing), set `relevance_tags: []` — the retrieval filter will skip it
- Run this as a batch job after raw pull, not inline, to keep pull fast and tagging cost isolated

---

## 5. Backfill Mode (Historical Dataset)

Used to build the training corpus for the eval loop.

### Input
```python
backfill(
    tickers=["AAPL", "MSFT", "NVDA"],
    start_date="2022-01-01",
    end_date="2024-12-31",
    sources=["newsapi", "gdelt", "fmp", "stocktwits", "fred"]
)
```

### Behavior
- Iterates quarter by quarter to stay within API rate limits
- Skips files that already exist (idempotent — safe to re-run)
- Writes a `backfill_manifest.json` tracking which ticker/quarter combos are complete
- Rate-limit aware: adds jitter between requests; respects per-source limits

### API Rate Limit Reference

| Source | Limit |
|---|---|
| NewsAPI (free) | 100 requests/day; use paid for historical beyond 1 month |
| GDELT | No key required; be polite (1 req/sec) |
| FMP | Depends on plan; ~300 req/min on basic |
| StockTwits | 200 req/hour unauthenticated |
| FRED | 120 req/min with key |

For deep historical backfills (2+ years), consider caching raw API responses to disk before writing formatted markdown — avoids re-fetching on re-runs.

---

## 6. Live Mode (Production)

Runs as a daily scheduled job. Pulls the prior day's content for all tracked tickers.

```python
live_run(
    tickers=get_tracked_tickers(),   # from user watchlist or position list
    date=yesterday(),
    sources=["newsapi", "gdelt", "fmp", "stocktwits", "fred"]
)
```

- Triggered by cron at 06:00 UTC (after US market close data is available)
- On failure: log error, send alert, do NOT crash — missing one day is recoverable
- LLM tagging pass runs after all files are written, not inline

---

## 7. Deduplication

Before writing a file, check:
1. Does a file with the same `url` already exist in the corpus? → skip
2. Does a file with the same `date` + `ticker` + headline (fuzzy match > 0.9 similarity) already exist? → skip

For social aggregates (StockTwits/Reddit), check: does `corpus/social/{date}_{ticker}_{platform}.md` already exist? → skip (idempotent by design).

---

## 8. Corpus Quality Rules

Files that fail any of these rules are written to `corpus/_rejected/` with a reason tag, not deleted:

| Rule | Reason |
|---|---|
| Body text < 50 words | Too short to be meaningful |
| `relevance_tags: []` after LLM pass | Not meaningfully about the ticker |
| Body is mostly boilerplate/ad text | Detected by simple heuristic (ratio of unique words) |
| Date outside the requested window | Source API returned out-of-range result |

Rejected files are kept for audit purposes. The retrieval pipeline never indexes `_rejected/`.

---

## 9. File Layout

```
corpus/
  news/
    2024-03-15_AAPL_apple-beats-q2-earnings.md
    2024-03-15_AAPL_fed-signals-rate-hold.md
  analyst/
    2024-03-10_AAPL_morgan-stanley-upgrade.md
  social/
    2024-03-15_AAPL_stocktwits.md
    2024-03-15_AAPL_reddit.md
  macro/
    2024-03-12_CPI.md
    2024-03-20_FOMC.md
  _rejected/
    2024-03-15_AAPL_short-article.md    # reason: body < 50 words
backfill_manifest.json
```

---

## 10. Implementation Notes

- Write in Python; use `httpx` for async HTTP (multiple sources in parallel per date window)
- Use `markdownify` or manual stripping for HTML → clean text conversion
- Frontmatter: use `python-frontmatter` library for consistent read/write
- LLM tagging: batch 20 articles per call using a message array to minimize cost
- Test the backfill on a single ticker + single quarter before running full historical pull
