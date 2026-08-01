"""Self-improvement loop — iterate on prompts until benchmarks pass.

Flow:
  1. For each quarter, run the pipeline on all watchlist tickers
  2. Fetch ground truth (actual price movement) for each run
  3. Score predictions against reality → QuarterReport
  4. If all benchmarks pass → move to next quarter
  5. If failing → LLM analyzes failures, generates prompt patches, re-run
  6. Max 5 iterations per quarter; if still failing or big gap on new quarter → alert user

Usage:
    python -m swing_trader.eval.self_improve
    python -m swing_trader.eval.self_improve --quarters 2025-Q1 2025-Q2
    python -m swing_trader.eval.self_improve --watchlist watchlist.yaml --max-iters 3
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from swing_trader.eval.benchmarks import QuarterReport, build_quarter_report
from swing_trader.eval.metrics import RunScore, fetch_ground_truth, score_run
from swing_trader.reasoning.prompts import (
    JUDGE_SYSTEM_PROMPT,
    PERSONA_SYSTEM_PROMPTS,
)

log = logging.getLogger("swing_trader.eval.self_improve")

MAX_ITERATIONS = 5
# If a metric is off by more than this on a NEW quarter, alert the user
BIG_GAP_THRESHOLD = 0.20


# ── Quarter date ranges ──────────────────────────────────────────────────────

def quarter_to_dates(q: str) -> tuple[date, date]:
    """Convert '2025-Q1' → (date(2024,12,31), date(2025,3,31)).

    Starts one day before the quarter to satisfy the 90-day minimum window
    (a calendar quarter like Q1 is only 89 days: Jan 1 – Mar 31).
    """
    year, qn = q.split("-Q")
    year = int(year)
    qn = int(qn)
    starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
    ends = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    raw_start = date(year, *starts[qn])
    # Push start back one day so span >= 90 days
    return raw_start - timedelta(days=1), date(year, *ends[qn])


def default_eval_quarters() -> list[str]:
    """Return the last 4 completed quarters."""
    today = date.today()
    current_q = (today.month - 1) // 3 + 1
    current_year = today.year
    quarters = []
    y, q = current_year, current_q - 1
    if q == 0:
        q, y = 4, y - 1
    for _ in range(4):
        quarters.append(f"{y}-Q{q}")
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return list(reversed(quarters))


# ── Run pipeline for a quarter ───────────────────────────────────────────────

async def _run_quarter(
    tickers: list[str],
    window_start: date,
    window_end: date,
    delay_seconds: int = 30,
) -> list[dict]:
    """Run the pipeline for each ticker and return list of state dicts."""
    from main import run_pipeline

    results = []
    for i, ticker in enumerate(tickers):
        try:
            state = await run_pipeline(
                ticker=ticker,
                window_start=window_start,
                window_end=window_end,
                run_type="eval",
            )
            results.append(state)
        except Exception:
            log.exception("Pipeline failed for %s", ticker)
            results.append({"ticker": ticker, "pipeline_cancelled": True})

        # Delay between tickers to avoid rate limits (skip after last)
        if i < len(tickers) - 1:
            log.info("Waiting %ds before next ticker (rate limit cooldown)", delay_seconds)
            await asyncio.sleep(delay_seconds)
    return results


def _score_quarter(
    quarter_label: str,
    states: list[dict],
    tickers: list[str],
    window_start: date,
    window_end: date,
) -> QuarterReport:
    """Fetch ground truth and score all runs for a quarter."""
    scores: list[RunScore] = []
    driver_lists: list[list[str]] = []
    cancelled = 0

    for state, ticker in zip(states, tickers):
        if state.get("pipeline_cancelled") or state.get("layer1_output") is None:
            cancelled += 1
            continue

        l1 = state["layer1_output"]
        gt = fetch_ground_truth(ticker, window_start, window_end)
        run_score = score_run(
            predicted_direction=l1.direction,
            predicted_magnitude=l1.magnitude_bucket,
            predicted_confidence=l1.confidence,
            ground_truth=gt,
        )
        if run_score:
            scores.append(run_score)
            driver_lists.append(list(l1.dominant_drivers))

    return build_quarter_report(
        quarter_label=quarter_label,
        scores=scores,
        total_runs=len(tickers),
        cancelled_runs=cancelled,
        driver_lists=driver_lists,
    )


# ── Prompt rewriter ──────────────────────────────────────────────────────────

REWRITER_SYSTEM_PROMPT = """\
You are a prompt engineering specialist for a swing-trading AI pipeline.

