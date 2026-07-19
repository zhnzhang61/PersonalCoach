"""Seed / refit stat-derived models from raw data.

PR P1 ships the pattern-store schema + one seed model
(`recovery.hrv_14d_baseline`) to prove the end-to-end path works:
read raw Garmin sensor data → compute rolling stat → write/update
the model row → MCP tool surfaces it to the agent.

PR P6 batch 1 added two on the same scaffold (§5 of coach_brain_design):
- `aerobic.decoupling_baseline` (mean_std) — pace/HR drift on easy
  runs. Lower = better aerobic fitness. Comparing today's run to
  this baseline tells the agent whether the HR drift is normal for
  the user or unusual.
- `cadence.baseline` (mean_std) — typical steady-state cadence on
  easy effort. Drops below baseline often correlate with fatigue,
  shoe change, or terrain shift.

PR P6 batch 2 adds two more:
- `sleep.debt_14d` (mean_std) — 14-day rolling sleep baseline +
  total debt against an 8h target + count of below-target nights.
  Mirrors hrv_14d's shape since both move on the same timescale.
- `cycle.weekly_volume_diff` (linear_trend) — slope of weekly
  mileage over the last 6 weeks. One number ("ramping at +3.5
  mi/wk") replaces the agent doing week-vs-week math each turn.

Still pending (deferred from P6 batch 2 because user data is too
thin to characterize meaningfully):
- `tempo.pace_hr_table` — typical pace at each HR band on tempo
  days. Needs either tagged `lap_categories` (currently sparse —
  user mostly does easy runs + the occasional marathon) OR an
  HR-band heuristic. Revisit when there's data.

Future stat-derived models (`heat.pace_drop_at_temp`,
`menstrual.hrv_phase_response`, etc.) follow the same shape — each
is one function in this module that the nightly cron (or manual
trigger) calls.

Design notes:
- All functions take a TraceLogger-free MemoryOS + a DataProcessor.
- Each function is idempotent: create model on first call, update
  in place on subsequent calls. Caller doesn't need to check existence.
- `params_json` shape is per `model_type`. mean_std uses
  {mean, sd, n_used} always, plus {low_warning, high_warning} at
  n_used >= 7 (band suppressed below that — too noisy with few
  samples). Other shapes documented per function.
- P6 models gate on a sample-count floor (n_used >= 3) so a single
  noisy run doesn't poison the baseline. Status flips
  Forming → Stable around n_used >= 8.

`evidence_json` shape (per model_key — keep this list in sync as
new models land; a generic "what fed this baseline?" tool needs
the contract documented since the shape varies):

  - `recovery.hrv_14d_baseline`     → `{"dates": ["YYYY-MM-DD", ...]}`
                                       (one date per night with HRV
                                       in the window)
  - `aerobic.decoupling_baseline`   → `{"activity_ids": [int, ...]}`
                                       (one activity_id per
                                       qualifying easy/long run)
  - `cadence.baseline`              → `{"activity_ids": [int, ...]}`
                                       (same as decoupling, but
                                       includes treadmills which
                                       decoupling excludes — see
                                       comments on the compute
                                       helpers for why)
  - `sleep.debt_14d`                → `{"dates": ["YYYY-MM-DD", ...]}`
                                       (one date per qualifying
                                       night, same shape as HRV
                                       since both share the
                                       health-ledger source)
  - `cycle.weekly_volume_diff`      → `{"weeks": ["YYYY-Www", ...]}`
                                       (ISO calendar weeks; the
                                       weeks_used field in params
                                       has the {week, miles} pairs)
"""

from __future__ import annotations

