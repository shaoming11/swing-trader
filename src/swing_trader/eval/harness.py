"""Eval harness — calibration curves, feature attribution, regression comparison.

Designed to run against fixture data before the live pipeline exists.
All functions accept plain dicts or the eval store's Postgres records.

Typical usage during development:
    from swing_trader.eval.harness import calibration_curve, feature_attribution
    runs = load_golden_set("datasets/golden_set/entries/")
    print(calibration_curve(runs))
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


# ── Data types ────────────────────────────────────────────────────────────────


@dataclass
class EvalRecord:
    """Minimal representation of a pipeline_runs row for eval purposes."""

    run_id: str
    ticker: str
    pipeline_version: str
    confidence: float
    direction: str  # bullish | bearish | neutral
    magnitude_bucket: str
    dominant_drivers: list[str]
    guardrail_retries: int = 0
    pipeline_cancelled: bool = False
    # Ground truth — None if not yet populated
    actual_direction: str | None = None
    actual_magnitude_pct: float | None = None
    hit: bool | None = None
    price_target_error_pct: float | None = None
    timing_result: str | None = None

    @property
    def has_ground_truth(self) -> bool:
        return self.hit is not None

    @classmethod
    def from_dict(cls, d: dict) -> "EvalRecord":
        return cls(
            run_id=str(d.get("run_id", "")),
            ticker=str(d.get("ticker", "")),
            pipeline_version=str(d.get("pipeline_version", "0.0.0")),
            confidence=float(d.get("confidence") or 0),
            direction=str(d.get("direction") or "neutral"),
            magnitude_bucket=str(d.get("magnitude_bucket") or "0-3%"),
            dominant_drivers=list(d.get("dominant_drivers") or []),
            guardrail_retries=int(d.get("guardrail_retries") or 0),
            pipeline_cancelled=bool(d.get("pipeline_cancelled")),
            actual_direction=d.get("actual_direction"),
            actual_magnitude_pct=d.get("actual_magnitude_pct"),
            hit=d.get("hit"),
            price_target_error_pct=d.get("price_target_error_pct"),
            timing_result=d.get("timing_result"),
        )


@dataclass
class CalibrationBucket:
    label: str
    confidence_min: float
    confidence_max: float
    total: int = 0
    correct: int = 0

    @property
    def hit_rate(self) -> float | None:
        return (self.correct / self.total) if self.total > 0 else None

    @property
    def sample_size_ok(self) -> bool:
        return self.total >= 10


@dataclass
class CalibrationCurve:
    buckets: list[CalibrationBucket]
    min_samples_per_bucket: int = 10

    def is_well_calibrated(self, tolerance: float = 0.10) -> bool:
        """Returns True if all populated buckets are within tolerance of their midpoint."""
        for b in self.buckets:
            if not b.sample_size_ok or b.hit_rate is None:
                continue
            midpoint = (b.confidence_min + b.confidence_max) / 2
            if abs(b.hit_rate - midpoint) > tolerance:
                return False
        return True

    def corrective_actions(self) -> list[str]:
        """Generate natural-language corrective actions for miscalibrated buckets."""
        actions = []
        for b in self.buckets:
            if not b.sample_size_ok or b.hit_rate is None:
                continue
            midpoint = (b.confidence_min + b.confidence_max) / 2
            drift = b.hit_rate - midpoint
            if drift < -0.10:
                actions.append(
                    f"Bucket {b.label}: stated {midpoint:.0%} but actual {b.hit_rate:.0%} "
                    f"— model is OVERCONFIDENT; reduce confidence scores in this range"
                )
            elif drift > 0.10:
                actions.append(
                    f"Bucket {b.label}: stated {midpoint:.0%} but actual {b.hit_rate:.0%} "
                    f"— model is UNDERCONFIDENT; increase confidence scores in this range"
                )
        return actions

    def to_dict(self) -> dict:
        return {
            "buckets": [
                {
                    "label": b.label,
                    "confidence_range": f"{b.confidence_min:.0%}–{b.confidence_max:.0%}",
                    "total": b.total,
                    "correct": b.correct,
                    "hit_rate": round(b.hit_rate * 100, 1) if b.hit_rate is not None else None,
                    "sample_size_ok": b.sample_size_ok,
                }
                for b in self.buckets
            ],
            "well_calibrated": self.is_well_calibrated(),
            "corrective_actions": self.corrective_actions(),
        }


@dataclass
class FeatureAttribution:
    driver: str
    total_calls: int
    misses: int

    @property
    def miss_rate(self) -> float:
        return (self.misses / self.total_calls) if self.total_calls > 0 else 0.0


@dataclass
class RegressionReport:
    pipeline_version: str
    runs: int
    hit_rate_pct: float | None
    avg_confidence_pct: float | None
    avg_retries: float
    cancellation_rate_pct: float

    def vs(self, baseline: "RegressionReport") -> dict:
        """Compare this version against a baseline."""

        def _diff(a, b):
            if a is None or b is None:
                return None
            return round(a - b, 1)

        return {
            "pipeline_version": self.pipeline_version,
            "baseline_version": baseline.pipeline_version,
            "hit_rate_delta_pp": _diff(self.hit_rate_pct, baseline.hit_rate_pct),
            "confidence_delta_pp": _diff(self.avg_confidence_pct, baseline.avg_confidence_pct),
            "retries_delta": round(self.avg_retries - baseline.avg_retries, 2),
            "cancellation_delta_pp": round(
                self.cancellation_rate_pct - baseline.cancellation_rate_pct, 1
            ),
            "regression_detected": (
                (self.hit_rate_pct or 0) < (baseline.hit_rate_pct or 0) - 5.0
                or self.cancellation_rate_pct > baseline.cancellation_rate_pct + 10.0
            ),
        }


# ── Eval functions ────────────────────────────────────────────────────────────


def calibration_curve(records: list[EvalRecord]) -> CalibrationCurve:
    """Compute the calibration curve from a list of eval records.

    Requires records with ground truth populated (hit is not None).
    """
    bucket_defs = [
        CalibrationBucket("90%+", 0.85, 1.00),
        CalibrationBucket("70-85%", 0.65, 0.85),
        CalibrationBucket("50-65%", 0.45, 0.65),
        CalibrationBucket("<50%", 0.00, 0.45),
    ]

    grounded = [r for r in records if r.has_ground_truth and not r.pipeline_cancelled]
    for r in grounded:
        for bucket in bucket_defs:
            if bucket.confidence_min <= r.confidence < bucket.confidence_max or (
                r.confidence >= 1.0 and bucket.confidence_max == 1.0
            ):
                bucket.total += 1
                if r.hit:
                    bucket.correct += 1
                break

    return CalibrationCurve(buckets=bucket_defs)


def feature_attribution(records: list[EvalRecord]) -> list[FeatureAttribution]:
    """Compute per-driver miss rates.

    Returns drivers sorted by miss rate descending — highest miss rate first
    tells you which input type is most associated with wrong calls.
    """
    driver_total: dict[str, int] = defaultdict(int)
    driver_misses: dict[str, int] = defaultdict(int)

    grounded = [r for r in records if r.has_ground_truth and not r.pipeline_cancelled]
    for r in grounded:
        for driver in r.dominant_drivers:
            driver_total[driver] += 1
            if not r.hit:
                driver_misses[driver] += 1

    result = [
        FeatureAttribution(
            driver=driver,
            total_calls=total,
            misses=driver_misses.get(driver, 0),
        )
        for driver, total in driver_total.items()
    ]
    return sorted(result, key=lambda x: x.miss_rate, reverse=True)


def price_target_error_by_ticker(records: list[EvalRecord]) -> dict[str, dict]:
    """Compute mean and median absolute price target error per ticker."""
    by_ticker: dict[str, list[float]] = defaultdict(list)
    for r in records:
        if r.price_target_error_pct is not None and not r.pipeline_cancelled:
            by_ticker[r.ticker].append(abs(r.price_target_error_pct))

    result = {}
    for ticker, errors in by_ticker.items():
        errors_sorted = sorted(errors)
        n = len(errors_sorted)
        result[ticker] = {
            "count": n,
            "mean_abs_error": round(sum(errors) / n, 1),
            "median_abs_error": round(
                errors_sorted[n // 2]
                if n % 2
                else (errors_sorted[n // 2 - 1] + errors_sorted[n // 2]) / 2,
                1,
            ),
        }

    return dict(sorted(result.items(), key=lambda x: x[1]["mean_abs_error"], reverse=True))


def timing_distribution(records: list[EvalRecord]) -> dict[str, int]:
    """Count timing outcomes: early | on-time | late | never."""
    dist: dict[str, int] = defaultdict(int)
    for r in records:
        if r.timing_result and not r.pipeline_cancelled:
            dist[r.timing_result] += 1
    return dict(dist)


def regression_comparison(records: list[EvalRecord]) -> list[dict]:
    """Compare pipeline versions by hit rate, confidence, retries, cancellation rate.

    Returns a list of version summaries sorted by pipeline_version descending.
    Marks regression if hit rate drops > 5pp or cancellation rate rises > 10pp.
    """
    by_version: dict[str, list[EvalRecord]] = defaultdict(list)
    for r in records:
        if r.has_ground_truth:
            by_version[r.pipeline_version].append(r)

    reports = []
    for version, runs in sorted(by_version.items(), reverse=True):
        grounded = [r for r in runs if r.hit is not None]
        n = len(grounded)
        reports.append(
            RegressionReport(
                pipeline_version=version,
                runs=n,
                hit_rate_pct=round(sum(r.hit for r in grounded if r.hit) / n * 100, 1)
                if n
                else None,
                avg_confidence_pct=round(sum(r.confidence for r in grounded) / n * 100, 1)
                if n
                else None,
                avg_retries=round(sum(r.guardrail_retries for r in runs) / len(runs), 2),
                cancellation_rate_pct=round(
                    sum(r.pipeline_cancelled for r in runs) / len(runs) * 100, 1
                ),
            )
        )

    if len(reports) < 2:
        return [_report_to_dict(r) for r in reports]

    output = []
    baseline = reports[-1]
    for r in reports:
        d = _report_to_dict(r)
        if r.pipeline_version != baseline.pipeline_version:
            d["vs_baseline"] = r.vs(baseline)
        output.append(d)
    return output


def _report_to_dict(r: RegressionReport) -> dict:
    return {
        "pipeline_version": r.pipeline_version,
        "runs": r.runs,
        "hit_rate_pct": r.hit_rate_pct,
        "avg_confidence_pct": r.avg_confidence_pct,
        "avg_retries": r.avg_retries,
        "cancellation_rate_pct": r.cancellation_rate_pct,
    }


# ── Retrieval eval ────────────────────────────────────────────────────────────


@dataclass
class RetrievalEvalEntry:
    """One query from the retrieval eval dataset."""

    ticker: str
    window_start: str
    window_end: str
    relevant_file_paths: list[str]  # manually labeled relevant files
    irrelevant_file_paths: list[str]  # manually labeled irrelevant noise


@dataclass
class RetrievalMetrics:
    recall_at_10: float
    precision_at_10: float
    mrr: float
    empty_block_rate: float
    total_queries: int

    def passes(self) -> bool:
        return (
            self.recall_at_10 >= 0.80
            and self.precision_at_10 >= 0.60
            and self.mrr >= 0.70
            and self.empty_block_rate <= 0.05
        )

    def to_dict(self) -> dict:
        return {
            "recall_at_10": round(self.recall_at_10, 3),
            "precision_at_10": round(self.precision_at_10, 3),
            "mrr": round(self.mrr, 3),
            "empty_block_rate": round(self.empty_block_rate, 3),
            "total_queries": self.total_queries,
            "passes_targets": self.passes(),
        }


async def run_retrieval_eval(
    eval_entries: list[RetrievalEvalEntry],
) -> RetrievalMetrics:
    """Run retrieval eval against a set of labeled entries.

    For each entry, calls the retriever and compares returned file_paths
    against the manually labeled relevant set.
    """
    from datetime import date as date_type
    from swing_trader.rag.retriever import retrieve

    recalls, precisions, reciprocal_ranks = [], [], []
    empty_count = 0

    for entry in eval_entries:
        ws = date_type.fromisoformat(entry.window_start)
        we = date_type.fromisoformat(entry.window_end)
        block = await retrieve(entry.ticker, ws, we)

        if block.chunks_used == 0:
            empty_count += 1
            recalls.append(0.0)
            precisions.append(0.0)
            reciprocal_ranks.append(0.0)
            continue

        returned_paths = []
        for item in block.items:
            # QualItem doesn't carry file_path; we'd need to extend the pipeline
            # for eval. For now, match on source + date as a proxy.
            returned_paths.append(f"{item.date}_{item.source}")

        relevant_set = set(entry.relevant_file_paths)
        returned_set = set(returned_paths)
        hits = returned_set & relevant_set

        recall = len(hits) / len(relevant_set) if relevant_set else 0.0
        precision = len(hits) / len(returned_set) if returned_set else 0.0
        recalls.append(recall)
        precisions.append(precision)

        # MRR: rank of first relevant result
        rr = 0.0
        for i, path in enumerate(returned_paths):
            if path in relevant_set:
                rr = 1.0 / (i + 1)
                break
        reciprocal_ranks.append(rr)

    n = len(eval_entries)
    return RetrievalMetrics(
        recall_at_10=sum(recalls) / n if n else 0.0,
        precision_at_10=sum(precisions) / n if n else 0.0,
        mrr=sum(reciprocal_ranks) / n if n else 0.0,
        empty_block_rate=empty_count / n if n else 0.0,
        total_queries=n,
    )


# ── Golden set loader ─────────────────────────────────────────────────────────


def load_golden_set(directory: str | Path) -> list[EvalRecord]:
    """Load all golden set JSON entries from a directory into EvalRecord objects."""
    path = Path(directory)
    records = []
    for f in sorted(path.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            records.append(EvalRecord.from_dict(data))
        except Exception as e:
            print(f"[harness] failed to load {f}: {e}")
    return records