The pipeline has 4 persona prompts (bull, bear, macro, technicals) and 1 judge \
synthesis prompt. You will receive a failure report showing which metrics are \
below benchmark and which tickers/drivers performed worst.

Your job: output a JSON object with targeted prompt patches. Do NOT rewrite \
entire prompts — add 1-2 sentences of emphasis or adjustment to fix the \
specific failures.

Return ONLY valid JSON with this structure:
{
  "reasoning": "<2-3 sentences explaining what pattern you see in the failures>",
  "patches": {
    "bull": "<sentence to APPEND to the bull persona prompt, or null if no change>",
    "bear": "<sentence to APPEND to the bear persona prompt, or null if no change>",
    "macro": "<sentence to APPEND to the macro persona prompt, or null if no change>",
    "technicals": "<sentence to APPEND to the technicals persona prompt, or null if no change>",
    "judge": "<sentence to APPEND to the judge system prompt, or null if no change>"
  }
}

Rules:
- Only patch prompts that are directly related to the failing metrics.
- Each patch must be 1-2 sentences max.
- If direction accuracy is low, focus on making personas more decisive.
- If calibration error is high, tell the judge to be more honest about uncertainty.
- If a specific driver has high miss rate, strengthen that persona's rigor.
- If magnitude accuracy is low, tell the judge to be more conservative with bucket sizing.
- Never remove existing instructions, only add emphasis or nuance.
- Return ONLY the JSON object, no markdown fences.\
"""

REWRITER_USER_TEMPLATE = """\
=== FAILURE REPORT ===
{failure_report}

=== CURRENT PERSONA PROMPTS ===
BULL: {bull_prompt}

BEAR: {bear_prompt}

MACRO: {macro_prompt}

TECHNICALS: {technicals_prompt}

=== CURRENT JUDGE PROMPT (first 500 chars) ===
{judge_prompt_preview}

=== ITERATION ===
This is iteration {iteration} of {max_iterations}. \
Previous patches applied: {previous_patch_count}.

