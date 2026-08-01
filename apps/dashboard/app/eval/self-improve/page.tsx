"use client";

import { useRef, useState } from "react";
import { streamSelfImprove } from "@/lib/api";
import type { SelfImproveEvent } from "@/lib/api";

// ── Default quarters helper ────────────────────────────────────────────────

function defaultQuarters(): string[] {
  const now = new Date();
  const currentQ = Math.floor(now.getMonth() / 3) + 1;
  const currentYear = now.getFullYear();
  const quarters: string[] = [];
  let y = currentYear;
  let q = currentQ - 1;
  if (q === 0) { q = 4; y--; }
  for (let i = 0; i < 4; i++) {
    quarters.push(`${y}-Q${q}`);
    q--;
    if (q === 0) { q = 4; y--; }
  }
  return quarters.reverse();
}

// ── Types ──────────────────────────────────────────────────────────────────

interface IterationResult {
  quarter: string;
  iteration: number;
  elapsed_s: number;
  all_passed: boolean;
  scored_runs: number;
  total_runs: number;
  cancelled_runs: number;
  metrics: Record<string, { value: number; benchmark: number; passed: boolean; gap: number }>;
  driver_miss_rates: Record<string, number>;
  worst_tickers: Record<string, string[]>;
}

interface PatchInfo {
  quarter: string;
  iteration: number;
  reasoning: string;
  patches_this_round: number;
  total_patches: number;
}

interface AlertInfo {
  quarter: string;
  reason: string;
  failing?: string[];
  metric?: string;
  gap?: number;
}

interface QuarterResult {
  quarter: string;
  all_passed: boolean;
  scored_runs: number;
  failing: string[];
}

// ── Page ───────────────────────────────────────────────────────────────────