import json
import os
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
    falls back to a generic 155 bpm ceiling otherwise.

    Catches only the "profile not bootstrapped yet" exceptions —
    fresh install, semantic memory not seeded, missing file. Real
    failures (DB lock, schema migration error, buggy refactor) will
    propagate so they don't silently demote a fit user's ceiling
    from 165 → 155 bpm and skew the baseline sample set.
    """
    try:
        profile = data_processor.get_athlete_profile_full() or {}
    except (AttributeError, KeyError, FileNotFoundError):
        # Profile not bootstrapped yet — genuine fallback case.
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
    # Side effect: treadmill runs without a footpod (Speed_mps=0 from
    # zero-distance deltas) drop to zero surviving samples → return
    # None and never contribute to the baseline. Intentional —
    # decoupling math requires pace, which treadmills don't provide.
    # The cadence baseline does NOT filter on speed, so treadmill
    # cadence DOES contribute there (treadmill cadence is meaningful
    # without GPS).
    df = df_raw[
        df_raw["HeartRate"].notna()
        & df_raw["Speed_mps"].notna()
        & (df_raw["Speed_mps"] > 0.5)
    ]
    n = len(df)
    if n < 60:
        return None
    # Split on wall-clock midpoint of the run, not row position.
    # Walk-pause clusters (lights / hills / GPS dropouts) shift row
    # density across the run; using iloc[n//2] would split into two
    # halves with unequal time spans, biasing decoupling toward
    # whichever side saw the steeper HR trajectory. Wall-clock split
    # matches the conventional "first half vs second half" definition
    # the agent's coaching prompts quote.
    midpoint = (df["Second"].iloc[0] + df["Second"].iloc[-1]) / 2.0
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
    have at least 60 valid samples (~10 min @ 10s downsample).

    Note (deliberate asymmetry vs decoupling): this does NOT filter
    on Speed_mps, so treadmill runs DO contribute to the cadence
    baseline. Cadence is meaningful without GPS — your feet still
    move whether you're outdoors or on a belt. Decoupling needs pace
    and so excludes treadmills; cadence doesn't and so includes them."""
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

    `low_warning` / `high_warning` (mean ± 2σ) are ONLY emitted at
    n >= 7. At n=3 the population SD estimate is wildly unstable —
    one outlier swings the band by 50%. Emitting the bands at low-n
    would tempt the agent to quote a "your normal range is X to Y"
    answer built on noise. Keeping `mean` + `sd` at all n is fine:
    they're descriptive, not threshold-y.
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
    params: dict[str, Any] = {
        "mean": round(mean_v, 1),
        "sd": round(sd_v, 1),
        "n_used": n_used,
    }
    if n_used >= 7:
        # ±2σ — pin user-facing "out-of-band" thresholds so the
        # agent can quote them as concrete numbers rather than
        # re-deriving in prose. Suppressed below n=7 (see docstring).
        params["low_warning"] = round(mean_v - 2 * sd_v, 1)
        params["high_warning"] = round(mean_v + 2 * sd_v, 1)
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


# ---------------------------------------------------------------------------
# PR #114 — rest-recovery (HRR60) baseline
# ---------------------------------------------------------------------------


# HRR changes slowly and interval days are sparse (1-2/week at most),
# so the 28-day window the aerobic baselines use would rarely collect
# the 3-run floor. 90 days trades a little staleness for actually
# having a baseline.
_HRR_LOOKBACK_DAYS = 90


def _compute_run_rest_recovery(activity_id: int, dp: Any) -> float | None:
    """Median HRR60 across a run's qualifying Rest transitions, using
    the same definition the per-run verdict uses (run_verdicts.
    rest_recovery_drops) so verdict and baseline can't drift apart.
    None when the run has no qualifying transition."""
    from backend.run_verdicts import rest_recovery_drops

    laps = dp.get_run_laps(activity_id)
    if not laps:
        return None
    meta_path = os.path.join(dp.paths["manual"], f"run_{activity_id}_meta.json")
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            categories = json.load(f).get("lap_categories") or []
    except Exception:
        return None
    if "Rest" not in categories:
        return None
    df_raw, _ = dp.get_activity_telemetry(activity_id, laps=laps)
    if df_raw is None or len(df_raw) == 0:
        return None
    drops = rest_recovery_drops(laps, categories, df_raw)
    if not drops:
        return None
    return float(statistics.median(d["drop_bpm"] for d in drops))


