"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { triggerRun } from "@/lib/api";

// Window constraints — must match src/swing_trader/window.py
const MIN_WINDOW_DAYS = 90;
const MIN_RECENCY_DAYS = 45;
const MAX_WINDOW_DAYS = 365;

function lastSafeQuarter(): { start: string; end: string } {
  const now = new Date();
  const currentQ = Math.floor(now.getMonth() / 3); // 0-indexed quarter

  // Walk back quarters until the end date passes the recency check
  for (let i = 1; i <= 4; i++) {
    let q = currentQ - i;
    let year = now.getFullYear();
    while (q < 0) { q += 4; year--; }
    const startMonth = q * 3; // 0-indexed
    const endMonth = startMonth + 2;
    const start = new Date(year, startMonth, 1);
    const end = new Date(year, endMonth + 1, 1); // first day after quarter
    const daysAgo = Math.round((Date.now() - end.getTime()) / 864e5);
    if (daysAgo >= MIN_RECENCY_DAYS) {
      return {
        start: start.toISOString().slice(0, 10),
        end: end.toISOString().slice(0, 10),
      };
    }
  }

  // Fallback: two quarters ago (always safe)
  const fallbackQ = currentQ - 2 < 0 ? currentQ - 2 + 4 : currentQ - 2;
  const fallbackYear = currentQ - 2 < 0 ? now.getFullYear() - 1 : now.getFullYear();
  const s = new Date(fallbackYear, fallbackQ * 3, 1);
  const e = new Date(fallbackYear, fallbackQ * 3 + 3, 1);
  return { start: s.toISOString().slice(0, 10), end: e.toISOString().slice(0, 10) };
}

function validateWindow(startStr: string, endStr: string): string | null {
  const start = new Date(startStr);
  const end = new Date(endStr);
  if (isNaN(start.getTime()) || isNaN(end.getTime())) return "Invalid dates.";
  if (start >= end) return "Start must be before end.";

  const span = Math.round((end.getTime() - start.getTime()) / 864e5);
  if (span < MIN_WINDOW_DAYS)
    return `Window is ${span} days — minimum is ${MIN_WINDOW_DAYS}. A full quarter is needed to guarantee GDP and FOMC observations.`;
  if (span > MAX_WINDOW_DAYS)
    return `Window is ${span} days — maximum is ${MAX_WINDOW_DAYS}.`;

  const daysAgo = Math.round((Date.now() - end.getTime()) / 864e5);
  if (daysAgo < MIN_RECENCY_DAYS) {
    const safeEnd = new Date(Date.now() - MIN_RECENCY_DAYS * 864e5).toISOString().slice(0, 10);
    return `End date is too recent — FRED monthly data has a 2-6 week release lag. Use end date ≤ ${safeEnd}, or leave blank for the last completed quarter.`;
  }

  return null;
}

const defaults = lastSafeQuarter();

export default function HomePage() {
  const router = useRouter();
  const [form, setForm] = useState({
    ticker: "AAPL",
    window_start: defaults.start,
    window_end: defaults.end,
    run_type: "live" as "live" | "backfill" | "eval",
    thesis_hint: "",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    const windowErr = validateWindow(form.window_start, form.window_end);
    if (windowErr) {
      setError(windowErr);
      return;
    }

    setLoading(true);
    try {
      const { run_id } = await triggerRun({
        ...form,
        thesis_hint: form.thesis_hint || undefined,
      });
      router.push(`/runs/${run_id}`);
    } catch (err) {
      setError(String(err));
      setLoading(false);
    }
  }

  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-semibold text-white mb-1">New Pipeline Run</h1>
      <p className="text-gray-400 text-sm mb-8">
        Trigger a run and watch each node execute in real time.
      </p>

      <form onSubmit={handleSubmit} className="space-y-5">
        <Field label="Ticker">
          <input
            className={input}
            value={form.ticker}
            onChange={(e) => setForm({ ...form, ticker: e.target.value.toUpperCase() })}
            placeholder="AAPL"
            required
          />
        </Field>

        <div className="grid grid-cols-2 gap-4">
          <Field label="Window Start">
            <input
              type="date"
              className={input}
              value={form.window_start}
              onChange={(e) => setForm({ ...form, window_start: e.target.value })}
              required
            />
          </Field>
          <Field label="Window End">
            <input
              type="date"
              className={input}
              value={form.window_end}
              onChange={(e) => setForm({ ...form, window_end: e.target.value })}
              required
            />
          </Field>
        </div>

        <Field label="Run Type">
          <select
            className={input}
            value={form.run_type}
            onChange={(e) => setForm({ ...form, run_type: e.target.value as "live" | "backfill" | "eval" })}
          >
            <option value="live">live</option>
            <option value="backfill">backfill</option>
            <option value="eval">eval</option>
          </select>
        </Field>

        <Field label="Thesis Hint (optional)">
          <input
            className={input}
            value={form.thesis_hint}
            onChange={(e) => setForm({ ...form, thesis_hint: e.target.value })}
            placeholder="e.g. earnings beat catalyst"
          />
        </Field>

        {error && (
          <p className="text-sm text-danger bg-danger/10 rounded-lg px-4 py-3">{error}</p>
        )}

        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          {loading ? "Starting…" : "Run Pipeline"}
        </button>
      </form>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-400 mb-1.5">{label}</label>
      {children}
    </div>
  );
}

const input =
  "w-full rounded-lg border border-border bg-card px-3 py-2 text-sm text-gray-100 placeholder-gray-600 focus:border-accent focus:outline-none";
