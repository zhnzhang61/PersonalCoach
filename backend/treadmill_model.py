"""Treadmill road-equivalent pace/distance estimator.

Treadmill runs have no trustworthy speed source: the watch's wrist
accelerometer underestimates by ~1 min/mi and belt displays overstate
speed increasingly with load (both user-confirmed). The two signals
that ARE trustworthy indoors — HRM-strap heart rate and cadence — are
exactly what this module consumes.

The model learns stride length from the user's OUTDOOR GPS runs
(grade-adjusted, per-lap, user-labeled effort with Rest laps excluded)
and inverts it indoors:

    stride(m) = b0 + b1*cad + b2*HR + b3*HR² + b4*cad*HR
                + b5*max(T-15°C, 0)          # heat shortens stride at fixed HR
                + b6*max(12-t, 0)            # warm-up: early HR under-reads effort
                + b7*t                       # late-run cardiac drift
    speed = cad * stride

Calibration is ROLLING: only outdoor runs from the last WINDOW_DAYS
train the fit, so the mapping tracks current fitness (the same HR meant
a ~5 bpm faster runner in late 2025 — using all-time data would inflate
estimates). The fit is cached on disk and refit lazily whenever a newer
labeled run or lap file appears.

Prediction assumes the user's treadmill protocol: 1% incline (which
offsets missing air resistance, so outdoor-flat stride applies) and a
constant indoor temperature (INDOOR_TEMP_F).
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

import numpy as np

TREADMILL_TYPE_KEYS = {"treadmill_running", "indoor_running"}

INDOOR_TEMP_F = 78.0
INDOOR_TEMP_C = (INDOOR_TEMP_F - 32) * 5 / 9

WINDOW_DAYS = 150
FALLBACK_WINDOW_DAYS = 300
MIN_TRAIN_LAPS = 120

# Lap hygiene — same thresholds the offline calibration converged on.
MIN_LAP_SECONDS = 60
MIN_LAP_MILES = 0.15
MIN_RUN_CADENCE = 140  # below this it's walking, whatever the label says
MILE_M = 1609.34

MODEL_CACHE_FILENAME = "treadmill_model.json"


class ModelUnavailable(Exception):
    """Not enough labeled outdoor training data to fit."""


class NoTelemetry(Exception):
    """The activity has no HR/cadence curve on disk."""


class NotTreadmill(Exception):
    """The activity is not a treadmill/indoor run."""


def is_treadmill(summary: dict) -> bool:
    key = ((summary.get("activityType") or {}).get("typeKey") or "")
    return key in TREADMILL_TYPE_KEYS


# ---------------------------------------------------------------------------
# Training-set assembly
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Any | None:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _cached_temp_c(processor, activity_id: int) -> float | None:
    """Weather-cache read ONLY — fitting must never hit the network."""
    w = _read_json(os.path.join(processor.paths["weather"], f"{activity_id}.json"))
    if isinstance(w, dict):
        return w.get("temperature_c")
    return None


def _collect_training_laps(processor, start: str, end: str) -> list[dict]:
    """Outdoor GPS runs in [start, end] that the user has lap-labeled.

    One row per non-Rest lap that passes hygiene gates. Runs without a
    lap_categories meta are skipped entirely (unlabeled interval days
    would poison the fit — the labels are the source of truth), as are
    runs without cached weather (temperature is a model input).
    """
    rows: list[dict] = []
    for summary in processor.get_activities_in_range(start, end):
        key = ((summary.get("activityType") or {}).get("typeKey") or "")
        if key not in ("running", "track_running"):
            continue
        rid = summary.get("activityId")
        meta = _read_json(
            os.path.join(processor.paths["manual"], f"run_{rid}_meta.json")
        )
        lap_categories = (meta or {}).get("lap_categories") or []
        if not lap_categories:
            continue
        temp_c = _cached_temp_c(processor, rid)
        if temp_c is None:
            continue
        laps = processor.get_run_laps(rid)
        elapsed_s = 0.0
        for i, lap in enumerate(laps):
            duration_s = lap.get("movingDuration") or lap.get("duration") or 0
            mid_min = (elapsed_s + duration_s / 2) / 60
            elapsed_s += duration_s
            if i >= len(lap_categories) or lap_categories[i] == "Rest":
                continue
            dist_mi = (lap.get("distance") or 0) / MILE_M
            cad = lap.get("averageRunCadence")
            hr = lap.get("averageHR")
            speed = lap.get("avgGradeAdjustedSpeed") or lap.get("averageSpeed")
            if (
                duration_s < MIN_LAP_SECONDS
                or dist_mi < MIN_LAP_MILES
                or not cad
                or cad < MIN_RUN_CADENCE
                or not hr
                or not speed
            ):
                continue
            rows.append(
                {
                    "rid": rid,
                    "date": (summary.get("startTimeLocal") or "")[:10],
                    "cad": float(cad),
                    "hr": float(hr),
                    "stride": float(speed) * 60 / float(cad),
                    "temp_c": float(temp_c),
                    "t_min": mid_min,
                    "duration_s": float(duration_s),
                    "speed": float(speed),
                }
            )
    return rows


def _design(cad, hr, hinge, warm, drift) -> np.ndarray:
    return np.column_stack(
        [np.ones(len(cad)), cad, hr, hr * hr, cad * hr, hinge, warm, drift]
    )


def _features(rows: list[dict]):
    cad = np.array([r["cad"] for r in rows])
    hr = np.array([r["hr"] for r in rows])
    temp = np.array([r["temp_c"] for r in rows])
    t = np.array([r["t_min"] for r in rows])
    stride = np.array([r["stride"] for r in rows])
    X = _design(cad, hr, np.maximum(temp - 15, 0), np.maximum(12 - t, 0), t)
    return X, stride


def _cv_median_distance_pct(rows: list[dict]) -> float | None:
    """5-fold CV grouped by run; per-run |distance error| %, median."""
    rids = np.array([r["rid"] for r in rows])
    unique = np.unique(rids)
    if len(unique) < 10:
        return None
    rng = np.random.default_rng(0)
    order = rng.permutation(unique)
    folds = np.array_split(order, 5)
    X, stride = _features(rows)
    cad = np.array([r["cad"] for r in rows])
    dur = np.array([r["duration_s"] for r in rows])
    actual_speed = np.array([r["speed"] for r in rows])
    errs = []
    for fold in folds:
        test = np.isin(rids, fold)
        coef, *_ = np.linalg.lstsq(X[~test], stride[~test], rcond=None)
        pred_stride = np.maximum(X @ coef, 0.4)
        for rid in fold:
            m = test & (rids == rid)
            if m.sum() < 2:
                continue
            pred = (cad[m] * pred_stride[m] / 60 * dur[m]).sum()
            actual = (actual_speed[m] * dur[m]).sum()
            if actual > 0:
                errs.append(abs(pred / actual - 1) * 100)
    return round(float(np.median(errs)), 1) if errs else None


def fit_model(processor, today: datetime.date | None = None) -> dict:
    """Fit on the rolling window; widen once if data is thin."""
    today = today or datetime.date.today()
    window = WINDOW_DAYS
    start = (today - datetime.timedelta(days=window)).isoformat()
    rows = _collect_training_laps(processor, start, today.isoformat())
    if len(rows) < MIN_TRAIN_LAPS:
        window = FALLBACK_WINDOW_DAYS
        start = (today - datetime.timedelta(days=window)).isoformat()
        rows = _collect_training_laps(processor, start, today.isoformat())
    if len(rows) < MIN_TRAIN_LAPS:
        raise ModelUnavailable(
            f"Need ≥{MIN_TRAIN_LAPS} labeled outdoor laps in the last "
            f"{FALLBACK_WINDOW_DAYS} days; found {len(rows)}."
        )
    X, stride = _features(rows)
    coef, *_ = np.linalg.lstsq(X, stride, rcond=None)
    return {
        "coef": [float(c) for c in coef],
        "n_laps": len(rows),
        "n_runs": int(len({r["rid"] for r in rows})),
        "window_days": window,
        "trained_through": max(r["date"] for r in rows),
        "cv_median_pct": _cv_median_distance_pct(rows),
        "fitted_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "indoor_temp_f": INDOOR_TEMP_F,
    }


# ---------------------------------------------------------------------------
# Cache + staleness
# ---------------------------------------------------------------------------

def _cache_path(processor) -> str:
    return os.path.join(processor.data_dir, "derived", MODEL_CACHE_FILENAME)


def _labels_mtime(processor) -> float:
    """Newest mtime across run meta files — cheap staleness signal that
    moves whenever the user labels/relabels ANY run."""
    newest = 0.0
    manual = processor.paths["manual"]
    if not os.path.isdir(manual):
        return newest
    with os.scandir(manual) as it:
        for entry in it:
            if entry.name.startswith("run_") and entry.name.endswith("_meta.json"):
                newest = max(newest, entry.stat().st_mtime)
    return newest


def get_model(processor) -> dict:
    """Cached fit; refit when labels changed or the fit is from an older
    day (so a fresh outdoor sync rolls the window forward)."""
    path = _cache_path(processor)
    cached = _read_json(path)
    labels_mtime = _labels_mtime(processor)
    today = datetime.date.today().isoformat()
    if (
        isinstance(cached, dict)
        and cached.get("coef")
        and cached.get("labels_mtime") == labels_mtime
        and str(cached.get("fitted_at", "")).startswith(today)
    ):
        return cached
    model = fit_model(processor)
    model["labels_mtime"] = labels_mtime
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(model, f, indent=2)
    return model


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def _fmt_pace(seconds_per_mi: float) -> str:
    s = int(round(seconds_per_mi))
    return f"{s // 60}:{s % 60:02d}"


def _fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def predict_run(processor, activity_id: int, summary: dict) -> dict:
    """Road-equivalent estimate for one treadmill activity.

    Integrates speed = cad × stride(cad, HR, t) over the HR/cadence
    telemetry curves. Buckets with cadence < MIN_RUN_CADENCE (standing /
    walking / belt pause) contribute zero distance but still advance the
    clock, matching how the user actually uses the machine.
    """
    if not is_treadmill(summary):
        raise NotTreadmill(
            f"activity {activity_id} is "
            f"{(summary.get('activityType') or {}).get('typeKey')!r}, "
            "not a treadmill/indoor run"
        )
    model = get_model(processor)
    b = model["coef"]

    raw, _ = processor.get_activity_telemetry(activity_id, downsample_sec=5)
    if raw is None or len(raw) == 0:
        raise NoTelemetry(f"no telemetry for activity {activity_id}")
    cols = set(raw.columns)
    if not {"Second", "HeartRate", "Cadence"} <= cols:
        raise NoTelemetry(f"telemetry missing HR/cadence for {activity_id}")

    frame = raw[["Second", "HeartRate", "Cadence"]].dropna()
    frame = frame.sort_values("Second")
    seconds = frame["Second"].to_numpy(dtype=float)
    hr = frame["HeartRate"].to_numpy(dtype=float)
    cad = frame["Cadence"].to_numpy(dtype=float)
    if len(seconds) < 10:
        raise NoTelemetry(f"telemetry too short for {activity_id}")

    hinge = max(INDOOR_TEMP_C - 15, 0)
    t_min = seconds / 60
    stride = (
        b[0]
        + b[1] * cad
        + b[2] * hr
        + b[3] * hr * hr
        + b[4] * cad * hr
        + b[5] * hinge
        + b[6] * np.maximum(12 - t_min, 0)
        + b[7] * t_min
    )
    speed = np.where(cad >= MIN_RUN_CADENCE, cad * np.maximum(stride, 0.4) / 60, 0.0)

    dt = np.diff(seconds, prepend=seconds[0])
    # Ignore recording gaps (watch paused): they advance neither clock nor
    # distance — Garmin's own timer excludes them too.
    dt = np.where((dt <= 0) | (dt > 30), 0.0, dt)
    dist_m = np.cumsum(speed * dt)
    total_m = float(dist_m[-1])
    duration_s = float(dt.sum())
    if total_m < MILE_M / 10 or duration_s <= 0:
        raise NoTelemetry(f"not enough running signal in {activity_id}")

    # Mile splits: time at each whole-mile crossing (linear interp inside
    # the bucket where the crossing happens).
    splits = []
    prev_cross = 0.0
    elapsed = np.cumsum(dt)
    for mile in range(1, int(total_m / MILE_M) + 1):
        target = mile * MILE_M
        idx = int(np.searchsorted(dist_m, target))
        d0 = dist_m[idx - 1] if idx > 0 else 0.0
        t0 = elapsed[idx - 1] if idx > 0 else 0.0
        frac = (target - d0) / max(dist_m[idx] - d0, 1e-9)
        cross = t0 + frac * (elapsed[idx] - t0)
        pace_s = cross - prev_cross
        splits.append(
            {"mile": mile, "pace_s": round(pace_s), "pace_str": _fmt_pace(pace_s)}
        )
        prev_cross = cross
    partial_mi = total_m / MILE_M - len(splits)
    if partial_mi > 0.02:
        pace_s = (duration_s - prev_cross) / partial_mi
        splits.append(
            {
                "mile": len(splits) + 1,
                "partial_mi": round(partial_mi, 2),
                "pace_s": round(pace_s),
                "pace_str": _fmt_pace(pace_s),
            }
        )

    avg_pace_s = duration_s / (total_m / MILE_M)
    return {
        "activity_id": activity_id,
        "estimate": {
            "total_distance_mi": round(total_m / MILE_M, 2),
            "total_distance_km": round(total_m / 1000, 2),
            "duration_s": round(duration_s),
            "duration_str": _fmt_duration(duration_s),
            "avg_pace_s_per_mi": round(avg_pace_s),
            "avg_pace_str": _fmt_pace(avg_pace_s),
            "splits": splits,
        },
        "model": {
            k: model.get(k)
            for k in (
                "n_laps",
                "n_runs",
                "window_days",
                "trained_through",
                "cv_median_pct",
                "fitted_at",
                "indoor_temp_f",
            )
        },
    }
