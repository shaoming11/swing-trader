"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { listRuns } from "@/lib/api";
import type { RunSummary } from "@/lib/api";

export default function RunsPage() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await listRuns({ ticker: ticker || undefined, limit: 100 });
      setRuns(data);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-white">Run History</h1>
        <div className="flex gap-2">
          <input
            className="rounded-lg border border-border bg-card px-3 py-1.5 text-sm text-gray-100 placeholder-gray-600 focus:border-accent focus:outline-none"
            placeholder="Filter by ticker"
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && load()}
          />
          <button
            onClick={load}
            className="rounded-lg bg-card border border-border px-3 py-1.5 text-sm text-gray-300 hover:border-accent hover:text-white transition-colors"
          >
            Search
          </button>
        </div>
      </div>

      {error && <p className="text-sm text-danger">{error}</p>}

      {loading ? (
        <p className="text-sm text-gray-500 animate-pulse">Loading…</p>
      ) : runs.length === 0 ? (
        <p className="text-sm text-gray-500">No runs found.</p>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-border">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border bg-card text-xs text-gray-400">
                <Th>Ticker</Th>
                <Th>Window</Th>
                <Th>Direction</Th>
                <Th>Confidence</Th>
                <Th>Gate</Th>
                <Th>Retries</Th>
                <Th>Hit</Th>
                <Th>Completed</Th>
                <Th></Th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {runs.map((r) => (
                <tr key={r.run_id} className="hover:bg-card/60 transition-colors">
                  <Td><span className="font-mono font-medium text-white">{r.ticker}</span></Td>
                  <Td className="text-gray-400 text-xs">{r.window_start} → {r.window_end}</Td>
                  <Td><DirectionBadge direction={r.direction} /></Td>
                  <Td>
                    {r.confidence != null ? (
                      <span className={r.confidence >= 0.7 ? "text-success" : r.confidence >= 0.5 ? "text-warning" : "text-danger"}>
                        {(r.confidence * 100).toFixed(0)}%
                      </span>
                    ) : "—"}
                  </Td>
                  <Td>
                    {r.gate_passed == null ? "—" : r.gate_passed ? (
                      <span className="text-success text-xs">acted</span>
                    ) : (
                      <span className="text-gray-500 text-xs">skipped</span>
                    )}
                  </Td>
                  <Td>
                    <span className={r.guardrail_retries > 0 ? "text-warning" : "text-gray-500"}>
                      {r.guardrail_retries}
                    </span>
                  </Td>
                  <Td>
                    {r.hit == null ? "—" : r.hit ? (
                      <span className="text-success">&#10003;</span>
                    ) : (
                      <span className="text-danger">&#10007;</span>
                    )}
                  </Td>
                  <Td className="text-gray-500 text-xs">
                    {r.completed_at ? new Date(r.completed_at).toLocaleString() : "—"}
                  </Td>
                  <Td>
                    <Link
                      href={`/runs/${r.run_id}`}
                      className="text-accent text-xs hover:underline"
                    >
                      View
                    </Link>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-4 py-3 text-left font-medium">{children}</th>;
}

function Td({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <td className={`px-4 py-3 ${className}`}>{children}</td>;
}

function DirectionBadge({ direction }: { direction: string | null }) {
  if (!direction) return <span className="text-gray-600">—</span>;
  const colors: Record<string, string> = {
    bullish: "text-success",
    bearish: "text-danger",
    neutral: "text-gray-400",
  };
  return <span className={`text-xs font-medium ${colors[direction] ?? "text-gray-400"}`}>{direction}</span>;
}
