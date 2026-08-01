const BASE = "/api";

export interface RunRequest {
  ticker: string;
  window_start: string;
  window_end: string;
  run_type: "live" | "backfill" | "eval";
  thesis_hint?: string;
  user_id?: string;
}

export interface RunSummary {
  run_id: string;
  ticker: string;
  window_start: string;
  window_end: string;
  run_type: string;
  pipeline_version: string;
  direction: string | null;
  magnitude_bucket: string | null;
  confidence: number | null;
  gate_passed: boolean | null;
  guardrail_retries: number;
  pipeline_cancelled: boolean;
  hit: boolean | null;
  completed_at: string | null;
}

export interface NodeEvent {
  event: "node_start" | "node_done" | "pipeline_done" | "error" | "stream_closed" | "timeout";
  node?: string;
  elapsed_s?: number;
  ts?: number;
  output?: Record<string, unknown>;
  warnings?: string[];
  message?: string;
  run_id?: string;
}

export async function triggerRun(req: RunRequest): Promise<{ run_id: string }> {
  const res = await fetch(`${BASE}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getRun(runId: string): Promise<Record<string, unknown>> {
  const res = await fetch(`${BASE}/runs/${runId}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function listRuns(params?: {
  ticker?: string;
  run_type?: string;
  limit?: number;
}): Promise<RunSummary[]> {
  const qs = new URLSearchParams();
  if (params?.ticker) qs.set("ticker", params.ticker);
  if (params?.run_type) qs.set("run_type", params.run_type);
  if (params?.limit) qs.set("limit", String(params.limit));
  const res = await fetch(`${BASE}/runs?${qs}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export function streamRun(runId: string, onEvent: (e: NodeEvent) => void): () => void {
  const es = new EventSource(`${BASE}/runs/${runId}/stream`);
  es.onmessage = (msg) => {
    try {
      onEvent(JSON.parse(msg.data) as NodeEvent);
    } catch {
      // ignore parse errors
    }
  };
  es.onerror = () => es.close();
  return () => es.close();
}

export async function getCalibration(params?: {
  ticker?: string;
  pipeline_version?: string;
}): Promise<unknown> {
  const qs = new URLSearchParams();
  if (params?.ticker) qs.set("ticker", params.ticker);
  if (params?.pipeline_version) qs.set("pipeline_version", params.pipeline_version);
  const res = await fetch(`${BASE}/eval/calibration?${qs}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getAttribution(params?: { ticker?: string }): Promise<unknown> {
  const qs = new URLSearchParams();
  if (params?.ticker) qs.set("ticker", params.ticker);
  const res = await fetch(`${BASE}/eval/attribution?${qs}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export async function getRegression(params?: { ticker?: string }): Promise<unknown> {
  const qs = new URLSearchParams();
  if (params?.ticker) qs.set("ticker", params.ticker);
  const res = await fetch(`${BASE}/eval/regression?${qs}`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ── Corpus ──────────────────────────────────────────────────────────────────

export interface CorpusStatus {
  total_files: number;
  by_source_type: Record<string, number>;
  rejected_files: number;
  tickers_in_manifest: string[];
  manifest_entries: number;
  vector_store_chunks: number;
}

export interface BackfillRequest {
  tickers: string[];
  start_date: string;
  end_date: string;
  run_tagger?: boolean;
  run_indexer?: boolean;
}

export interface CorpusEvent {
  event: "start" | "backfill_done" | "indexing_start" | "indexing_done" | "error" | "done";
  tickers?: string[];
  written?: number;
  skipped?: number;
  indexed?: number;
  message?: string;
  ts?: number;
}

export async function getCorpusStatus(): Promise<CorpusStatus> {
  const res = await fetch(`${BASE}/corpus/status`);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

export function streamBackfill(
  req: BackfillRequest,
  onEvent: (e: CorpusEvent) => void,
): AbortController {
  const controller = new AbortController();

  fetch(`${BASE}/corpus/backfill`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal: controller.signal,
  }).then(async (res) => {
    if (!res.ok || !res.body) {
      onEvent({ event: "error", message: await res.text() });
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.startsWith("data: ")) {
          try {
            onEvent(JSON.parse(line.slice(6)) as CorpusEvent);
          } catch { /* ignore parse errors */ }
        }
      }
    }
  }).catch((err) => {
    if (err.name !== "AbortError") {
      onEvent({ event: "error", message: String(err) });
    }
  });

  return controller;
}

export async function triggerIndex(): Promise<{ indexed: number }> {
  const res = await fetch(`${BASE}/corpus/index`, { method: "POST" });
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}
