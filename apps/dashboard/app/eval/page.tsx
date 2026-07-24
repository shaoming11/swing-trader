"use client";

import { useEffect, useState } from "react";
import { getCalibration, getAttribution, getRegression } from "@/lib/api";

export default function EvalPage() {
  const [calibration, setCalibration] = useState<CalibrationData | null>(null);
  const [attribution, setAttribution] = useState<AttributionData | null>(null);
  const [regression, setRegression] = useState<RegressionData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([getCalibration(), getAttribution(), getRegression()])
      .then(([cal, attr, reg]) => {
        setCalibration(cal as CalibrationData);
        setAttribution(attr as AttributionData);
        setRegression(reg as RegressionData);
      })
      .catch((err) => setError(String(err)))
      .finally(() => setLoading(false));
  }, []);

  if (loading) return <p className="text-sm text-gray-500 animate-pulse">Loading eval data…</p>;
  if (error) return <p className="text-sm text-danger">{error}</p>;

  return (
    <div className="space-y-10">
      <div>
        <h1 className="text-xl font-semibold text-white mb-1">Eval Harness</h1>
        <p className="text-sm text-gray-400">
          {calibration?.records_with_ground_truth ?? 0} runs with ground truth out of {calibration?.total_records ?? 0} total.
        </p>
      </div>

      {/* Calibration */}
      <section>
        <h2 className="text-base font-medium text-white mb-4">Confidence Calibration</h2>
        {calibration && <CalibrationTable data={calibration} />}
      </section>

      {/* Attribution */}
      <section>
        <h2 className="text-base font-medium text-white mb-4">Driver Miss Rates</h2>
        {attribution && <AttributionTable data={attribution} />}
      </section>

      {/* Regression */}
      <section>
        <h2 className="text-base font-medium text-white mb-4">Version Regression</h2>
        {regression && <RegressionTable data={regression} />}
      </section>
    </div>
  );
}

// ── Types ─────────────────────────────────────────────────────────────────────

interface CalibrationBucket {
  label: string;
  confidence_range: string;
  total: number;
  correct: number;
  hit_rate: number | null;
  sample_size_ok: boolean;
}

interface CalibrationData {
  total_records: number;
  records_with_ground_truth: number;
  well_calibrated: boolean;
  corrective_actions: string[];
  buckets: CalibrationBucket[];
}

interface DriverMissRate {
  driver: string;
  total_calls: number;
  misses: number;
  miss_rate: number;
}

interface AttributionData {
  driver_miss_rates: DriverMissRate[];
  price_target_error_by_ticker: Record<string, { count: number; mean_abs_error: number; median_abs_error: number }>;
}

interface VersionRow {
  pipeline_version: string;
  runs: number;
  hit_rate_pct: number | null;
  avg_confidence_pct: number | null;
  avg_retries: number;
  cancellation_rate_pct: number;
  vs_baseline?: { hit_rate_delta_pp: number | null; regression_detected: boolean };
}

interface RegressionData {
  versions: VersionRow[];
  timing_distribution: Record<string, number>;
}

// ── Calibration Table ─────────────────────────────────────────────────────────

