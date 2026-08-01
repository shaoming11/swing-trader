/**
 * Tests for lib/api.ts — all fetch-based API functions.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  triggerRun,
  getRun,
  listRuns,
  getCalibration,
  getAttribution,
  getRegression,
  getCorpusStatus,
  triggerIndex,
} from "@/lib/api";

// ── Helpers ─────────────────────────────────────────────────────────────────

function mockFetch(body: unknown, ok = true, status = 200) {
  return vi.fn().mockResolvedValue({
    ok,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
});

// ── triggerRun ───────────────────────────────────────────────────────────────

describe("triggerRun", () => {
  it("POSTs run request and returns run_id", async () => {
    global.fetch = mockFetch({ run_id: "abc-123" });

    const result = await triggerRun({
      ticker: "AAPL",
      window_start: "2024-01-01",
      window_end: "2024-03-31",
      run_type: "live",
    });

    expect(result).toEqual({ run_id: "abc-123" });
    expect(fetch).toHaveBeenCalledWith("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ticker: "AAPL",
        window_start: "2024-01-01",
        window_end: "2024-03-31",
        run_type: "live",
      }),
    });
  });

  it("throws on non-ok response", async () => {
    global.fetch = mockFetch("Bad request", false, 400);
    await expect(triggerRun({
      ticker: "AAPL",
      window_start: "2024-01-01",
      window_end: "2024-03-31",
      run_type: "live",
    })).rejects.toThrow();
  });
});

// ── getRun ───────────────────────────────────────────────────────────────────

describe("getRun", () => {
  it("fetches a single run by ID", async () => {
    const run = { run_id: "abc-123", ticker: "AAPL", direction: "bullish" };
    global.fetch = mockFetch(run);

    const result = await getRun("abc-123");

    expect(result).toEqual(run);
    expect(fetch).toHaveBeenCalledWith("/api/runs/abc-123");
  });

  it("throws on 404", async () => {
    global.fetch = mockFetch("Not found", false, 404);
    await expect(getRun("missing")).rejects.toThrow();
  });
});

// ── listRuns ─────────────────────────────────────────────────────────────────

describe("listRuns", () => {
  it("fetches runs with no params", async () => {
    global.fetch = mockFetch([]);

    await listRuns();

    expect(fetch).toHaveBeenCalledWith("/api/runs?");
  });

  it("includes query params", async () => {
    global.fetch = mockFetch([]);

    await listRuns({ ticker: "TSLA", limit: 10 });

    const url = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain("ticker=TSLA");
    expect(url).toContain("limit=10");
  });

  it("returns RunSummary array", async () => {
    const runs = [
      {
        run_id: "r1",
        ticker: "AAPL",
        window_start: "2024-01-01",
        window_end: "2024-03-31",
        run_type: "live",
        pipeline_version: "0.1.0",
        direction: "bullish",
        magnitude_bucket: "3-8%",
        confidence: 0.72,
        gate_passed: true,
        guardrail_retries: 0,
        pipeline_cancelled: false,
        hit: true,
        completed_at: "2024-04-01T00:00:00Z",
      },
    ];
    global.fetch = mockFetch(runs);

    const result = await listRuns();

    expect(result).toHaveLength(1);
    expect(result[0].ticker).toBe("AAPL");
    expect(result[0].direction).toBe("bullish");
  });
});

// ── Eval endpoints ──────────────────────────────────────────────────────────

describe("getCalibration", () => {
  it("fetches calibration data", async () => {
    const data = { total_records: 50, well_calibrated: true, buckets: [] };
    global.fetch = mockFetch(data);

    const result = await getCalibration({ ticker: "AAPL" });

    expect(result).toEqual(data);
    const url = (fetch as ReturnType<typeof vi.fn>).mock.calls[0][0] as string;
    expect(url).toContain("ticker=AAPL");
  });
});

describe("getAttribution", () => {
  it("fetches attribution data", async () => {
    const data = { driver_miss_rates: [], price_target_error_by_ticker: {} };
    global.fetch = mockFetch(data);

    const result = await getAttribution();

    expect(result).toEqual(data);
  });
});

describe("getRegression", () => {
  it("fetches regression data", async () => {
    const data = { versions: [], timing_distribution: {} };
    global.fetch = mockFetch(data);

    const result = await getRegression();

    expect(result).toEqual(data);
  });
});

// ── Corpus endpoints ────────────────────────────────────────────────────────

describe("getCorpusStatus", () => {
  it("fetches corpus status", async () => {
    const data = {
      total_files: 100,
      by_source_type: { news: 50, analyst: 30 },
      rejected_files: 5,
      tickers_in_manifest: ["AAPL", "TSLA"],
      manifest_entries: 95,
      vector_store_chunks: 200,
    };
    global.fetch = mockFetch(data);

    const result = await getCorpusStatus();

    expect(result.total_files).toBe(100);
    expect(result.tickers_in_manifest).toContain("AAPL");
  });
});

describe("triggerIndex", () => {
  it("POSTs index trigger and returns count", async () => {
    global.fetch = mockFetch({ indexed: 42 });

    const result = await triggerIndex();

    expect(result).toEqual({ indexed: 42 });
    expect(fetch).toHaveBeenCalledWith("/api/corpus/index", { method: "POST" });
  });
});
