"use client";

import type { NodeEvent } from "@/lib/api";

const NODE_ORDER = [
  "data_pull",
  "rag_retrieval",
  "persona_bull",
  "persona_bear",
  "persona_macro",
  "persona_technicals",
  "judge",
  "guardrails",
  "layer2",
];

type NodeStatus = "pending" | "running" | "done" | "error";

interface NodeState {
  status: NodeStatus;
  elapsed_s?: number;
  output?: Record<string, unknown>;
}

interface Props {
  events: NodeEvent[];
}

function statusIcon(s: NodeStatus) {
  if (s === "done") return <span className="text-success">&#10003;</span>;
  if (s === "running") return <span className="text-accent animate-pulse">&#9679;</span>;
  if (s === "error") return <span className="text-danger">&#10007;</span>;
  return <span className="text-gray-600">&#9675;</span>;
}

function statusColor(s: NodeStatus) {
  if (s === "done") return "border-success/40 bg-success/5";
  if (s === "running") return "border-accent/60 bg-accent/5";
  if (s === "error") return "border-danger/40 bg-danger/5";
  return "border-border bg-card/50";
}

export function NodeTimeline({ events }: Props) {
  const nodeMap: Record<string, NodeState> = {};
  NODE_ORDER.forEach((n) => (nodeMap[n] = { status: "pending" }));

  for (const e of events) {
    if (e.event === "node_start" && e.node) {
      nodeMap[e.node] = { status: "running" };
    } else if (e.event === "node_done" && e.node) {
      nodeMap[e.node] = {
        status: "done",
        elapsed_s: e.elapsed_s,
        output: e.output,
      };
    } else if (e.event === "error") {
      // mark the last running node as errored
      const running = Object.entries(nodeMap).find(([, v]) => v.status === "running");
      if (running) nodeMap[running[0]] = { ...running[1], status: "error" };
    }
  }

  return (
    <div className="space-y-3">
      {NODE_ORDER.map((node) => {
        const state = nodeMap[node];
        return (
          <div
            key={node}
            className={`rounded-lg border p-4 transition-colors ${statusColor(state.status)}`}
          >
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <span className="text-lg w-5 text-center">{statusIcon(state.status)}</span>
                <span className="font-mono text-sm font-medium text-gray-200">{node}</span>
              </div>
              {state.elapsed_s !== undefined && (
                <span className="text-xs text-gray-400">{state.elapsed_s}s</span>
              )}
            </div>

            {state.status === "done" && state.output && (
              <NodeOutput node={node} output={state.output} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function NodeOutput({ node, output }: { node: string; output: Record<string, unknown> }) {
  if (node === "data_pull") {
    return (
      <div className="mt-3 space-y-2 text-sm">
        <div className="text-gray-400">
          Sources: <span className="text-gray-200">{(output.sources_used as string[])?.join(", ") || "—"}</span>
        </div>
        {(output.data_gaps as string[])?.length > 0 && (
          <div className="text-warning text-xs">
            Gaps: {(output.data_gaps as string[]).join(" · ")}
          </div>
        )}
        {output.rendered_text && (
          <pre className="mt-2 max-h-48 overflow-y-auto rounded bg-black/30 p-3 text-xs text-gray-300 whitespace-pre-wrap">
            {output.rendered_text as string}
          </pre>
        )}
      </div>
    );
  }

  if (node === "rag_retrieval") {
    const items = (output.items as Array<Record<string, unknown>>) ?? [];
    return (
      <div className="mt-3 space-y-2 text-sm">
        <div className="flex gap-4 text-gray-400 text-xs">
          <span>Retrieved: <strong className="text-gray-200">{output.chunks_retrieved as number}</strong></span>
          <span>Used: <strong className="text-gray-200">{output.chunks_used as number}</strong></span>
        </div>
        {items.length === 0 ? (
          <div className="text-warning text-xs">No chunks used — qualitative context empty.</div>
        ) : (
          <div className="mt-2 space-y-2 max-h-56 overflow-y-auto pr-1">
            {items.map((item, i) => (
              <div key={i} className="rounded bg-black/20 p-2 text-xs">
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-gray-500">{item.date as string}</span>
                  <span className="text-gray-300 font-medium">{item.source as string}</span>
                  <SentimentBadge label={item.sentiment_label as string} />
                  <span className="ml-auto text-gray-500">{(item.relevance_score as number).toFixed(3)}</span>
                </div>
                <p className="text-gray-300 leading-relaxed">{item.summary as string}</p>
              </div>
            ))}
          </div>
        )}
      </div>
    );
  }

  // Generic JSON view for other nodes
  return (
    <pre className="mt-3 max-h-48 overflow-y-auto rounded bg-black/30 p-3 text-xs text-gray-300 whitespace-pre-wrap">
      {JSON.stringify(output, null, 2)}
    </pre>
  );
}

function SentimentBadge({ label }: { label: string }) {
  const color =
    label === "bullish" ? "bg-success/20 text-success" :
    label === "bearish" ? "bg-danger/20 text-danger" :
    "bg-gray-700 text-gray-400";
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${color}`}>{label}</span>
  );
}
