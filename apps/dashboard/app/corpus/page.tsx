"use client";

import { useEffect, useState, useRef } from "react";
import { getCorpusStatus, streamBackfill } from "@/lib/api";
import type { CorpusStatus, CorpusEvent } from "@/lib/api";

// Major NASDAQ-100 + S&P 500 tickers for quick selection
const PRESET_GROUPS: Record<string, string[]> = {
  "Mag 7": ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"],
  "Indices": ["SPY", "QQQ", "DIA", "IWM"],
  "Semis": ["NVDA", "AMD", "INTC", "AVGO", "QCOM", "TSM", "MU", "ASML"],
  "Finance": ["JPM", "BAC", "GS", "MS", "V", "MA", "BRK.B"],
  "Health": ["UNH", "JNJ", "LLY", "PFE", "ABBV", "MRK", "TMO"],
  "Energy": ["XOM", "CVX", "COP", "SLB", "EOG"],
  "Consumer": ["AMZN", "COST", "WMT", "HD", "NKE", "SBUX", "MCD"],
  "Software": ["MSFT", "CRM", "ADBE", "NOW", "ORCL", "SNOW", "PLTR"],
};

function todayStr(): string {
  return new Date().toISOString().slice(0, 10);
}

function sixMonthsAgo(): string {
  const d = new Date();
  d.setMonth(d.getMonth() - 6);
  return d.toISOString().slice(0, 10);
}

