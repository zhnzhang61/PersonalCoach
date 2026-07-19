"""Post-run verdict sentences — the four questions the user actually
asks after a run, each with an explicit trigger gate.

Design (PR #114): verdicts are a POOL, not a dashboard. A run only
fires the verdicts it qualifies for; everything else is reported in
`not_fired` with the reason, so both the UI and the agent know the
difference between "checked, fine" and "couldn't check".

The pool:
  • label_vs_objective — did the objective stream (HR) stay inside the
    zone band the user's perceived-effort label claims? The mismatch
    between streams IS the coach signal; this is its per-run receipt.
    Easy-labeled blocks are excluded here (easy_purity owns them).
  • rest_recovery — how fast HR falls in the first 60 s of a
    Rest-labeled block after hard work (HRR60). Judged against the
    personal baseline model `hrr.rest_recovery_baseline` when present.
  • lr_asymmetry — ground-contact L/R balance by thirds; the user has
    a real chronic imbalance and watches whether fatigue amplifies it.
  • easy_purity — % of Easy-labeled time actually spent at Easy HR.

Whole-run (and even per-segment) HR drift is deliberately ABSENT: it
died twice in design review — structured runs + out-and-back terrain
break the whole-run version, and the user races positive-split so the
segment version answers a question they never ask.

Shaping rule (memory: data-shape-for-ai): numeric and preformatted
fields side by side; one shape feeds the UI verdict rows AND the agent.
Pure computation lives in the module-level functions (testable on
synthetic frames); `compute_run_verdicts` is the thin loader.
"""

from __future__ import annotations

import os
import json
from typing import Any

import numpy as np
import pandas as pd

# --- Tunables (referenced from tests; keep names stable) -------------------

# HR needs time to respond after a lap-button press — samples inside the
# first SETTLE_SEC of a labeled block don't count toward mismatch/purity.
SETTLE_SEC = 60
# Smoothing windows: 45 s for zone-mismatch (kills spikes, keeps sustained
# excursions), 15 s for recovery (HRR60 needs sharpness).
SMOOTH_MISMATCH_S = 45
SMOOTH_MISMATCH = f"{SMOOTH_MISMATCH_S}s"
SMOOTH_RECOVERY = "15s"
# The trailing smoother remembers SMOOTH_MISMATCH_S of pre-settle HR, so
# mismatch/purity accounting starts a full smoother-length after the
# settle boundary — otherwise warmup samples leak "below band" time into
# the block through the rolling mean.
MISMATCH_AMNESTY_S = SETTLE_SEC + SMOOTH_MISMATCH_S
# A zone-band crossing only counts when the smoothed HR clears the band
# edge by this much — user zones are hand-annotated, ±2 bpm is noise.
ZONE_TOL_BPM = 2.0
# Sustained out-of-band time (per block) that flips label_vs_objective
# to attention.
MISMATCH_ATTENTION_MIN = 3.0
# Rest-recovery gates: the rest block must last long enough to measure
# HRR60, and the HR entering it must show actual hard work.
REST_MIN_DURATION_S = 75
REST_MIN_START_HR = 140
# L/R gates + thresholds (percent points of deviation from 50).
LR_MIN_COVERAGE_S = 900
LR_ATTENTION_DEV = 1.5
LR_ATTENTION_GROWTH = 1.0
# Easy purity: gate on total labeled-Easy time, attention threshold.
EASY_MIN_TOTAL_S = 480
EASY_PURITY_ATTENTION_PCT = 80.0
# Samples further apart than this are a recording pause, not elapsed
# training time — cap the credited dt.
MAX_SAMPLE_GAP_S = 15.0

EASY_LABEL = "Hold Back Easy"
REST_LABEL = "Rest"


def _fmt_min(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}:{s:02d}"


def _smoothed(df: pd.DataFrame, column: str, window: str) -> np.ndarray:
    idx = pd.to_timedelta(df["Second"], unit="s")
    return (
        df[column].astype(float).set_axis(idx).rolling(window, min_periods=1)
        .mean().to_numpy()
    )


