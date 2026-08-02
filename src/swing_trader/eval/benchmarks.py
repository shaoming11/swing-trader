"""Benchmark definitions and quarter-level scoring.

Aggregates per-run scores into a QuarterReport, checks each metric against
its benchmark threshold, and produces a structured failure report that the
self-improvement loop can feed to the LLM prompt rewriter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swing_trader.eval.metrics import RunScore


# ── Benchmark thresholds ─────────────────────────────────────────────────────
# Each is (metric_name, threshold, comparison).  "gte" = score must be >= threshold.

BENCHMARKS: dict[str, tuple[float, str]] = {
    "direction_accuracy": (0.55, "gte"),  # >= 55%
    "magnitude_accuracy": (0.35, "gte"),  # >= 35%
    "calibration_error": (0.15, "lte"),  # <= 15pp avg drift
    "high_confidence_precision": (0.65, "gte"),  # >= 65% hit rate when conf >= 0.7
    "cancellation_rate": (0.20, "lte"),  # <= 20%
    "mean_price_target_error": (12.0, "lte"),  # <= 12pp
}


# ── Quarter report ───────────────────────────────────────────────────────────


@dataclass
class MetricResult:
    name: str
    value: float
    benchmark: float
    comparison: str  # gte | lte
    passed: bool

    @property
    def gap(self) -> float:
        """How far off from benchmark (positive = failing by this much)."""
        if self.comparison == "gte":
            return max(0, self.benchmark - self.value)
        return max(0, self.value - self.benchmark)

    def summary(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        op = ">=" if self.comparison == "gte" else "<="
        return f"[{status}] {self.name}: {self.value:.3f} (benchmark {op} {self.benchmark})"


@dataclass
class QuarterReport:
    """Aggregated eval for one quarter across all tickers."""

    quarter_label: str  # e.g. "2025-Q1"
    total_runs: int
    scored_runs: int
    cancelled_runs: int
    metrics: list[MetricResult] = field(default_factory=list)

    # Per-ticker breakdown for the LLM to analyze
    ticker_failures: dict[str, list[str]] = field(default_factory=dict)
    # Per-driver miss rates
    driver_miss_rates: dict[str, float] = field(default_factory=dict)

    @property
    def all_passed(self) -> bool:
        if self.scored_runs == 0:
            return False  # no data = not passing
        return all(m.passed for m in self.metrics)

    @property
    def failing_metrics(self) -> list[MetricResult]:
        return [m for m in self.metrics if not m.passed]

    @property
    def worst_metric(self) -> MetricResult | None:
        failing = self.failing_metrics
        if not failing:
            return None
        return max(failing, key=lambda m: m.gap)

    def summary(self) -> str:
        lines = [
            f"=== {self.quarter_label} — {self.scored_runs}/{self.total_runs} scored ===",
        ]
        for m in self.metrics:
            lines.append(f"  {m.summary()}")

        if self.ticker_failures:
            lines.append("\n  Worst tickers:")
            for ticker, reasons in sorted(
                self.ticker_failures.items(),
                key=lambda x: len(x[1]),
                reverse=True,
            )[:5]:
                lines.append(f"    {ticker}: {', '.join(reasons)}")

        if self.driver_miss_rates:
            lines.append("\n  Driver miss rates:")
            for driver, rate in sorted(
                self.driver_miss_rates.items(), key=lambda x: x[1], reverse=True
            ):
                lines.append(f"    {driver}: {rate:.1%}")

        return "\n".join(lines)

    def failure_report_for_llm(self) -> str:
        """Structured failure report the LLM prompt rewriter will consume."""
        if self.all_passed:
            return "ALL BENCHMARKS PASSED — no changes needed."

        lines = [
            f"QUARTER: {self.quarter_label}",
            f"SCORED RUNS: {self.scored_runs}",
            "",
            "FAILING METRICS:",
        ]
        for m in self.failing_metrics:
            lines.append(
                f"  - {m.name}: got {m.value:.3f}, need {m.benchmark} "
                f"({'at least' if m.comparison == 'gte' else 'at most'}), "
                f"gap = {m.gap:.3f}"
            )

        if self.driver_miss_rates:
            lines.append("\nDRIVER MISS RATES (higher = worse):")
            for driver, rate in sorted(
                self.driver_miss_rates.items(), key=lambda x: x[1], reverse=True
            ):
                lines.append(f"  - {driver}: {rate:.1%}")

        if self.ticker_failures:
            lines.append("\nWORST TICKERS:")
            for ticker, reasons in sorted(
                self.ticker_failures.items(),
                key=lambda x: len(x[1]),
                reverse=True,
            )[:8]:
                lines.append(f"  - {ticker}: {', '.join(reasons)}")

        return "\n".join(lines)


# ── Scoring aggregation ──────────────────────────────────────────────────────


def build_quarter_report(
    quarter_label: str,
    scores: list[RunScore],
    total_runs: int,
    cancelled_runs: int,
    driver_lists: list[list[str]] | None = None,
) -> QuarterReport:
    """Aggregate per-run scores into a QuarterReport with benchmark checks."""
    n = len(scores)
    if n == 0:
        return QuarterReport(
            quarter_label=quarter_label,
            total_runs=total_runs,
            scored_runs=0,
            cancelled_runs=cancelled_runs,
        )

    # 1. Direction accuracy
    dir_acc = sum(s.direction_correct for s in scores) / n

    # 2. Magnitude accuracy
    mag_acc = sum(s.magnitude_correct for s in scores) / n

    # 3. Calibration error — bin by confidence, compare to actual hit rate
    cal_error = _calibration_error(scores)

    # 4. High-confidence precision
    hc = [s for s in scores if s.high_confidence]
    hc_prec = (sum(s.hit for s in hc) / len(hc)) if hc else 1.0  # pass if none are high-conf

    # 5. Cancellation rate
    cancel_rate = cancelled_runs / total_runs if total_runs > 0 else 0.0

    # 6. Mean price target error
    mean_pte = sum(s.price_target_error_pct for s in scores) / n

    # Build metric results
    raw = {
        "direction_accuracy": dir_acc,
        "magnitude_accuracy": mag_acc,
        "calibration_error": cal_error,
        "high_confidence_precision": hc_prec,
        "cancellation_rate": cancel_rate,
        "mean_price_target_error": mean_pte,
    }
    metrics = []
    for name, (threshold, cmp) in BENCHMARKS.items():
        val = raw[name]
        passed = val >= threshold if cmp == "gte" else val <= threshold
        metrics.append(
            MetricResult(name=name, value=val, benchmark=threshold, comparison=cmp, passed=passed)
        )

    # Per-ticker failure breakdown
    ticker_failures: dict[str, list[str]] = {}
    for s in scores:
        reasons = []
        if not s.direction_correct:
            reasons.append(f"dir:{s.predicted_direction}->actual:{s.actual_direction}")
        if not s.magnitude_correct:
            reasons.append(f"mag:{s.predicted_magnitude}->actual:{s.actual_magnitude}")
        if s.price_target_error_pct > 12:
            reasons.append(f"pte:{s.price_target_error_pct:.1f}%")
        if reasons:
            ticker_failures[s.ticker] = reasons

    # Per-driver miss rates
    driver_miss: dict[str, list[bool]] = {}
    if driver_lists:
        for drivers, score in zip(driver_lists, scores):
            for d in drivers:
                driver_miss.setdefault(d, []).append(not score.hit)
    driver_miss_rates = {
        d: sum(misses) / len(misses) for d, misses in driver_miss.items() if misses
    }

    return QuarterReport(
        quarter_label=quarter_label,
        total_runs=total_runs,
        scored_runs=n,
        cancelled_runs=cancelled_runs,
        metrics=metrics,
        ticker_failures=ticker_failures,
        driver_miss_rates=driver_miss_rates,
    )


def _calibration_error(scores: list[RunScore]) -> float:
    """Mean absolute calibration error across confidence bins."""
    bins: dict[str, list[RunScore]] = {
        "0.0-0.4": [],
        "0.4-0.6": [],
        "0.6-0.8": [],
        "0.8-1.0": [],
    }
    midpoints = {"0.0-0.4": 0.2, "0.4-0.6": 0.5, "0.6-0.8": 0.7, "0.8-1.0": 0.9}

    for s in scores:
        c = s.predicted_confidence
        if c < 0.4:
            bins["0.0-0.4"].append(s)
        elif c < 0.6:
            bins["0.4-0.6"].append(s)
        elif c < 0.8:
            bins["0.6-0.8"].append(s)
        else:
            bins["0.8-1.0"].append(s)

    drifts = []
    for label, group in bins.items():
        if len(group) < 3:  # need minimum sample
            continue
        actual_hit_rate = sum(s.hit for s in group) / len(group)
        drifts.append(abs(actual_hit_rate - midpoints[label]))

    return sum(drifts) / len(drifts) if drifts else 0.0