export default function CorpusPage() {
  const [status, setStatus] = useState<CorpusStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);

  // Form state
  const [tickerInput, setTickerInput] = useState("");
  const [selectedTickers, setSelectedTickers] = useState<string[]>([]);
  const [startDate, setStartDate] = useState(sixMonthsAgo);
  const [endDate, setEndDate] = useState(todayStr);
  const [runTagger, setRunTagger] = useState(true);
  const [runIndexer, setRunIndexer] = useState(true);

  // Run state
  const [running, setRunning] = useState(false);
  const [events, setEvents] = useState<CorpusEvent[]>([]);
  const controllerRef = useRef<AbortController | null>(null);

  function loadStatus() {
    setStatusLoading(true);
    setStatusError(null);
    getCorpusStatus()
      .then(setStatus)
      .catch((err) => setStatusError(String(err)))
      .finally(() => setStatusLoading(false));
  }

  useEffect(() => {
    loadStatus();
  }, []);

  function addTicker(ticker: string) {
    const t = ticker.trim().toUpperCase();
    if (t && !selectedTickers.includes(t)) {
      setSelectedTickers((prev) => [...prev, t]);
    }
  }

  function removeTicker(ticker: string) {
    setSelectedTickers((prev) => prev.filter((t) => t !== ticker));
  }

  function addPresetGroup(group: string) {
    const tickers = PRESET_GROUPS[group] ?? [];
    setSelectedTickers((prev) => {
      const set = new Set(prev);
      tickers.forEach((t) => set.add(t));
      return Array.from(set);
    });
  }

  function handleTickerKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      addTicker(tickerInput);
      setTickerInput("");
    }
  }

  function handleRun() {
    if (selectedTickers.length === 0) return;
    setRunning(true);
    setEvents([]);

    const controller = streamBackfill(
      {
        tickers: selectedTickers,
        start_date: startDate,
        end_date: endDate,
        run_tagger: runTagger,
        run_indexer: runIndexer,
      },
      (event) => {
        setEvents((prev) => [...prev, event]);
        if (event.event === "done" || event.event === "error") {
          setRunning(false);
          loadStatus();
        }
      },
    );
    controllerRef.current = controller;
  }

  function handleCancel() {
    controllerRef.current?.abort();
    setRunning(false);
  }

  const lastBackfillDone = events.find((e) => e.event === "backfill_done");
  const lastIndexDone = events.find((e) => e.event === "indexing_done");
  const lastError = events.findLast((e) => e.event === "error");
  const isDone = events.some((e) => e.event === "done");

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-white mb-1">Corpus Generator</h1>
        <p className="text-sm text-gray-400">
          Pull news, analyst ratings, macro data, and social sentiment into the RAG corpus.
        </p>
      </div>

      {/* Status card */}
      <StatusCard status={status} loading={statusLoading} error={statusError} />

      {/* Ticker selection */}
      <div className="space-y-3">
        <label className="block text-xs font-medium text-gray-400">Tickers</label>

        {/* Preset groups */}
        <div className="flex flex-wrap gap-2">
          {Object.keys(PRESET_GROUPS).map((group) => (
            <button
              key={group}
              onClick={() => addPresetGroup(group)}
              className="rounded-md border border-border bg-card px-2.5 py-1 text-xs text-gray-300 hover:border-accent hover:text-white transition-colors"
            >
              + {group}
            </button>
          ))}
        </div>

        {/* Manual input */}
        <div className="flex gap-2">
          <input
            className={inputClass}
            value={tickerInput}
            onChange={(e) => setTickerInput(e.target.value.toUpperCase())}
            onKeyDown={handleTickerKeyDown}
            placeholder="Type ticker and press Enter"
          />
          <button
            onClick={() => { addTicker(tickerInput); setTickerInput(""); }}
            className="rounded-lg border border-border bg-card px-3 py-2 text-sm text-gray-300 hover:border-accent hover:text-white transition-colors"
          >
            Add
          </button>
        </div>

        {/* Selected tickers */}
        {selectedTickers.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {selectedTickers.map((t) => (
              <span
                key={t}
                className="inline-flex items-center gap-1 rounded-md bg-accent/15 px-2 py-1 text-xs font-mono font-medium text-accent"
              >
                {t}
                <button
                  onClick={() => removeTicker(t)}
                  className="text-accent/60 hover:text-white ml-0.5"
                >
                  x
                </button>
              </span>
            ))}
            <button
              onClick={() => setSelectedTickers([])}
              className="text-xs text-gray-500 hover:text-gray-300 px-1"
            >
              Clear all
            </button>
          </div>
        )}
      </div>

      {/* Date range */}
      <div className="grid grid-cols-2 gap-4 max-w-xl">
        <div>
          <label className="block text-xs font-medium text-gray-400 mb-1.5">Start Date</label>
          <input
            type="date"
            className={inputClass}
            value={startDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-400 mb-1.5">End Date</label>
          <input
            type="date"
            className={inputClass}
            value={endDate}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </div>
      </div>

      {/* Options */}
      <div className="flex gap-6">
        <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
          <input
            type="checkbox"
            checked={runTagger}
            onChange={(e) => setRunTagger(e.target.checked)}
            className="rounded border-border bg-card accent-blue-500"
          />
          Run LLM tagger
        </label>
        <label className="flex items-center gap-2 text-sm text-gray-300 cursor-pointer">
          <input
            type="checkbox"
            checked={runIndexer}
            onChange={(e) => setRunIndexer(e.target.checked)}
            className="rounded border-border bg-card accent-blue-500"
          />
          Index into vector store
        </label>
      </div>

      {/* Run button */}
      <div className="flex gap-3">
        <button
          onClick={handleRun}
          disabled={running || selectedTickers.length === 0}
          className="rounded-lg bg-accent px-6 py-2.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {running ? "Running..." : `Run Backfill (${selectedTickers.length} tickers)`}
        </button>
        {running && (
          <button
            onClick={handleCancel}
            className="rounded-lg border border-danger/50 px-4 py-2.5 text-sm text-danger hover:bg-danger/10 transition-colors"
          >
            Cancel
          </button>
        )}
      </div>

      {/* Progress */}
      {events.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-4 space-y-3">
          <h3 className="text-sm font-medium text-white">Progress</h3>

          <div className="space-y-2">
            {events.map((ev, i) => (
              <EventRow key={i} event={ev} />
            ))}
          </div>

          {isDone && !lastError && (
            <div className="rounded-lg bg-success/10 border border-success/30 px-4 py-3 text-sm text-success">
              Backfill complete
              {lastBackfillDone ? ` — ${lastBackfillDone.written} files written` : ""}
              {lastIndexDone ? `, ${lastIndexDone.indexed} chunks indexed` : ""}
            </div>
          )}

          {lastError && (
            <div className="rounded-lg bg-danger/10 border border-danger/30 px-4 py-3 text-sm text-danger">
              {lastError.message}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function StatusCard({
  status,
  loading,
  error,
}: {
  status: CorpusStatus | null;
  loading: boolean;
  error: string | null;
}) {
  if (loading) return <p className="text-sm text-gray-500 animate-pulse">Loading corpus status...</p>;
  if (error) return <p className="text-sm text-danger">{error}</p>;
  if (!status) return null;

  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
      <StatCard label="Corpus Files" value={status.total_files} />
      <StatCard label="Vector Chunks" value={status.vector_store_chunks} />
      <StatCard label="Tickers" value={status.tickers_in_manifest.length} />
      <StatCard label="Rejected" value={status.rejected_files} />

      {Object.entries(status.by_source_type).length > 0 && (
        <div className="col-span-full">
          <div className="flex gap-4 text-xs text-gray-400">
            {Object.entries(status.by_source_type).map(([type, count]) => (
              <span key={type}>
                <span className="text-gray-200 font-medium">{count}</span> {type}
              </span>
            ))}
          </div>
          {status.tickers_in_manifest.length > 0 && (
            <p className="text-xs text-gray-500 mt-1.5">
              Tickers: {status.tickers_in_manifest.join(", ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <p className="text-xl font-semibold text-white">{value.toLocaleString()}</p>
      <p className="text-xs text-gray-400 mt-0.5">{label}</p>
    </div>
  );
}

function EventRow({ event }: { event: CorpusEvent }) {
  const labels: Record<string, string> = {
    start: "Starting backfill",
    backfill_done: `Backfill complete — ${event.written ?? 0} written, ${event.skipped ?? 0} skipped`,
    indexing_start: "Indexing into vector store...",
    indexing_done: `Indexing complete — ${event.indexed ?? 0} chunks`,
    error: `Error: ${event.message}`,
    done: "All done",
  };

  const colors: Record<string, string> = {
    start: "text-accent",
    backfill_done: "text-success",
    indexing_start: "text-accent",
    indexing_done: "text-success",
    error: "text-danger",
    done: "text-gray-400",
  };

  return (
    <div className="flex items-center gap-2 text-xs">
      <span className={`w-1.5 h-1.5 rounded-full ${
        event.event === "error" ? "bg-danger" :
        event.event.includes("done") || event.event === "done" ? "bg-success" :
        "bg-accent animate-pulse"
      }`} />
      <span className={colors[event.event] ?? "text-gray-400"}>
        {labels[event.event] ?? event.event}
      </span>
    </div>
  );
}

const inputClass =
  "w-full rounded-lg border border-border bg-card px-3 py-2 text-sm text-gray-100 placeholder-gray-600 focus:border-accent focus:outline-none";