Analyze the failure report and return a JSON patch object.\
"""


async def _generate_prompt_patch(
    report: QuarterReport,
    iteration: int,
    max_iterations: int,
    previous_patch_count: int,
) -> dict | None:
    """Ask the LLM to generate prompt patches based on the failure report."""
    from swing_trader.clients import get_judge_client, LLM_JUDGE_MODEL

    client = get_judge_client()
    user_msg = REWRITER_USER_TEMPLATE.format(
        failure_report=report.failure_report_for_llm(),
        bull_prompt=PERSONA_SYSTEM_PROMPTS["bull"],
        bear_prompt=PERSONA_SYSTEM_PROMPTS["bear"],
        macro_prompt=PERSONA_SYSTEM_PROMPTS["macro"],
        technicals_prompt=PERSONA_SYSTEM_PROMPTS["technicals"],
        judge_prompt_preview=JUDGE_SYSTEM_PROMPT[:500],
        iteration=iteration,
        max_iterations=max_iterations,
        previous_patch_count=previous_patch_count,
    )

    try:
        response = await client.chat.completions.create(
            model=LLM_JUDGE_MODEL,
            messages=[
                {"role": "system", "content": REWRITER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.4,
            max_tokens=800,
        )
        raw = (response.choices[0].message.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            log.warning("Rewriter returned no JSON")
            return None
        return json.loads(raw[start:end])
    except Exception:
        log.exception("Prompt rewriter failed")
        return None


def _apply_patch(patch: dict) -> int:
    """Apply prompt patches in-memory. Returns number of patches applied.

    Mutates PERSONA_SYSTEM_PROMPTS and JUDGE_SYSTEM_PROMPT module-level vars.
    """
    import swing_trader.reasoning.prompts as prompts_module

    patches_applied = 0
    patches_obj = patch.get("patches", {})

    for persona in ("bull", "bear", "macro", "technicals"):
        addition = patches_obj.get(persona)
        if addition and isinstance(addition, str):
            current = prompts_module.PERSONA_SYSTEM_PROMPTS[persona]
            prompts_module.PERSONA_SYSTEM_PROMPTS[persona] = current + " " + addition
            patches_applied += 1
            log.info("Patched %s persona prompt: +%d chars", persona, len(addition))

    judge_addition = patches_obj.get("judge")
    if judge_addition and isinstance(judge_addition, str):
        prompts_module.JUDGE_SYSTEM_PROMPT = (
            prompts_module.JUDGE_SYSTEM_PROMPT + "\n" + judge_addition
        )
        patches_applied += 1
        log.info("Patched judge prompt: +%d chars", len(judge_addition))

    return patches_applied


def _save_prompts_snapshot(quarter: str, iteration: int) -> Path:
    """Save current prompt state to disk for audit trail."""
    import swing_trader.reasoning.prompts as prompts_module

    out_dir = Path("eval_runs") / quarter
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"prompts_iter{iteration}.json"

    snapshot = {
        "quarter": quarter,
        "iteration": iteration,
        "persona_prompts": dict(prompts_module.PERSONA_SYSTEM_PROMPTS),
        "judge_system_prompt": prompts_module.JUDGE_SYSTEM_PROMPT,
    }
    path.write_text(json.dumps(snapshot, indent=2))
    log.info("Saved prompt snapshot to %s", path)
    return path


def _save_report(quarter: str, iteration: int, report: QuarterReport) -> Path:
    """Save quarter report to disk."""
    out_dir = Path("eval_runs") / quarter
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"report_iter{iteration}.txt"
    path.write_text(report.summary())
    return path


# ── Main loop ────────────────────────────────────────────────────────────────

async def run_eval_loop(
    tickers: list[str],
    quarters: list[str],
    max_iterations: int = MAX_ITERATIONS,
) -> dict[str, QuarterReport]:
    """Run the self-improvement loop across quarters.

    For each quarter:
      1. Run pipeline on all tickers
      2. Score against ground truth
      3. If passing → move on
      4. If failing → LLM patches prompts, re-run (up to max_iterations)
      5. If still failing → log warning and move on
    """
    all_reports: dict[str, QuarterReport] = {}
    total_patches = 0
    first_quarter = True

    for quarter in quarters:
        window_start, window_end = quarter_to_dates(quarter)
        log.info("=" * 60)
        log.info("QUARTER: %s  (%s to %s)", quarter, window_start, window_end)
        log.info("=" * 60)

        best_report: QuarterReport | None = None

        for iteration in range(1, max_iterations + 1):
            log.info("--- Iteration %d/%d ---", iteration, max_iterations)
            t0 = time.monotonic()

            # Save current prompts before running
            _save_prompts_snapshot(quarter, iteration)

            # Run pipeline
            states = await _run_quarter(tickers, window_start, window_end)

            # Score
            report = _score_quarter(quarter, states, tickers, window_start, window_end)
            _save_report(quarter, iteration, report)

            elapsed = time.monotonic() - t0
            log.info("Iteration %d done in %.0fs", iteration, elapsed)
            print(report.summary())

            best_report = report

            if report.all_passed:
                log.info("ALL BENCHMARKS PASSED for %s on iteration %d", quarter, iteration)
                break

            if iteration == max_iterations:
                log.warning(
                    "EXHAUSTED %d iterations for %s — still failing: %s",
                    max_iterations, quarter,
                    [m.name for m in report.failing_metrics],
                )
                _alert_user(quarter, report, reason="max_iterations_exhausted")
                break

            # Check for big gap on non-first quarter (prompt may not generalize)
            if not first_quarter and iteration == 1:
                worst = report.worst_metric
                if worst and worst.gap > BIG_GAP_THRESHOLD:
                    log.warning(
                        "BIG GAP on new quarter %s: %s gap=%.3f — alerting user",
                        quarter, worst.name, worst.gap,
                    )
                    _alert_user(quarter, report, reason="big_gap_new_quarter")
                    # Still try to self-improve, but user is informed

            # Generate and apply prompt patch
            log.info("Generating prompt patch...")
            patch = await _generate_prompt_patch(
                report, iteration, max_iterations, total_patches,
            )
            if patch:
                reasoning = patch.get("reasoning", "(no reasoning)")
                log.info("Rewriter reasoning: %s", reasoning)
                applied = _apply_patch(patch)
                total_patches += applied
                log.info("Applied %d patches (total across all quarters: %d)",
                         applied, total_patches)
            else:
                log.warning("Rewriter returned no patch — retrying with same prompts")

        all_reports[quarter] = best_report
        first_quarter = False

    # Final summary
    print("\n" + "=" * 60)
    print("EVAL LOOP COMPLETE")
    print("=" * 60)
    for q, report in all_reports.items():
        status = "PASS" if report.all_passed else "FAIL"
        failing = [m.name for m in report.failing_metrics]
        print(f"  {q}: {status}  scored={report.scored_runs}"
              + (f"  failing={failing}" if failing else ""))
    print(f"\nTotal prompt patches applied: {total_patches}")

    # Save final prompts
    _save_prompts_snapshot("final", 0)

    return all_reports


def _alert_user(quarter: str, report: QuarterReport, reason: str) -> None:
    """Print a prominent alert for the user to manually review prompts."""
    print("\n" + "!" * 60)
    print(f"  ACTION REQUIRED — {reason}")
    print(f"  Quarter: {quarter}")
    print(f"  Failing metrics:")
    for m in report.failing_metrics:
        print(f"    {m.name}: {m.value:.3f} (need {m.benchmark})")
    if report.driver_miss_rates:
        worst_driver = max(report.driver_miss_rates, key=report.driver_miss_rates.get)
        print(f"  Worst driver: {worst_driver} ({report.driver_miss_rates[worst_driver]:.1%} miss)")
    print(f"\n  Review prompt snapshots in: eval_runs/{quarter}/")
    print(f"  Edit prompts in: src/swing_trader/reasoning/prompts.py")
    print("!" * 60 + "\n")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Eval self-improvement loop")
    parser.add_argument(
        "--quarters", nargs="+", default=None,
        help="Quarters to eval, e.g. 2025-Q1 2025-Q2 (default: last 4 completed)",
    )
    parser.add_argument(
        "--watchlist", default="watchlist.yaml",
        help="Path to watchlist YAML",
    )
    parser.add_argument(
        "--max-iters", type=int, default=MAX_ITERATIONS,
        help=f"Max iterations per quarter (default: {MAX_ITERATIONS})",
    )
    parser.add_argument(
        "--tickers", nargs="+", default=None,
        help="Override tickers (instead of watchlist)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    quarters = args.quarters or default_eval_quarters()

    if args.tickers:
        tickers = [t.upper() for t in args.tickers]
    else:
        from swing_trader.batch import load_watchlist
        wl = load_watchlist(args.watchlist)
        tickers = wl["tickers"]

    log.info("Eval loop starting: quarters=%s  tickers=%s  max_iters=%d",
             quarters, tickers, args.max_iters)

    asyncio.run(run_eval_loop(tickers, quarters, args.max_iters))


if __name__ == "__main__":
    main()
