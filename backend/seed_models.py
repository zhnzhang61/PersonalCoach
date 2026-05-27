"""Seed / refit stat-derived models from raw data.

PR P1 ships the pattern-store schema + one seed model
(`recovery.hrv_14d_baseline`) to prove the end-to-end path works:
read raw Garmin sensor data → compute rolling stat → write/update
the model row → MCP tool surfaces it to the agent.

Future stat-derived models (`recovery.hrv_curve_post_long_run`,
`heat.pace_drop_at_temp`, etc.) follow the same shape — each is one
function in this module that the nightly cron (or manual trigger)
calls. P6 builds the next batch on top of this scaffolding.

Design notes:
- All functions take a TraceLogger-free MemoryOS + a DataProcessor.
- Each function is idempotent: create model on first call, update
  in place on subsequent calls. Caller doesn't need to check existence.
- `params_json` shape is per `model_type`. mean_std uses
  {mean, sd, baseline_label}. Other shapes documented per function.
"""

from __future__ import annotations

import statistics
from typing import Any


def refit_hrv_14d_baseline(memory_engine: Any, data_processor: Any) -> str | None:
    """Compute rolling 14-day HRV mean + sd from the health ledger.
    Returns the model_key if a refit happened, None if there wasn't
    enough data (< 7 days with non-null HRV in the window).

    Model shape (model_type='mean_std'):
        params_json = {
            "mean":  72.4,           # rolling 14d mean of nightly HRV (ms)
            "sd":    2.8,            # sd over the same window
            "window_days": 14,
            "n_used": 12,            # actual non-null samples in window
            "low_warning":  67.0,    # mean - 2*sd → user's "below baseline" floor
            "high_warning": 78.0,    # mean + 2*sd → suspiciously high (illness recovery)
        }

    Status flips:
        n_used <  7  → don't create (return None)
        n_used <  10 → 'Forming'
        n_used >= 10 → 'Stable'

    Confidence: low (<10), medium (10–12), high (13+).
    """
    rows = data_processor.get_health_stats() or []
    # get_health_stats returns oldest → newest; take last 14 calendar
    # days. NULL hrv (= no Garmin upload that night) is skipped — the
    # rolling baseline is about "what's normal when watch records",
    # not "what was the schedule".
    window = rows[-14:]
    hrv_values = [
        float(r["hrv"]) for r in window
        if r.get("hrv") is not None
    ]
    if len(hrv_values) < 7:
        return None  # not enough data to characterize

    mean_v = statistics.fmean(hrv_values)
    sd_v = statistics.pstdev(hrv_values) if len(hrv_values) > 1 else 0.0
    n_used = len(hrv_values)

    if n_used >= 13:
        confidence = "high"
    elif n_used >= 10:
        confidence = "medium"
    else:
        confidence = "low"
    status = "Stable" if n_used >= 10 else "Forming"

    params = {
        "mean": round(mean_v, 1),
        "sd": round(sd_v, 1),
        "window_days": 14,
        "n_used": n_used,
        "low_warning": round(mean_v - 2 * sd_v, 1),
        "high_warning": round(mean_v + 2 * sd_v, 1),
    }
    evidence = {"dates": [r["date"] for r in window if r.get("hrv") is not None]}

    model_key = "recovery.hrv_14d_baseline"
    existing = memory_engine.get_model(model_key)
    if existing:
        memory_engine.update_model_params(
            model_key,
            params_json=params,
            n_samples=n_used,
            confidence=confidence,
            evidence_json=evidence,
            status=status,
        )
    else:
        memory_engine.create_model(
            model_key=model_key,
            name="14 天 HRV 基线",
            category="Health/Recovery",
            model_type="mean_std",
            params_json=params,
            n_samples=n_used,
            confidence=confidence,
            evidence_json=evidence,
            derivation_method="stat",
            status=status,
        )
    return model_key