export default function SelfImprovePage() {
  const [tickerInput, setTickerInput] = useState("AAPL, MSFT, NVDA, TSLA, AMZN");
  const [quarterInput, setQuarterInput] = useState(defaultQuarters().join(", "));
  const [maxIters, setMaxIters] = useState(5);

  const [running, setRunning] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Current status
  const [currentQuarter, setCurrentQuarter] = useState<string | null>(null);
  const [currentIteration, setCurrentIteration] = useState<number | null>(null);

  // Accumulated results
  const [iterations, setIterations] = useState<IterationResult[]>([]);
  const [patches, setPatches] = useState<PatchInfo[]>([]);
  const [alerts, setAlerts] = useState<AlertInfo[]>([]);
  const [quarterResults, setQuarterResults] = useState<QuarterResult[]>([]);
  const [totalPatches, setTotalPatches] = useState(0);

  const controllerRef = useRef<AbortController | null>(null);

  function handleStart() {
    setRunning(true);
    setDone(false);
    setError(null);
    setIterations([]);
    setPatches([]);
    setAlerts([]);
    setQuarterResults([]);
    setTotalPatches(0);
    setCurrentQuarter(null);
    setCurrentIteration(null);

    const tickers = tickerInput
      .split(/[,\s]+/)
      .map((t) => t.trim().toUpperCase())
      .filter(Boolean);
    const quarters = quarterInput
      .split(/[,\s]+/)
      .map((q) => q.trim())
      .filter(Boolean);

    const controller = streamSelfImprove(
      { tickers, quarters, max_iterations: maxIters },
      (e: SelfImproveEvent) => {
        switch (e.event) {
          case "quarter_start":
            setCurrentQuarter(e.quarter ?? null);
            setCurrentIteration(null);
            break;

          case "iteration_start":
            setCurrentIteration(e.iteration ?? null);
            break;

          case "iteration_done":
            setIterations((prev) => [
              ...prev,
              {
                quarter: e.quarter!,
                iteration: e.iteration!,
                elapsed_s: e.elapsed_s!,
                all_passed: e.all_passed!,
                scored_runs: e.scored_runs!,
                total_runs: e.total_runs!,
                cancelled_runs: e.cancelled_runs!,
                metrics: e.metrics!,
                driver_miss_rates: e.driver_miss_rates ?? {},
                worst_tickers: e.worst_tickers ?? {},
              },
            ]);
            break;

          case "patch_applied":
            setPatches((prev) => [
              ...prev,
              {
                quarter: e.quarter!,
                iteration: e.iteration!,
                reasoning: e.reasoning ?? "",
                patches_this_round: e.patches_this_round!,
                total_patches: e.total_patches!,
              },
            ]);
            setTotalPatches(e.total_patches ?? 0);
            break;

          case "alert":
            setAlerts((prev) => [
              ...prev,
              {
                quarter: e.quarter!,
                reason: e.reason!,
                failing: e.failing,
                metric: e.metric,
                gap: e.gap,
              },
            ]);
            break;

          case "quarter_done":
            if (e.result) {
              setQuarterResults((prev) => [
                ...prev,
                { quarter: e.quarter!, ...e.result! },
              ]);
            }
            break;

          case "loop_done":
            setDone(true);
            setRunning(false);
            break;

          case "error":
            setError(e.message ?? "Unknown error");
            setRunning(false);
            setDone(true);
            break;

          case "stream_closed":
          case "timeout":
            setRunning(false);
            setDone(true);
            break;
        }
      },
    );
    controllerRef.current = controller;
  }

  function handleStop() {
    controllerRef.current?.abort();
    setRunning(false);
    setDone(true);
  }

  return (
    <div className="space-y-8 max-w-4xl">
      <div>
        <h1 className="text-xl font-semibold text-white mb-1">
          Self-Improvement Loop
        </h1>
        <p className="text-sm text-gray-400">
          Run the eval loop across quarters. The LLM patches prompts when benchmarks fail.
        </p>
      </div>

      {/* ── Config form ─────────────────────────────────────────────── */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-4">
        <div className="grid grid-cols-2 gap-4">
          <Field label="Tickers (comma-separated)">
            <input
              className={inputClass}
              value={tickerInput}
              onChange={(e) => setTickerInput(e.target.value.toUpperCase())}
              disabled={running}
            />
          </Field>
          <Field label="Quarters (comma-separated)">
            <input
              className={inputClass}
              value={quarterInput}
              onChange={(e) => setQuarterInput(e.target.value)}
              disabled={running}
            />
          </Field>
        </div>
        <div className="flex items-end gap-4">
          <Field label="Max iterations per quarter">
            <input
              type="number"
              className={inputClass + " w-24"}
              value={maxIters}
              onChange={(e) => setMaxIters(Number(e.target.value))}
              min={1}
              max={10}
              disabled={running}
            />
          </Field>
          <div className="flex gap-2">
            <button
              onClick={handleStart}
              disabled={running}
              className="rounded-lg bg-accent px-5 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {running ? "Running..." : "Start Loop"}
            </button>
            {running && (
              <button
                onClick={handleStop}
                className="rounded-lg bg-danger/20 border border-danger/40 px-4 py-2 text-sm font-medium text-danger hover:bg-danger/30 transition-colors"
              >
                Stop
              </button>
            )}
          </div>
        </div>
      </div>

      {/* ── Live status ─────────────────────────────────────────────── */}
      {running && currentQuarter && (
        <div className="flex items-center gap-3 text-sm">
          <span className="h-2 w-2 rounded-full bg-accent animate-pulse" />
          <span className="text-gray-300">
            {currentQuarter}
            {currentIteration != null && ` — iteration ${currentIteration}`}
          </span>
        </div>
      )}

      {error && (
        <div className="rounded-lg border border-danger/30 bg-danger/5 px-4 py-3">
          <p className="text-sm text-danger">{error}</p>
        </div>
      )}

      {/* ── Alerts ──────────────────────────────────────────────────── */}
      {alerts.length > 0 && (
        <div className="space-y-2">
          {alerts.map((a, i) => (
            <div
              key={i}
              className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3"
            >
              <p className="text-xs font-medium text-warning">
                {a.quarter} — {a.reason.replace(/_/g, " ")}
              </p>
              {a.failing && (
                <p className="text-xs text-gray-300 mt-1">
                  Failing: {a.failing.join(", ")}
                </p>
              )}
              {a.metric && a.gap != null && (
                <p className="text-xs text-gray-300 mt-1">
                  {a.metric}: gap = {a.gap.toFixed(4)}
                </p>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── Quarter results summary ─────────────────────────────────── */}
      {quarterResults.length > 0 && (
        <section>
          <h2 className="text-base font-medium text-white mb-3">Quarter Results</h2>
          <div className="flex gap-3 flex-wrap">
            {quarterResults.map((qr) => (
              <div
                key={qr.quarter}
                className={`rounded-lg border px-4 py-3 text-center min-w-[120px] ${
                  qr.all_passed
                    ? "border-success/40 bg-success/5"
                    : "border-danger/40 bg-danger/5"
                }`}
              >
                <p className="text-sm font-medium text-white">{qr.quarter}</p>
                <p
                  className={`text-xs font-medium mt-1 ${
                    qr.all_passed ? "text-success" : "text-danger"
                  }`}
                >
                  {qr.all_passed ? "PASS" : "FAIL"}
                </p>
                <p className="text-xs text-gray-400 mt-0.5">
                  {qr.scored_runs} scored
                </p>
                {qr.failing.length > 0 && (
                  <p className="text-xs text-gray-500 mt-1">
                    {qr.failing.join(", ")}
                  </p>
                )}
              </div>
            ))}
          </div>
          {done && (
            <p className="text-xs text-gray-400 mt-3">
              Total prompt patches applied: {totalPatches}
            </p>
          )}
        </section>
      )}

      {/* ── Iteration timeline ──────────────────────────────────────── */}
      {iterations.length > 0 && (
        <section>
          <h2 className="text-base font-medium text-white mb-3">
            Iteration Timeline
          </h2>
          <div className="space-y-3">
            {iterations.map((it, idx) => {
              const patch = patches.find(
                (p) => p.quarter === it.quarter && p.iteration === it.iteration,
              );
              return (
                <IterationCard key={idx} iteration={it} patch={patch} />
              );
            })}
          </div>
        </section>
      )}
    </div>
  );
}

// ── Iteration card ─────────────────────────────────────────────────────────

function IterationCard({
  iteration: it,
  patch,
}: {
  iteration: IterationResult;
  patch?: PatchInfo;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className={`rounded-lg border p-4 transition-colors ${
        it.all_passed
          ? "border-success/40 bg-success/5"
          : "border-border bg-card/50"
      }`}
    >
      <button
        className="w-full text-left"
        onClick={() => setExpanded(!expanded)}
      >
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={`text-sm ${it.all_passed ? "text-success" : "text-gray-400"}`}>
              {it.all_passed ? "\u2713" : "\u2022"}
            </span>
            <span className="font-mono text-sm text-gray-200">
              {it.quarter} iter {it.iteration}
            </span>
            <span className="text-xs text-gray-500">
              {it.scored_runs}/{it.total_runs} scored
              {it.cancelled_runs > 0 && `, ${it.cancelled_runs} cancelled`}
            </span>
          </div>
          <div className="flex items-center gap-3">
            {patch && (
              <span className="rounded bg-accent/20 px-2 py-0.5 text-xs text-accent">
                {patch.patches_this_round} patch{patch.patches_this_round !== 1 ? "es" : ""}
              </span>
            )}
            <span className="text-xs text-gray-500">{it.elapsed_s}s</span>
            <span className="text-xs text-gray-600">{expanded ? "\u25B2" : "\u25BC"}</span>
          </div>
        </div>

        {/* Mini metric badges */}
        <div className="flex gap-2 mt-2 flex-wrap">
          {Object.entries(it.metrics).map(([name, m]) => (
            <span
              key={name}
              className={`rounded px-2 py-0.5 text-xs ${
                m.passed
                  ? "bg-success/10 text-success"
                  : "bg-danger/10 text-danger"
              }`}
            >
              {name.replace(/_/g, " ")}: {(m.value * (name.includes("error") || name.includes("rate") ? 1 : 100)).toFixed(1)}
              {name.includes("error") || name.includes("price") ? "" : "%"}
            </span>
          ))}
        </div>
      </button>

      {expanded && (
        <div className="mt-4 space-y-4 border-t border-border pt-4">
          {/* Metrics table */}
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border bg-card text-gray-400">
                  <th className="px-3 py-2 text-left">Metric</th>
                  <th className="px-3 py-2 text-left">Value</th>
                  <th className="px-3 py-2 text-left">Benchmark</th>
                  <th className="px-3 py-2 text-left">Gap</th>
                  <th className="px-3 py-2 text-left">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {Object.entries(it.metrics).map(([name, m]) => (
                  <tr key={name}>
                    <td className="px-3 py-2 text-gray-200">{name}</td>
                    <td className="px-3 py-2 font-mono text-gray-300">
                      {m.value.toFixed(4)}
                    </td>
                    <td className="px-3 py-2 font-mono text-gray-500">
                      {m.benchmark}
                    </td>
                    <td className="px-3 py-2 font-mono text-gray-500">
                      {m.gap > 0 ? m.gap.toFixed(4) : "-"}
                    </td>
                    <td className="px-3 py-2">
                      <span className={m.passed ? "text-success" : "text-danger"}>
                        {m.passed ? "PASS" : "FAIL"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Driver miss rates */}
          {Object.keys(it.driver_miss_rates).length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-2">Driver Miss Rates</p>
              <div className="flex gap-2 flex-wrap">
                {Object.entries(it.driver_miss_rates)
                  .sort((a, b) => b[1] - a[1])
                  .map(([driver, rate]) => (
                    <span
                      key={driver}
                      className={`rounded px-2 py-0.5 text-xs ${
                        rate > 0.5
                          ? "bg-danger/10 text-danger"
                          : rate > 0.3
                          ? "bg-warning/10 text-warning"
                          : "bg-gray-800 text-gray-400"
                      }`}
                    >
                      {driver}: {(rate * 100).toFixed(1)}%
                    </span>
                  ))}
              </div>
            </div>
          )}

          {/* Worst tickers */}
          {Object.keys(it.worst_tickers).length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400 mb-2">Worst Tickers</p>
              <div className="space-y-1">
                {Object.entries(it.worst_tickers).map(([ticker, reasons]) => (
                  <p key={ticker} className="text-xs text-gray-300">
                    <span className="font-mono font-medium text-white">{ticker}</span>{" "}
                    {reasons.join(", ")}
                  </p>
                ))}
              </div>
            </div>
          )}

          {/* Patch reasoning */}
          {patch && (
            <div className="rounded-lg border border-accent/20 bg-accent/5 px-4 py-3">
              <p className="text-xs font-medium text-accent mb-1">Prompt Patch Applied</p>
              <p className="text-xs text-gray-300">{patch.reasoning}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-400 mb-1.5">
        {label}
      </label>
      {children}
    </div>
  );
}

const inputClass =
  "w-full rounded-lg border border-border bg-card px-3 py-2 text-sm text-gray-100 placeholder-gray-600 focus:border-accent focus:outline-none disabled:opacity-50";