def refit_rest_recovery_baseline(
    memory_engine: Any, data_processor: Any
) -> str | None:
    """Typical HRR60 (HR drop in the first 60 s of a Rest-labeled
    block after hard work) over the last _HRR_LOOKBACK_DAYS. mean_std
    baseline so the rest_recovery verdict (and the agent) can answer
    "is today's between-rep recovery normal for this user?".

    Returns the model_key on a successful refit; None when fewer than
    3 runs in the window have a qualifying Rest transition."""
    today = date.today()
    start = (today - timedelta(days=_HRR_LOOKBACK_DAYS)).isoformat()
    end = today.isoformat()
    runs = data_processor.list_runs(start, end)

    samples: list[float] = []
    evidence_aids: list[int] = []
    for r in runs:
        d = _compute_run_rest_recovery(r.activity_id, data_processor)
        if d is None:
            continue
        samples.append(d)
        evidence_aids.append(r.activity_id)

    if len(samples) < 3:
        return None

    params, confidence, status = _compute_baseline_params(samples)
    params["units"] = "bpm_per_60s"
    params["lookback_days"] = _HRR_LOOKBACK_DAYS
    evidence = {"activity_ids": evidence_aids}

    model_key = "hrr.rest_recovery_baseline"
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
            name="组间 HR 恢复基线",
            category="Running/Recovery",
            model_type="mean_std",
            params_json=params,
            n_samples=len(samples),
            confidence=confidence,
            evidence_json=evidence,
            derivation_method="stat",
            status=status,
        )
    return model_key


# ---------------------------------------------------------------------------
# PR P6 batch 2 — sleep debt + weekly volume trend
# ---------------------------------------------------------------------------


# Conventional "full night" target. Used to compute the per-night
# deficit that aggregates into `total_debt_hours_14d`. 8.0 is the
# generic adult-runner ceiling — could be tightened to a user
# preference later (athlete_profile already has a place for this);
# for now the value is conservative enough that high-volume runners
# still accrue some deficit on a typical week, which is the signal
# the agent uses.
_SLEEP_TARGET_HOURS = 8.0

# Look-back for sleep + volume. Same 14-day window the HRV baseline
# uses — sleep and HRV move on similar timescales (a single bad
# night doesn't define a baseline; a fortnight does).
_SLEEP_LOOKBACK_DAYS = 14

# Number of completed weeks the volume trend characterizes. 6 weeks
# covers a typical mesocycle (4 weeks build + 1 deload + this week).
# Fewer than 3 weeks of data → can't characterize a slope.
_VOLUME_WEEKS = 6

# Floor for the `weekly_change_pct` denominator. Below this weekly
# mileage, percent-change becomes a misleading amplifier — a 0.27
# mi/wk delta on a 1mi mean reads as +27% (alarming) but is
# clinically irrelevant. Clamping the divisor at 5 mi/wk means the
# pct only ever describes meaningful absolute changes; below that
# threshold the agent should reason from `slope` (raw mi/wk) and
# `weeks_used` directly instead of trusting the percentage.
_LOW_VOLUME_FLOOR_MI = 5.0


