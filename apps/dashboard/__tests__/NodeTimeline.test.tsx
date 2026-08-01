/**
 * Tests for NodeTimeline component — renders pipeline node status from SSE events.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { NodeTimeline } from "@/components/NodeTimeline";
import type { NodeEvent } from "@/lib/api";

describe("NodeTimeline", () => {
  it("renders all pipeline nodes in pending state when no events", () => {
    render(<NodeTimeline events={[]} />);

    expect(screen.getByText("data_pull")).toBeInTheDocument();
    expect(screen.getByText("rag_retrieval")).toBeInTheDocument();
    expect(screen.getByText("persona_bull")).toBeInTheDocument();
    expect(screen.getByText("persona_bear")).toBeInTheDocument();
    expect(screen.getByText("persona_macro")).toBeInTheDocument();
    expect(screen.getByText("persona_technicals")).toBeInTheDocument();
    expect(screen.getByText("judge")).toBeInTheDocument();
    expect(screen.getByText("guardrails")).toBeInTheDocument();
    expect(screen.getByText("layer2")).toBeInTheDocument();
  });

  it("shows running state for active node", () => {
    const events: NodeEvent[] = [
      { event: "node_start", node: "data_pull" },
    ];
    const { container } = render(<NodeTimeline events={events} />);

    // The running node should have the pulsing indicator
    const pulsingElements = container.querySelectorAll(".animate-pulse");
    expect(pulsingElements.length).toBeGreaterThan(0);
  });

  it("shows done state with elapsed time", () => {
    const events: NodeEvent[] = [
      { event: "node_start", node: "data_pull" },
      { event: "node_done", node: "data_pull", elapsed_s: 2.5, output: { sources_used: ["yfinance", "FRED"] } },
    ];
    render(<NodeTimeline events={events} />);

    expect(screen.getByText("2.5s")).toBeInTheDocument();
  });

  it("renders data_pull output with sources", () => {
    const events: NodeEvent[] = [
      {
        event: "node_done",
        node: "data_pull",
        elapsed_s: 3.1,
        output: {
          sources_used: ["yfinance", "FRED"],
          data_gaps: [],
          rendered_text: "# AAPL — 2024Q1",
        },
      },
    ];
    render(<NodeTimeline events={events} />);

    expect(screen.getByText("yfinance, FRED")).toBeInTheDocument();
  });

  it("renders data_pull data gaps as warnings", () => {
    const events: NodeEvent[] = [
      {
        event: "node_done",
        node: "data_pull",
        elapsed_s: 2.0,
        output: {
          sources_used: ["yfinance"],
          data_gaps: ["FRED API unavailable"],
        },
      },
    ];
    render(<NodeTimeline events={events} />);

    expect(screen.getByText(/FRED API unavailable/)).toBeInTheDocument();
  });

  it("renders rag_retrieval output with chunk counts", () => {
    const events: NodeEvent[] = [
      {
        event: "node_done",
        node: "rag_retrieval",
        elapsed_s: 1.8,
        output: {
          chunks_retrieved: 12,
          chunks_used: 4,
          items: [
            {
              date: "2024-02-02",
              source: "Morgan Stanley",
              sentiment_label: "bullish",
              relevance_score: 0.92,
              summary: "PT raised to $220.",
            },
          ],
        },
      },
    ];
    render(<NodeTimeline events={events} />);

    expect(screen.getByText("12")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument();
    expect(screen.getByText("Morgan Stanley")).toBeInTheDocument();
    expect(screen.getByText("bullish")).toBeInTheDocument();
    expect(screen.getByText("PT raised to $220.")).toBeInTheDocument();
  });

  it("renders rag_retrieval empty state", () => {
    const events: NodeEvent[] = [
      {
        event: "node_done",
        node: "rag_retrieval",
        elapsed_s: 0.5,
        output: {
          chunks_retrieved: 0,
          chunks_used: 0,
          items: [],
        },
      },
    ];
    render(<NodeTimeline events={events} />);

    expect(screen.getByText(/No chunks used/)).toBeInTheDocument();
  });

  it("renders generic JSON for other nodes", () => {
    const events: NodeEvent[] = [
      {
        event: "node_done",
        node: "judge",
        elapsed_s: 4.2,
        output: { direction: "bullish", confidence: 0.72 },
      },
    ];
    render(<NodeTimeline events={events} />);

    expect(screen.getByText(/bullish/)).toBeInTheDocument();
    expect(screen.getByText("4.2s")).toBeInTheDocument();
  });

  it("marks error state on error event", () => {
    const events: NodeEvent[] = [
      { event: "node_start", node: "data_pull" },
      { event: "error", message: "API timeout" },
    ];
    const { container } = render(<NodeTimeline events={events} />);

    // Error node should show X mark
    const errorMarks = container.querySelectorAll(".text-danger");
    expect(errorMarks.length).toBeGreaterThan(0);
  });

  it("handles full pipeline sequence", () => {
    const events: NodeEvent[] = [
      { event: "node_start", node: "data_pull" },
      { event: "node_done", node: "data_pull", elapsed_s: 3.0, output: { sources_used: ["yfinance"] } },
      { event: "node_start", node: "rag_retrieval" },
      { event: "node_done", node: "rag_retrieval", elapsed_s: 1.5, output: { chunks_retrieved: 8, chunks_used: 3, items: [] } },
      { event: "node_start", node: "persona_bull" },
      { event: "node_start", node: "persona_bear" },
      { event: "node_start", node: "persona_macro" },
      { event: "node_start", node: "persona_technicals" },
      { event: "node_done", node: "persona_bull", elapsed_s: 2.0, output: {} },
      { event: "node_done", node: "persona_bear", elapsed_s: 2.1, output: {} },
      { event: "node_done", node: "persona_macro", elapsed_s: 1.8, output: {} },
      { event: "node_done", node: "persona_technicals", elapsed_s: 1.9, output: {} },
      { event: "node_start", node: "judge" },
      { event: "node_done", node: "judge", elapsed_s: 4.5, output: { direction: "bullish" } },
      { event: "node_start", node: "guardrails" },
      { event: "node_done", node: "guardrails", elapsed_s: 0.1, output: {} },
      { event: "node_start", node: "layer2" },
      { event: "node_done", node: "layer2", elapsed_s: 0.2, output: {} },
      { event: "pipeline_done" },
    ];
    render(<NodeTimeline events={events} />);

    // All nodes should show done (check marks present)
    expect(screen.getByText("3s")).toBeInTheDocument();
    expect(screen.getByText("1.5s")).toBeInTheDocument();
    expect(screen.getByText("4.5s")).toBeInTheDocument();
  });
});