def _dts(seconds: np.ndarray) -> np.ndarray:
    """Per-sample credited duration: forward diff, gap-capped, last
    sample credited the median step."""
    if len(seconds) < 2:
        return np.full(len(seconds), 1.0)
    d = np.diff(seconds).astype(float)
    med = float(np.median(d)) if len(d) else 1.0
    d = np.append(d, med)
    return np.clip(d, 0.0, MAX_SAMPLE_GAP_S)


def segments_from_laps(laps: list[dict], categories: list[str]) -> list[dict]:
    """Merge consecutive same-label laps into blocks on the cumulative
    lap-duration clock (the same clock get_activity_telemetry uses for
    its Lap column, so block windows line up with telemetry Seconds)."""
    blocks: list[dict] = []
    cursor = 0.0
    for i, lap in enumerate(laps):
        dur = float(lap.get("duration") or 0)
        label = categories[i] if i < len(categories) else None
        if blocks and blocks[-1]["label"] == label:
            blocks[-1]["end_sec"] = cursor + dur
        else:
            blocks.append({"label": label, "start_sec": cursor, "end_sec": cursor + dur})
        cursor += dur
    for b in blocks:
        b["duration_sec"] = b["end_sec"] - b["start_sec"]
    return blocks


def _longest_true_window(
    seconds: np.ndarray, dts: np.ndarray, mask: np.ndarray
) -> tuple[float, dict | None]:
    """Total masked time + the longest contiguous masked stretch (for
    the UI anchor). Returns (total_sec, {start_sec, end_sec} | None)."""
    total = float(dts[mask].sum())
    best = None
    run_start = None
    run_len = 0.0
    best_len = 0.0
    for i, on in enumerate(mask):
        if on:
            if run_start is None:
                run_start = seconds[i]
                run_len = 0.0
            run_len += dts[i]
            if run_len > best_len:
                best_len = run_len
                best = {"start_sec": float(run_start), "end_sec": float(seconds[i] + dts[i])}
        else:
            run_start = None
    return total, best


def label_vs_objective(
    blocks: list[dict], zones: list[dict], df: pd.DataFrame
) -> dict | None:
    """Per labeled block (non-Rest, non-Easy): sustained time the
    smoothed HR spent outside the labeled zone band. Returns None when
    no block has a matching zone (gate not met)."""
    zone_by_label = {z["rpe_label"]: z for z in zones}
    hr = _smoothed(df, "HeartRate", SMOOTH_MISMATCH)
    seconds = df["Second"].to_numpy(dtype=float)
    dts = _dts(seconds)

    details = []
    for b in blocks:
        z = zone_by_label.get(b["label"])
        if z is None or b["label"] in (REST_LABEL, EASY_LABEL):
            continue
        m_block = (
            (seconds >= b["start_sec"] + MISMATCH_AMNESTY_S)
            & (seconds < b["end_sec"])
            & ~np.isnan(hr)
        )
        if not m_block.any():
            continue
        above = m_block & (hr > z["high"] + ZONE_TOL_BPM)
        below = m_block & (hr < z["low"] - ZONE_TOL_BPM)
        above_s, above_win = _longest_true_window(seconds, dts, above)
        below_s, below_win = _longest_true_window(seconds, dts, below)
        block_s = float(dts[m_block].sum())
        details.append({
            "label": b["label"],
            "start_sec": b["start_sec"],
            "end_sec": b["end_sec"],
            "zone_low": z["low"],
            "zone_high": z["high"],
            "duration_min": round(block_s / 60, 1),
            "minutes_above": round(above_s / 60, 1),
            "minutes_below": round(below_s / 60, 1),
            "pct_in_band": round(100 * (1 - (above_s + below_s) / block_s), 1),
            "worst_above": above_win,
            "worst_below": below_win,
        })

    if not details:
        return None

    worst = max(details, key=lambda d: max(d["minutes_above"], d["minutes_below"]))
    worst_min = max(worst["minutes_above"], worst["minutes_below"])
    attention = worst_min >= MISMATCH_ATTENTION_MIN
    if attention:
        direction = "高" if worst["minutes_above"] >= worst["minutes_below"] else "低"
        window = (
            worst["worst_above"] if direction == "高" else worst["worst_below"]
        )
        summary = (
            f"标 {worst['label']} 的 {worst['duration_min']:g} 分钟里，"
            f"有 {worst_min:g} 分钟 HR 持续偏{direction}"
            f"（区间 {worst['zone_low']}–{worst['zone_high']}）"
        )
        anchor = window
    else:
        tightest = min(details, key=lambda d: d["pct_in_band"])
        summary = (
            f"无持续越界 — 最紧的 {tightest['label']} 段 "
            f"{tightest['pct_in_band']:g}% 在区内，出界都是短暂波动"
        )
        anchor = None
    return {
        "key": "label_vs_objective",
        "title": "标注 vs 客观",
        "status": "attention" if attention else "ok",
        "summary": summary,
        "anchor": anchor,
        "data": {"segments": details},
    }