def refit_sleep_debt_14d(memory_engine: Any, data_processor: Any) -> str | None:
    """Sleep baseline + debt over the last 14 days. Mirrors the
    hrv_14d baseline shape (mean_std + n_used + warning bands at
    n>=7), plus three sleep-specific fields in params_json:
      - `target_hours` (_SLEEP_TARGET_HOURS) — the threshold used
        for debt accounting; pinned in params so the agent reads
        the same definition the model was computed against.
      - `total_debt_hours_14d` — sum of max(0, target - actual)
        over qualifying nights. A 14-day total, not per-night.
      - `nights_below_target_14d` — count of nights with
        actual < target (useful for "you've had 5 short nights
        in two weeks" phrasing).

    Returns the model_key on successful refit; None when fewer than
    7 nights of valid sleep data in the window (matches HRV's floor
    — sleep and HRV share the same data window so the threshold
    should match)."""
    rows = data_processor.get_health_stats() or []
    window = rows[-_SLEEP_LOOKBACK_DAYS:]
    samples: list[float] = []
    dates: list[str] = []
    for r in window:
        sleep_h = r.get("sleep_hours")
        if sleep_h is None:
            continue
        try:
            sleep_h = float(sleep_h)
        except (TypeError, ValueError):
            continue
        if sleep_h <= 0:
            # Garmin sometimes reports 0 for nights where the watch
            # wasn't worn — distinct from "slept 0 hours". Skip.
            continue
        samples.append(sleep_h)
        dates.append(r["date"])
    if len(samples) < 7:
        return None

    params, confidence, status = _compute_baseline_params(samples)
    params["units"] = "hours"
    params["target_hours"] = _SLEEP_TARGET_HOURS
    params["window_days"] = _SLEEP_LOOKBACK_DAYS
    # Field names say "_in_window" rather than "_14d" because the
    # values are summed over the qualifying nights (`n_used`), NOT
    # over the full 14-day window. Watch-not-worn nights are
    # filtered out; if the user wore the watch 8 of 14 nights,
    # these totals reflect those 8 nights, not the missing 6. The
    # agent reads `n_used` (top-level on the model row) alongside
    # these and quotes them honestly ("16 hours of debt across 8
    # measured nights").
    total_debt = sum(
        max(0.0, _SLEEP_TARGET_HOURS - s) for s in samples
    )
    params["total_debt_hours_in_window"] = round(total_debt, 1)
    params["nights_below_target_in_window"] = sum(
        1 for s in samples if s < _SLEEP_TARGET_HOURS
    )
    evidence = {"dates": dates}

    model_key = "sleep.debt_14d"
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
            name="14 天睡眠基线 + 赤字",
            category="Health/Sleep",
            model_type="mean_std",
            params_json=params,
            n_samples=len(samples),
            confidence=confidence,
            evidence_json=evidence,
            derivation_method="stat",
            status=status,
        )
    return model_key


def _bucket_mileage_by_iso_week(
    rows: list[dict],
) -> list[tuple[int, str, float]]:
    """Bucket health-ledger rows into ISO calendar weeks.

    Returns a list of `(week_index, week_label, total_miles)` tuples,
    earliest first. `week_index` is sequential from 0 across the
    returned list so the linear regression below can use it as the
    x axis (avoids the ISO week-of-year edge case at year boundaries).
    `week_label` is human-readable: 'YYYY-Www' (ISO 8601).
    """
    by_iso: dict[tuple[int, int], float] = {}
    for r in rows:
        d = r.get("date")
        miles = r.get("run_miles")
        if not d or miles is None:
            continue
        try:
            iso = date.fromisoformat(d).isocalendar()
        except (TypeError, ValueError):
            continue
        key = (iso[0], iso[1])  # (year, week)
        by_iso[key] = by_iso.get(key, 0.0) + float(miles)
    # Sort by (year, week); assign sequential index.
    ordered = sorted(by_iso.items())
    out: list[tuple[int, str, float]] = []
    for i, ((yr, wk), miles) in enumerate(ordered):
        out.append((i, f"{yr}-W{wk:02d}", round(miles, 2)))
    return out