function CalibrationTable({ data }: { data: CalibrationData }) {
  return (
    <div className="space-y-4">
      <div className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium ${
        data.well_calibrated ? "bg-success/20 text-success" : "bg-warning/20 text-warning"
      }`}>
        {data.well_calibrated ? "Well calibrated" : "Miscalibrated — see actions below"}
      </div>

      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-card text-xs text-gray-400">
              <Th>Bucket</Th>
              <Th>Stated range</Th>
              <Th>Samples</Th>
              <Th>Hit rate</Th>
              <Th>Delta</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data.buckets.map((b) => {
              const midpoint = b.hit_rate != null
                ? parseFloat(b.confidence_range.split("–")[0]) / 100 + 0.075
                : null;
              const delta = b.hit_rate != null && midpoint != null
                ? (b.hit_rate / 100 - midpoint) * 100
                : null;
              return (
                <tr key={b.label} className="hover:bg-card/60">
                  <Td className="font-medium text-white">{b.label}</Td>
                  <Td className="text-gray-400 text-xs">{b.confidence_range}</Td>
                  <Td className={!b.sample_size_ok ? "text-gray-600" : "text-gray-200"}>{b.total}</Td>
                  <Td>
                    {b.hit_rate != null ? (
                      <span className={Math.abs(b.hit_rate / 100 - (midpoint ?? 0)) > 0.10 ? "text-warning" : "text-success"}>
                        {b.hit_rate}%
                      </span>
                    ) : <span className="text-gray-600">—</span>}
                  </Td>
                  <Td>
                    {delta != null ? (
                      <span className={Math.abs(delta) > 10 ? "text-warning" : "text-gray-400"}>
                        {delta > 0 ? "+" : ""}{delta.toFixed(1)}pp
                      </span>
                    ) : "—"}
                  </Td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {data.corrective_actions.length > 0 && (
        <ul className="space-y-1">
          {data.corrective_actions.map((a, i) => (
            <li key={i} className="text-xs text-warning bg-warning/5 border border-warning/20 rounded px-3 py-2">
              {a}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Attribution Table ─────────────────────────────────────────────────────────

function AttributionTable({ data }: { data: AttributionData }) {
  return (
    <div className="space-y-6">
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-card text-xs text-gray-400">
              <Th>Driver</Th>
              <Th>Total calls</Th>
              <Th>Misses</Th>
              <Th>Miss rate</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data.driver_miss_rates.map((d) => (
              <tr key={d.driver} className="hover:bg-card/60">
                <Td className="font-medium text-white">{d.driver}</Td>
                <Td className="text-gray-400">{d.total_calls}</Td>
                <Td className="text-gray-400">{d.misses}</Td>
                <Td>
                  <MissRateBar rate={d.miss_rate} />
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {Object.keys(data.price_target_error_by_ticker).length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-300 mb-3">Price Target Error by Ticker</h3>
          <div className="overflow-x-auto rounded-xl border border-border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-card text-xs text-gray-400">
                  <Th>Ticker</Th><Th>Count</Th><Th>Mean abs error</Th><Th>Median abs error</Th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {Object.entries(data.price_target_error_by_ticker).map(([ticker, stats]) => (
                  <tr key={ticker} className="hover:bg-card/60">
                    <Td className="font-mono font-medium text-white">{ticker}</Td>
                    <Td className="text-gray-400">{stats.count}</Td>
                    <Td className={stats.mean_abs_error > 10 ? "text-danger" : "text-gray-200"}>{stats.mean_abs_error}%</Td>
                    <Td className="text-gray-400">{stats.median_abs_error}%</Td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function MissRateBar({ rate }: { rate: number }) {
  const color = rate > 0.5 ? "bg-danger" : rate > 0.3 ? "bg-warning" : "bg-success";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 bg-gray-800 rounded-full h-1.5 max-w-24">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(rate * 100, 100)}%` }} />
      </div>
      <span className={rate > 0.5 ? "text-danger" : rate > 0.3 ? "text-warning" : "text-success"}>
        {(rate * 100).toFixed(1)}%
      </span>
    </div>
  );
}

// ── Regression Table ──────────────────────────────────────────────────────────

function RegressionTable({ data }: { data: RegressionData }) {
  return (
    <div className="space-y-6">
      <div className="overflow-x-auto rounded-xl border border-border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-border bg-card text-xs text-gray-400">
              <Th>Version</Th><Th>Runs</Th><Th>Hit Rate</Th><Th>Avg Conf</Th>
              <Th>Avg Retries</Th><Th>Cancel Rate</Th><Th>vs Baseline</Th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data.versions.map((v) => (
              <tr key={v.pipeline_version} className={`hover:bg-card/60 ${v.vs_baseline?.regression_detected ? "bg-danger/5" : ""}`}>
                <Td className="font-mono text-white text-xs">{v.pipeline_version}</Td>
                <Td className="text-gray-400">{v.runs}</Td>
                <Td>{v.hit_rate_pct != null ? `${v.hit_rate_pct}%` : "—"}</Td>
                <Td>{v.avg_confidence_pct != null ? `${v.avg_confidence_pct}%` : "—"}</Td>
                <Td className={v.avg_retries > 0.5 ? "text-warning" : "text-gray-400"}>{v.avg_retries}</Td>
                <Td className={v.cancellation_rate_pct > 10 ? "text-danger" : "text-gray-400"}>{v.cancellation_rate_pct}%</Td>
                <Td>
                  {v.vs_baseline ? (
                    <span className={v.vs_baseline.regression_detected ? "text-danger font-medium" : "text-gray-400"}>
                      {v.vs_baseline.hit_rate_delta_pp != null
                        ? `${v.vs_baseline.hit_rate_delta_pp > 0 ? "+" : ""}${v.vs_baseline.hit_rate_delta_pp}pp`
                        : "—"}
                      {v.vs_baseline.regression_detected && " ⚠"}
                    </span>
                  ) : <span className="text-gray-600">baseline</span>}
                </Td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {Object.keys(data.timing_distribution).length > 0 && (
        <div>
          <h3 className="text-sm font-medium text-gray-300 mb-3">Timing Distribution</h3>
          <div className="flex gap-4">
            {Object.entries(data.timing_distribution).map(([k, v]) => (
              <div key={k} className="rounded-lg border border-border bg-card px-4 py-3 text-center">
                <p className="text-xl font-semibold text-white">{v}</p>
                <p className="text-xs text-gray-400 mt-0.5">{k}</p>
              </div>
            ))}
          </div>
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
