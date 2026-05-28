"""Seed / refit stat-derived models from raw data.

PR P1 ships the pattern-store schema + one seed model
(`recovery.hrv_14d_baseline`) to prove the end-to-end path works:
read raw Garmin sensor data → compute rolling stat → write/update
the model row → MCP tool surfaces it to the agent.

PR P6 adds two more on the same scaffold (§5 of coach_brain_design):
- `aerobic.decoupling_baseline` (mean_std) — pace/HR drift on easy
  runs. Lower = better aerobic fitness. Comparing today's run to
  this baseline tells the agent whether the HR drift is normal for
  the user or unusual.
- `cadence.baseline` (mean_std) — typical steady-state cadence on
  easy effort. Drops below baseline often correlate with fatigue,
  shoe change, or terrain shift.

Future stat-derived models (`heat.pace_drop_at_temp`,
`menstrual.hrv_phase_response`, etc.) follow the same shape — each
is one function in this module that the nightly cron (or manual
trigger) calls.

Design notes:
- All functions take a TraceLogger-free MemoryOS + a DataProcessor.
- Each function is idempotent: create model on first call, update
  in place on subsequent calls. Caller doesn't need to check existence.
- `params_json` shape is per `model_type`. mean_std uses
  {mean, sd, n_used, low_warning, high_warning}. Other shapes
  documented per function.
- P6 models gate on a sample-count floor (n_used >= 3) so a single
  noisy run doesn't poison the baseline. Status flips
  Forming → Stable around n_used >= 8.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any


# Aerobic-effort ceiling expressed as a fraction of lactate threshold
# HR. 92% is a defensible "easy/long" ceiling — anything above puts
# the run in tempo territory where decoupling stops being about
# aerobic fitness and starts being about acute lactate accumulation.
_AEROBIC_HR_FRACTION_OF_LT = 0.92

# Fallback ceiling when the user doesn't have a Garmin-detected LT HR.
# 155 bpm is roughly aerobic for most adult runners; we'd rather skip
# a few borderline runs than poison the baseline with tempo data.
_AEROBIC_FALLBACK_HR = 155

# Per-run minimum duration. Too short and the second half doesn't
# differ meaningfully from the first.
_MIN_RUN_DURATION_S = 30 * 60

# Look-back window. ~one mesocycle — long enough to average out a
# fluky run, short enough that we're characterizing the user's
# CURRENT state (not last training block's).
_LOOKBACK_DAYS = 28


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


# ---------------------------------------------------------------------------
# PR P6 — per-run shape helpers + two baselines
# ---------------------------------------------------------------------------


def _aerobic_hr_ceiling(data_processor: Any) -> float:
    """Return the HR ceiling above which a run no longer counts as
    "aerobic / easy" for baseline purposes. Reads the user's
    lactate-threshold HR from the athlete profile when available;
    falls back to a generic ceiling otherwise."""
    try:
        profile = data_processor.get_athlete_profile_full() or {}
    except Exception:
        profile = {}
    lt = (profile.get("fitness") or {}).get("lactate_threshold_hr")
    if isinstance(lt, (int, float)) and lt > 0:
        return float(lt) * _AEROBIC_HR_FRACTION_OF_LT
    return float(_AEROBIC_FALLBACK_HR)


def _compute_run_decoupling_pct(
    activity_id: int, data_processor: Any
) -> float | None:
    """Aerobic decoupling % for ONE run: how much HR drifted upward
    per unit pace in the second half vs the first half.

    Formula:
        h1 = mean(HR) / mean(speed_m/s)  over first time-half
        h2 = mean(HR) / mean(speed_m/s)  over second time-half
        decoupling_pct = (h2 - h1) / h1 * 100

    Positive = HR drifted up (typical for any run; magnitude is the
    signal). Lower = better aerobic shape — the cardiovascular system
    can sustain the pace without HR creep.

    Returns None when telemetry is unavailable or either half lacks
    enough usable samples (at least 30 valid points per side, ~5 min
    @ 10s downsample). Skipping is preferred over emitting a noisy
    point that would shift the baseline.
    """
    df_raw, _ = data_processor.get_activity_telemetry(activity_id)
    if df_raw is None or df_raw.empty:
        return None
    # Restrict to samples with both HR and forward motion. Speed > 0.5
    # m/s (≈ slow jog) filters out pauses / stop-and-go around lights.
    df = df_raw[
        df_raw["HeartRate"].notna()
        & df_raw["Speed_mps"].notna()
        & (df_raw["Speed_mps"] > 0.5)
    ]
    n = len(df)
    if n < 60:
        return None
    midpoint = df["Second"].iloc[n // 2]
    h1_df = df[df["Second"] <= midpoint]
    h2_df = df[df["Second"] > midpoint]
    if len(h1_df) < 30 or len(h2_df) < 30:
        return None
    h1_hr = float(h1_df["HeartRate"].mean())
    h1_speed = float(h1_df["Speed_mps"].mean())
    h2_hr = float(h2_df["HeartRate"].mean())
    h2_speed = float(h2_df["Speed_mps"].mean())
    if h1_speed <= 0 or h2_speed <= 0:
        return None
    h1 = h1_hr / h1_speed
    h2 = h2_hr / h2_speed
    if h1 <= 0:
        return None
    return (h2 - h1) / h1 * 100.0


def _compute_run_avg_cadence(
    activity_id: int, data_processor: Any
) -> float | None:
    """Average cadence (steps per minute) for a run, from raw
    telemetry. Returns None when telemetry is missing or we don't
    have at least 60 valid samples (~10 min @ 10s downsample)."""
    df_raw, _ = data_processor.get_activity_telemetry(activity_id)
    if df_raw is None or df_raw.empty:
        return None
    cad = df_raw["Cadence"].dropna()
    # Filter out implausibly low samples (treadmill paused / GPS
    # confusion) — cadence below 100 spm isn't a running gait.
    cad = cad[cad >= 100]
    if len(cad) < 60:
        return None
    return float(cad.mean())


def _is_aerobic_run(run: Any, hr_ceiling: float) -> bool:
    """Filter for "easy/long enough to baseline" runs. Combines:
    - duration >= _MIN_RUN_DURATION_S (30 min)
    - avg_hr present AND below the aerobic ceiling
    Skips runs with no HR signal (e.g. forgot the strap) because we
    can't tell if they were aerobic, and including them would skew
    the cadence baseline toward whatever effort the user happened to
    do."""
    if (run.duration_s or 0) < _MIN_RUN_DURATION_S:
        return False
    if run.avg_hr is None:
        return False
    return run.avg_hr < hr_ceiling


def _compute_baseline_params(samples: list[float]) -> tuple[dict, str, str]:
    """Shared mean/sd characterization. Returns (params, confidence,
    status) given the raw sample list. Mirrors the hrv_14d shape so
    the agent reads them identically.

    Status flips at n=8 (Forming → Stable). Confidence ladder is
    n>=12 high / n>=8 medium / else low. These thresholds are
    looser than the HRV model's (HRV refits daily; these refit
    per-run) but the spirit is the same: pin a confidence the agent
    can quote.
    """
    n_used = len(samples)
    mean_v = statistics.fmean(samples)
    sd_v = statistics.pstdev(samples) if n_used > 1 else 0.0
    if n_used >= 12:
        confidence = "high"
    elif n_used >= 8:
        confidence = "medium"
    else:
        confidence = "low"
    status = "Stable" if n_used >= 8 else "Forming"
    params = {
        "mean": round(mean_v, 1),
        "sd": round(sd_v, 1),
        "n_used": n_used,
        # ±2σ — pin user-facing "out-of-band" thresholds so the agent
        # can quote them as concrete numbers rather than re-deriving
        # in prose.
        "low_warning": round(mean_v - 2 * sd_v, 1),
        "high_warning": round(mean_v + 2 * sd_v, 1),
    }
    return params, confidence, status


def refit_aerobic_decoupling_baseline(
    memory_engine: Any, data_processor: Any
) -> str | None:
    """Typical aerobic decoupling % from the last _LOOKBACK_DAYS of
    easy / long runs. Stored as a mean_std baseline so the agent
    can answer "is today's drift normal or unusually high?".

    Returns the model_key on a successful refit; None when there
    weren't enough qualifying runs (need 3+ aerobic runs with
    usable telemetry in the window)."""
    today = date.today()
    start = (today - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    end = today.isoformat()
    runs = data_processor.list_runs(start, end)

    hr_ceiling = _aerobic_hr_ceiling(data_processor)
    samples: list[float] = []
    evidence_aids: list[int] = []
    for r in runs:
        if not _is_aerobic_run(r, hr_ceiling):
            continue
        d = _compute_run_decoupling_pct(r.activity_id, data_processor)
        if d is None:
            continue
        samples.append(d)
        evidence_aids.append(r.activity_id)

    if len(samples) < 3:
        return None

    params, confidence, status = _compute_baseline_params(samples)
    # Decoupling-specific extras the agent will reason about — record
    # which HR ceiling was used so a future-me re-running the refit
    # in a different fitness state can tell why the sample set
    # changed.
    params["units"] = "percent"
    params["aerobic_hr_ceiling_used"] = round(hr_ceiling, 1)
    params["lookback_days"] = _LOOKBACK_DAYS
    evidence = {"activity_ids": evidence_aids}

    model_key = "aerobic.decoupling_baseline"
    existing = memory_engine.get_model(model_key)
    if existing:
        memory_engine.update_model_params(
            model_key,
            params_json=params,
            n_samples=len(samples),
            confidence=confidence,
            evidence_json=evidence,
            status=status,
        )
    else:
        memory_engine.create_model(
            model_key=model_key,
            name="有氧 HR/pace 漂移基线",
            category="Running/Aerobic",
            model_type="mean_std",
            params_json=params,
            n_samples=len(samples),
            confidence=confidence,
            evidence_json=evidence,
            derivation_method="stat",
            status=status,
        )
    return model_key


def refit_cadence_baseline(
    memory_engine: Any, data_processor: Any
) -> str | None:
    """Typical steady-state cadence on easy effort from the last
    _LOOKBACK_DAYS. mean_std baseline so the agent can flag drops
    ('your cadence today was 170 vs baseline 178 — fatigue or shoe
    change?').

    Returns the model_key on a successful refit; None when there
    weren't enough qualifying runs (need 3+ aerobic runs with
    usable cadence telemetry)."""
    today = date.today()
    start = (today - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    end = today.isoformat()
    runs = data_processor.list_runs(start, end)

    hr_ceiling = _aerobic_hr_ceiling(data_processor)
    samples: list[float] = []
    evidence_aids: list[int] = []
    for r in runs:
        if not _is_aerobic_run(r, hr_ceiling):
            continue
        c = _compute_run_avg_cadence(r.activity_id, data_processor)
        if c is None:
            continue
        samples.append(c)
        evidence_aids.append(r.activity_id)

    if len(samples) < 3:
        return None

    params, confidence, status = _compute_baseline_params(samples)
    params["units"] = "spm"
    params["aerobic_hr_ceiling_used"] = round(hr_ceiling, 1)
    params["lookback_days"] = _LOOKBACK_DAYS
    evidence = {"activity_ids": evidence_aids}

    model_key = "cadence.baseline"
    existing = memory_engine.get_model(model_key)
    if existing:
        memory_engine.update_model_params(
            model_key,
            params_json=params,
            n_samples=len(samples),
            confidence=confidence,
            evidence_json=evidence,
            status=status,
        )
    else:
        memory_engine.create_model(
            model_key=model_key,
            name="轻松跑步频基线",
            category="Running/Biomechanics",
            model_type="mean_std",
            params_json=params,
            n_samples=len(samples),
            confidence=confidence,
            evidence_json=evidence,
            derivation_method="stat",
            status=status,
        )
    return model_key
