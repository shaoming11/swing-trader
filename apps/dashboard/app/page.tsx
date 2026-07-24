"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { triggerRun } from "@/lib/api";

const today = new Date().toISOString().slice(0, 10);
const thirtyDaysAgo = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10);

export default function HomePage() {
  const router = useRouter();
  const [form, setForm] = useState({
    ticker: "AAPL",
    window_start: thirtyDaysAgo,
    window_end: today,
    run_type: "live" as "live" | "backfill" | "eval",
    thesis_hint: "",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
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
