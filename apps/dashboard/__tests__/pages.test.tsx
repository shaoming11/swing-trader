/**
 * Tests for all dashboard pages — Home, Runs, Run Detail, Eval, Corpus.
 *
 * Each page is rendered with mocked API calls and Next.js navigation stubs.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

// ── Mock next/navigation ────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: mockPush }),
  useParams: () => ({ runId: "test-run-123" }),
}));

// ── Mock lib/api ────────────────────────────────────────────────────────────

vi.mock("@/lib/api", () => ({
  triggerRun: vi.fn(),
  getRun: vi.fn(),
  listRuns: vi.fn(),
  streamRun: vi.fn(),
  getCalibration: vi.fn(),
  getAttribution: vi.fn(),
  getRegression: vi.fn(),
  getCorpusStatus: vi.fn(),
  streamBackfill: vi.fn(),
  triggerIndex: vi.fn(),
}));

import {
  triggerRun,
  listRuns,
  streamRun,
  getCalibration,
  getAttribution,
  getRegression,
  getCorpusStatus,
} from "@/lib/api";

beforeEach(() => {
  vi.clearAllMocks();
  mockPush.mockClear();
});

// ── Home Page ───────────────────────────────────────────────────────────────

describe("HomePage", () => {
  it("renders the run form with default values", async () => {
    const { default: HomePage } = await import("@/app/page");
    render(<HomePage />);

    expect(screen.getByText("New Pipeline Run")).toBeInTheDocument();
    expect(screen.getByText("Run Pipeline")).toBeInTheDocument();
    expect(screen.getByDisplayValue("AAPL")).toBeInTheDocument();
  });

  it("submits the form and navigates to run page", async () => {
    vi.mocked(triggerRun).mockResolvedValue({ run_id: "new-run-456" });
    const { default: HomePage } = await import("@/app/page");
    render(<HomePage />);

    fireEvent.click(screen.getByText("Run Pipeline"));

    await waitFor(() => {
      expect(triggerRun).toHaveBeenCalled();
      expect(mockPush).toHaveBeenCalledWith("/runs/new-run-456");
    });
  });

  it("shows validation error for too-short window", async () => {
    const { default: HomePage } = await import("@/app/page");
    render(<HomePage />);

    // Set dates only 30 days apart
    const allDateInputs = screen.getAllByDisplayValue(/\d{4}-\d{2}-\d{2}/);

    fireEvent.change(allDateInputs[0], { target: { value: "2024-01-01" } });
    fireEvent.change(allDateInputs[1], { target: { value: "2024-01-30" } });

    fireEvent.click(screen.getByText("Run Pipeline"));

    await waitFor(() => {
      expect(screen.getByText(/minimum is 90/)).toBeInTheDocument();
    });

    expect(triggerRun).not.toHaveBeenCalled();
  });

  it("shows API error on trigger failure", async () => {
    vi.mocked(triggerRun).mockRejectedValue(new Error("Server error"));
    const { default: HomePage } = await import("@/app/page");
    render(<HomePage />);

    fireEvent.click(screen.getByText("Run Pipeline"));

    await waitFor(() => {
      expect(screen.getByText(/Server error/)).toBeInTheDocument();
    });
  });

  it("uppercases ticker input", async () => {
    const { default: HomePage } = await import("@/app/page");
    render(<HomePage />);

    const tickerInput = screen.getByDisplayValue("AAPL");
    fireEvent.change(tickerInput, { target: { value: "tsla" } });

    expect(screen.getByDisplayValue("TSLA")).toBeInTheDocument();
  });
});

// ── Runs List Page ──────────────────────────────────────────────────────────

describe("RunsPage", () => {
  it("renders run history with data", async () => {
    vi.mocked(listRuns).mockResolvedValue([
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
        completed_at: "2024-04-01T12:00:00Z",
      },
      {
        run_id: "r2",
        ticker: "TSLA",
        window_start: "2024-01-01",
        window_end: "2024-03-31",
        run_type: "eval",
        pipeline_version: "0.1.0",
        direction: "bearish",
        magnitude_bucket: "0-3%",
        confidence: 0.45,
        gate_passed: false,
        guardrail_retries: 1,
        pipeline_cancelled: false,
        hit: false,
        completed_at: "2024-04-02T12:00:00Z",
      },
    ]);

    const { default: RunsPage } = await import("@/app/runs/page");
    render(<RunsPage />);

    await waitFor(() => {
      expect(screen.getByText("AAPL")).toBeInTheDocument();
      expect(screen.getByText("TSLA")).toBeInTheDocument();
    });

    expect(screen.getByText("bullish")).toBeInTheDocument();
    expect(screen.getByText("bearish")).toBeInTheDocument();
    expect(screen.getByText("72%")).toBeInTheDocument();
    expect(screen.getByText("45%")).toBeInTheDocument();
  });

  it("shows empty state when no runs", async () => {
    vi.mocked(listRuns).mockResolvedValue([]);

    const { default: RunsPage } = await import("@/app/runs/page");
    render(<RunsPage />);

    await waitFor(() => {
      expect(screen.getByText("No runs found.")).toBeInTheDocument();
    });
  });

  it("shows error state on API failure", async () => {
    vi.mocked(listRuns).mockRejectedValue(new Error("Network error"));

    const { default: RunsPage } = await import("@/app/runs/page");
    render(<RunsPage />);

    await waitFor(() => {
      expect(screen.getByText(/Network error/)).toBeInTheDocument();
    });
  });
});

// ── Run Detail Page ─────────────────────────────────────────────────────────

describe("RunPage (detail)", () => {
  it("renders run ID and streams events", async () => {
    let capturedCallback: ((e: any) => void) | null = null;
    vi.mocked(streamRun).mockImplementation((_runId, onEvent) => {
      capturedCallback = onEvent;
      return () => {};
    });

    const { default: RunPage } = await import("@/app/runs/[runId]/page");
    render(<RunPage />);

    expect(screen.getByText("test-run-123")).toBeInTheDocument();
    expect(screen.getByText(/Running/)).toBeInTheDocument();
    expect(streamRun).toHaveBeenCalledWith("test-run-123", expect.any(Function));
  });

  it("shows Done status when pipeline completes", async () => {
    let capturedCallback: ((e: any) => void) | null = null;
    vi.mocked(streamRun).mockImplementation((_runId, onEvent) => {
      capturedCallback = onEvent;
      // Simulate immediate pipeline completion
      setTimeout(() => {
        onEvent({ event: "node_start", node: "data_pull" });
        onEvent({ event: "node_done", node: "data_pull", elapsed_s: 2.0, output: {} });
        onEvent({ event: "pipeline_done", warnings: [] });
      }, 0);
      return () => {};
    });

    const { default: RunPage } = await import("@/app/runs/[runId]/page");
    render(<RunPage />);

    await waitFor(() => {
      expect(screen.getByText("Done")).toBeInTheDocument();
    });
  });

  it("shows error status and message on pipeline error", async () => {
    vi.mocked(streamRun).mockImplementation((_runId, onEvent) => {
      setTimeout(() => {
        onEvent({ event: "node_start", node: "data_pull" });
        onEvent({ event: "error", message: "FRED API rate limited" });
      }, 0);
      return () => {};
    });

    const { default: RunPage } = await import("@/app/runs/[runId]/page");
    render(<RunPage />);

    await waitFor(() => {
      expect(screen.getByText("Error")).toBeInTheDocument();
      expect(screen.getByText("FRED API rate limited")).toBeInTheDocument();
    });
  });

  it("shows warnings from pipeline_done event", async () => {
    vi.mocked(streamRun).mockImplementation((_runId, onEvent) => {
      setTimeout(() => {
        onEvent({
          event: "pipeline_done",
          warnings: ["rag_retrieval: no relevant chunks found"],
        });
      }, 0);
      return () => {};
    });

    const { default: RunPage } = await import("@/app/runs/[runId]/page");
    render(<RunPage />);

    await waitFor(() => {
      expect(screen.getByText("rag_retrieval: no relevant chunks found")).toBeInTheDocument();
    });
  });
});

// ── Eval Page ───────────────────────────────────────────────────────────────

describe("EvalPage", () => {
  it("renders calibration, attribution, and regression data", async () => {
    vi.mocked(getCalibration).mockResolvedValue({
      total_records: 50,
      records_with_ground_truth: 30,
      well_calibrated: true,
      corrective_actions: [],
      buckets: [
        {
          label: "Low",
          confidence_range: "0–30%",
          total: 10,
          correct: 3,
          hit_rate: 30,
          sample_size_ok: true,
        },
        {
          label: "Medium",
          confidence_range: "30–60%",
          total: 15,
          correct: 8,
          hit_rate: 53,
          sample_size_ok: true,
        },
      ],
    });

    vi.mocked(getAttribution).mockResolvedValue({
      driver_miss_rates: [
        { driver: "fundamental", total_calls: 20, misses: 4, miss_rate: 0.2 },
        { driver: "macro", total_calls: 18, misses: 6, miss_rate: 0.33 },
      ],
      price_target_error_by_ticker: {},
    });

    vi.mocked(getRegression).mockResolvedValue({
      versions: [
        {
          pipeline_version: "0.1.0",
          runs: 25,
          hit_rate_pct: 60,
          avg_confidence_pct: 55,
          avg_retries: 0.2,
          cancellation_rate_pct: 4,
        },
      ],
      timing_distribution: {},
    });

    const { default: EvalPage } = await import("@/app/eval/page");
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getByText("Eval Harness")).toBeInTheDocument();
      expect(screen.getByText("30 runs with ground truth out of 50 total.")).toBeInTheDocument();
    });

    // Calibration
    expect(screen.getByText("Well calibrated")).toBeInTheDocument();
    expect(screen.getByText("Low")).toBeInTheDocument();
    expect(screen.getByText("Medium")).toBeInTheDocument();

    // Attribution
    expect(screen.getByText("fundamental")).toBeInTheDocument();
    expect(screen.getByText("macro")).toBeInTheDocument();

    // Regression
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
  });

  it("shows miscalibrated badge and corrective actions", async () => {
    vi.mocked(getCalibration).mockResolvedValue({
      total_records: 100,
      records_with_ground_truth: 80,
      well_calibrated: false,
      corrective_actions: ["Reduce confidence for neutral predictions"],
      buckets: [],
    });
    vi.mocked(getAttribution).mockResolvedValue({
      driver_miss_rates: [],
      price_target_error_by_ticker: {},
    });
    vi.mocked(getRegression).mockResolvedValue({
      versions: [],
      timing_distribution: {},
    });

    const { default: EvalPage } = await import("@/app/eval/page");
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getByText(/Miscalibrated/)).toBeInTheDocument();
      expect(screen.getByText(/Reduce confidence/)).toBeInTheDocument();
    });
  });

  it("shows error state", async () => {
    vi.mocked(getCalibration).mockRejectedValue(new Error("DB down"));
    vi.mocked(getAttribution).mockRejectedValue(new Error("DB down"));
    vi.mocked(getRegression).mockRejectedValue(new Error("DB down"));

    const { default: EvalPage } = await import("@/app/eval/page");
    render(<EvalPage />);

    await waitFor(() => {
      expect(screen.getByText(/DB down/)).toBeInTheDocument();
    });
  });
});

// ── Corpus Page ─────────────────────────────────────────────────────────────

describe("CorpusPage", () => {
  it("renders corpus status and backfill form", async () => {
    vi.mocked(getCorpusStatus).mockResolvedValue({
      total_files: 250,
      by_source_type: { news: 120, analyst: 80, macro: 30, social: 20 },
      rejected_files: 15,
      tickers_in_manifest: ["AAPL", "TSLA", "NVDA"],
      manifest_entries: 235,
      vector_store_chunks: 500,
    });

    const { default: CorpusPage } = await import("@/app/corpus/page");
    render(<CorpusPage />);

    await waitFor(() => {
      expect(screen.getByText("Corpus Generator")).toBeInTheDocument();
      expect(screen.getByText("250")).toBeInTheDocument();
      expect(screen.getByText("500")).toBeInTheDocument();
    });

    // Source type breakdown
    expect(screen.getByText("120")).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();

    // Preset groups
    expect(screen.getByText("+ Mag 7")).toBeInTheDocument();
    expect(screen.getByText("+ Semis")).toBeInTheDocument();
  });

  it("adds tickers from preset groups", async () => {
    vi.mocked(getCorpusStatus).mockResolvedValue({
      total_files: 0,
      by_source_type: {},
      rejected_files: 0,
      tickers_in_manifest: [],
      manifest_entries: 0,
      vector_store_chunks: 0,
    });

    const { default: CorpusPage } = await import("@/app/corpus/page");
    render(<CorpusPage />);

    await waitFor(() => {
      expect(screen.getByText("+ Mag 7")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("+ Mag 7"));

    // Should show Mag 7 tickers as chips
    await waitFor(() => {
      expect(screen.getByText("AAPL")).toBeInTheDocument();
      expect(screen.getByText("NVDA")).toBeInTheDocument();
      expect(screen.getByText("TSLA")).toBeInTheDocument();
    });
  });

  it("shows error when corpus status fails", async () => {
    vi.mocked(getCorpusStatus).mockRejectedValue(new Error("API unreachable"));

    const { default: CorpusPage } = await import("@/app/corpus/page");
    render(<CorpusPage />);

    await waitFor(() => {
      expect(screen.getByText(/API unreachable/)).toBeInTheDocument();
    });
  });
});