def rest_recovery_drops(
    laps: list[dict], categories: list[str], df: pd.DataFrame
) -> list[dict]:
    """HRR60 per qualifying Rest transition: HR at rest start minus HR
    ~60 s in. Shared with seed_models.refit_rest_recovery_baseline so
    the per-run verdict and the baseline agree on the definition."""
    blocks = segments_from_laps(laps, categories)
    hr = _smoothed(df, "HeartRate", SMOOTH_RECOVERY)
    seconds = df["Second"].to_numpy(dtype=float)

    def hr_at(t: float) -> float | None:
        m = ~np.isnan(hr) & (np.abs(seconds - t) <= 10)
        if not m.any():
            return None
        return float(hr[np.abs(np.where(m, seconds, np.inf) - t).argmin()])

    def hr_peak_before(t: float) -> float | None:
        # HRR60's start point is the peak at the END of the work block —
        # the smoothed value AT the boundary already averages in the
        # first rest samples and understates the drop.
        m = ~np.isnan(hr) & (seconds >= t - 20) & (seconds < t)
        if not m.any():
            return None
        return float(hr[m].max())

    drops = []
    for i, b in enumerate(blocks):
        if b["label"] != REST_LABEL or i == 0:
            continue
        if blocks[i - 1]["label"] in (REST_LABEL, None):
            continue
        if b["duration_sec"] < REST_MIN_DURATION_S:
            continue
        h0 = hr_peak_before(b["start_sec"])
        h60 = hr_at(b["start_sec"] + 60)
        if h0 is None or h60 is None or h0 < REST_MIN_START_HR:
            continue
        drops.append({
            "start_sec": b["start_sec"],
            "from_label": blocks[i - 1]["label"],
            "hr_start": round(h0),
            "hr_60s": round(h60),
            "drop_bpm": round(h0 - h60),
        })
    return drops


def rest_recovery(
    laps: list[dict],
    categories: list[str],
    df: pd.DataFrame,
    baseline: dict | None = None,
) -> dict | None:
    """Run-level HRR60 verdict. `baseline` is the params_json of the
    `hrr.rest_recovery_baseline` model (or None before one exists)."""
    drops = rest_recovery_drops(laps, categories, df)
    if not drops:
        return None
    median_drop = float(np.median([d["drop_bpm"] for d in drops]))
    low_warn = (baseline or {}).get("low_warning")
    attention = low_warn is not None and median_drop < low_warn
    if baseline:
        base_str = (
            f"你的基线 {baseline.get('mean')}±{baseline.get('sd')}"
            if low_warn is None
            else f"你的基线 {low_warn:g}–{baseline.get('high_warning'):g}"
        )
    else:
        base_str = "还没有基线（历史带 Rest 的课不足）"
    worst = min(drops, key=lambda d: d["drop_bpm"])
    anchor_drop = worst if attention else drops[0]
    return {
        "key": "rest_recovery",
        "title": "Rest 段恢复",
        "status": "attention" if attention else "ok",
        "summary": f"组间 60 秒掉 {median_drop:g} bpm · {base_str}",
        "anchor": {
            "start_sec": anchor_drop["start_sec"],
            "end_sec": anchor_drop["start_sec"] + 60,
        },
        "data": {
            "median_drop_bpm": median_drop,
            "n_rests": len(drops),
            "drops": drops,
            "baseline": baseline,
        },
    }