def refit_cycle_weekly_volume_diff(
    memory_engine: Any, data_processor: Any
) -> str | None:
    """Week-over-week mileage trend from the last _VOLUME_WEEKS
    completed weeks. linear_trend on (week_index, total_miles)
    so the agent can answer 'am I ramping or tapering?' with one
    number (slope) instead of comparing two weeks each turn.

    params_json:
      - `slope`: miles/week change (positive = ramping)
      - `intercept`: regression baseline (x=0)
      - `weekly_change_pct`: percent change per week relative to
        the mean. Easier for the agent to quote than raw miles
        when weeks vary widely in absolute volume.
      - `r2`: fit quality. Low r2 = volume is volatile, the
        "trend" might just be noise. The agent should hedge.
      - `weeks_used`: list of `{week, miles}` dicts; one per
        observation. Lets the agent quote actual numbers
        ("last week 28mi, this week 32mi").

    Returns None when fewer than 3 weeks of data are available
    (need at least 3 points for a meaningful slope + r2).
    """
    rows = data_processor.get_health_stats() or []
    weeks = _bucket_mileage_by_iso_week(rows)
    # Drop the current (in-progress) ISO week from the regression.
    # `compile_health_ledger` emits a row per calendar day including
    # today, so the current week's bucket always reflects "what's in
    # the book so far" — typically 0 on a Monday morning refit. Left
    # in, that biases the slope downward at every Monday-morning
    # cron: a steady 25mi/wk runner reads as "tapering at -3.6 mi/wk"
    # right after the week rolls over. Dropping it means the slope
    # always covers complete weeks. The most-recent COMPLETED week's
    # mileage is still available to the agent via the model's
    # `weeks_used` list (it's the last entry after the drop).
    today_iso = date.today().isocalendar()
    weeks = [
        w for w in weeks
        if (int(w[1][:4]), int(w[1][6:])) != (today_iso[0], today_iso[1])
    ]
    weeks = weeks[-_VOLUME_WEEKS:]
    if len(weeks) < 3:
        return None

    # Re-index so x runs 0..n-1 over the windowed weeks (regardless
    # of original index from the full ledger).
    xs = list(range(len(weeks)))
    ys = [miles for _, _, miles in weeks]
    if all(y == 0 for y in ys):
        # Degenerate: no running at all — slope is zero, r2
        # undefined. Still want to publish the model so the agent
        # sees "0 mi/week with high confidence" rather than
        # "no model".
        slope, intercept, r2 = 0.0, 0.0, 1.0
    else:
        lr = statistics.linear_regression(xs, ys)
        slope = lr.slope
        intercept = lr.intercept
        # Pearson r² — manual since statistics doesn't expose it on
        # the LinearRegression result. r2 = 1 - SS_res/SS_tot.
        mean_y = statistics.fmean(ys)
        ss_tot = sum((y - mean_y) ** 2 for y in ys)
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 1.0

    mean_y = statistics.fmean(ys)
    # Floor the denominator at _LOW_VOLUME_FLOOR_MI so percent-change
    # doesn't explode in low-volume regimes (return-from-injury,
    # taper, off-season). Without this, [0.5, 1.0, 1.0, 1.5, 1.5,
    # 2.0] yields +22% per week — agent would read it as "ramping
    # fast, deload recommended" when the absolute change is
    # clinically irrelevant. Above 5 mi/wk the floor never bites
    # (mean_y > floor), so this only affects the regime where
    # absolute mileage matters more than percent anyway.
    weekly_change_pct = (
        slope / max(mean_y, _LOW_VOLUME_FLOOR_MI) * 100
    ) if mean_y > 0 else 0.0

    n = len(weeks)
    # Looser confidence ladder than the per-night models: 6 weeks
    # IS a full mesocycle, so all-of-window is high-confidence.
    if n >= 6:
        confidence = "high"
    elif n >= 4:
        confidence = "medium"
    else:
        confidence = "low"
    status = "Stable" if n >= 4 else "Forming"

    params = {
        "slope": round(slope, 2),
        "intercept": round(intercept, 2),
        "weekly_change_pct": round(weekly_change_pct, 1),
        "r2": round(r2, 2),
        "units": "miles_per_week",
        "weeks_used": [
            {"week": label, "miles": miles}
            for _, label, miles in weeks
        ],
    }
    evidence = {"weeks": [label for _, label, _ in weeks]}

    model_key = "cycle.weekly_volume_diff"
    existing = memory_engine.get_model(model_key)
    if existing:
        memory_engine.update_model_params(
            model_key,
            params_json=params,
            n_samples=n,
            confidence=confidence,
            evidence_json=evidence,
            status=status,
        )
    else:
        memory_engine.create_model(
            model_key=model_key,
            name="周里程趋势",
            category="Running/Volume",
            model_type="linear_trend",
            params_json=params,
            n_samples=n,
            confidence=confidence,
            evidence_json=evidence,
            derivation_method="stat",
            status=status,
        )
    return model_key