def lr_asymmetry(df: pd.DataFrame) -> dict | None:
    """Ground-contact balance by thirds, as signed deviation from 50
    (positive = left-heavy). The only verdict that needs no labels."""
    if "GroundContactBalanceLeft" not in df.columns:
        return None
    valid = df[df["GroundContactBalanceLeft"].notna()]
    if valid.empty:
        return None
    seconds = valid["Second"].to_numpy(dtype=float)
    dts = _dts(seconds)
    if dts.sum() < LR_MIN_COVERAGE_S:
        return None
    bal = valid["GroundContactBalanceLeft"].to_numpy(dtype=float)
    t0, t1 = seconds[0], seconds[-1]
    edges = np.linspace(t0, t1, 4)
    thirds = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (seconds >= lo) & (seconds <= hi)
        if not m.any():
            # A recording gap can leave a third empty — without all
            # three thirds the fatigue comparison is meaningless.
            return None
        mean_left = float(np.average(bal[m], weights=np.maximum(dts[m], 1e-9)))
        thirds.append({
            "left_pct": round(mean_left, 1),
            "dev": round(mean_left - 50.0, 1),
        })
    dev1, dev3 = thirds[0]["dev"], thirds[2]["dev"]
    growth = round(abs(dev3) - abs(dev1), 1)
    attention = abs(dev3) >= LR_ATTENTION_DEV or (
        growth >= LR_ATTENTION_GROWTH and abs(dev3) >= 1.0
    )
    side = "左" if dev3 >= 0 else "右"

    def _fmt_dev(d: float) -> str:
        return f"+{abs(d):g}% {'左' if d >= 0 else '右'}" if d else "±0"

    devs = " → ".join(_fmt_dev(t["dev"]) for t in thirds)
    tail = " — 疲劳在放大不对称" if attention and growth >= LR_ATTENTION_GROWTH else ""
    return {
        "key": "lr_asymmetry",
        "title": "L/R 疲劳不对称",
        "status": "attention" if attention else "ok",
        "summary": f"前⅓ {_fmt_dev(dev1)} · 后⅓ {_fmt_dev(dev3)}{tail}",
        "anchor": {"start_sec": float(edges[2]), "end_sec": float(edges[3])},
        "data": {
            "thirds": thirds,
            "growth": growth,
            "side_late": side,
            "detail": devs,
        },
    }


def easy_purity(
    blocks: list[dict], zones: list[dict], df: pd.DataFrame
) -> dict | None:
    """% of Easy-labeled time (post settle-in) with HR at or under the
    Easy band's ceiling."""
    easy_zone = next((z for z in zones if z["rpe_label"] == EASY_LABEL), None)
    easy_blocks = [b for b in blocks if b["label"] == EASY_LABEL]
    if easy_zone is None or not easy_blocks:
        return None
    hr = _smoothed(df, "HeartRate", SMOOTH_MISMATCH)
    seconds = df["Second"].to_numpy(dtype=float)
    dts = _dts(seconds)
    ceiling = easy_zone["high"] + ZONE_TOL_BPM

    total_s = 0.0
    out_s = 0.0
    worst_window = None
    worst_len = 0.0
    for b in easy_blocks:
        m = (
            (seconds >= b["start_sec"] + MISMATCH_AMNESTY_S)
            & (seconds < b["end_sec"])
            & ~np.isnan(hr)
        )
        total_s += float(dts[m].sum())
        over = m & (hr > ceiling)
        over_s, win = _longest_true_window(seconds, dts, over)
        out_s += over_s
        if win is not None:
            win_len = win["end_sec"] - win["start_sec"]
            if win_len > worst_len:
                worst_len, worst_window = win_len, win
    if total_s < EASY_MIN_TOTAL_S:
        return None
    purity = round(100 * (1 - out_s / total_s), 1)
    attention = purity < EASY_PURITY_ATTENTION_PCT
    total_min = round(total_s / 60)
    return {
        "key": "easy_purity",
        "title": "Easy 纯度",
        "status": "attention" if attention else "ok",
        "summary": (
            f"标 Easy 的 {total_min} 分钟里 {purity:g}% "
            f"在 ≤{easy_zone['high']} bpm 内"
        ),
        "anchor": worst_window if attention else None,
        "data": {
            "purity_pct": purity,
            "easy_minutes": total_min,
            "out_of_band_min": round(out_s / 60, 1),
            "ceiling_bpm": ceiling,
        },
    }


# --- Loader ----------------------------------------------------------------

_TITLES = {
    "label_vs_objective": "标注 vs 客观",
    "rest_recovery": "Rest 段恢复",
    "lr_asymmetry": "L/R 疲劳不对称",
    "easy_purity": "Easy 纯度",
}


def compute_run_verdicts(
    processor, activity_id: int, memory_engine=None
) -> dict[str, Any]:
    """Load laps/labels/zones/telemetry for one run and fire whichever
    verdicts qualify. Verdicts sorted attention-first; everything that
    didn't fire is listed with its reason so the agent can tell
    'checked, fine' from 'couldn't check'."""
    not_fired: list[dict] = []

    def skip(key: str, reason: str) -> None:
        not_fired.append({"key": key, "title": _TITLES[key], "reason": reason})

    laps = processor.get_run_laps(activity_id)
    df_raw = None
    if laps:
        df_raw, _ = processor.get_activity_telemetry(activity_id, laps=laps)
    if df_raw is None or len(df_raw) == 0 or df_raw["HeartRate"].isna().all():
        for key in _TITLES:
            skip(key, "无遥测数据" if laps else "无分段数据")
        return {"activity_id": activity_id, "verdicts": [], "not_fired": not_fired}

    meta_path = os.path.join(
        processor.paths["manual"], f"run_{activity_id}_meta.json"
    )
    categories: list[str] = []
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                categories = json.load(f).get("lap_categories") or []
        except Exception:
            categories = []
    zones = processor.get_hr_zones()
    blocks = segments_from_laps(laps, categories)

    verdicts: list[dict] = []
    if not categories:
        for key in ("label_vs_objective", "rest_recovery", "easy_purity"):
            skip(key, "本次未标注强度")
    elif not zones:
        for key in ("label_vs_objective", "easy_purity"):
            skip(key, "未配置 HR 区间")
    if categories:
        if zones:
            v = label_vs_objective(blocks, zones, df_raw)
            verdicts.append(v) if v else skip(
                "label_vs_objective", "无可对照的非 Easy 标注段"
            )
            v = easy_purity(blocks, zones, df_raw)
            verdicts.append(v) if v else skip(
                "easy_purity", "无足量的 Easy 标注段"
            )
        baseline = None
        if memory_engine is not None:
            model = memory_engine.get_model("hrr.rest_recovery_baseline")
            baseline = (model or {}).get("params_json")
        v = rest_recovery(laps, categories, df_raw, baseline=baseline)
        verdicts.append(v) if v else skip(
            "rest_recovery", "无符合条件的 Rest 段（需在高强度后、≥75 秒）"
        )
    v = lr_asymmetry(df_raw)
    verdicts.append(v) if v else skip(
        "lr_asymmetry", "无 L/R 数据或时长不足（需胸带 + ≥15 分钟）"
    )

    verdicts.sort(key=lambda v: (v["status"] != "attention"))
    return {
        "activity_id": activity_id,
        "verdicts": verdicts,
        "not_fired": not_fired,
    }
