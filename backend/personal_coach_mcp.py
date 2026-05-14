"""Personal Coach MCP server — v2 (coach-curated, three-stream).

Exposes the user's training, health, and calendar data to LLM agents via
the Model Context Protocol. Tool outputs follow the design in
docs/mcp_tools_design.md:

- All effort-related fields are nested under `objective` / `perceived` /
  `planned`. Never collapsed (see feedback_perceived_vs_intent.md).
- HR zones use the user's RPE-named bands from
  data/manual_inputs/user_zones.json — NOT Garmin's generic Z1-Z5,
  which doesn't align with `manual_meta.lap_categories`.
- Telemetry returns the bucket frame (with `elevation_change_m` per
  bucket) the AI needs to reason about HR-vs-climb causality.
- Garmin payload noise (OAuth scopes, profile image URLs, dive info)
  is stripped before payloads reach the LLM.

HTTP indirection (rather than `import data_processor`): two live
DataProcessor instances on the same JSON files invites concurrency
bugs; the existing api_server endpoints are the single source of truth.

Run: `uv run python -m backend.personal_coach_mcp` (stdio transport).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("PERSONAL_COACH_API_BASE", "http://127.0.0.1:8765")
HTTP_TIMEOUT = 60.0  # seconds

mcp = FastMCP("personal-coach")


# =============================================================================
# HTTP helpers
# =============================================================================

async def _get(path: str, **params: Any) -> Any:
    """GET an api_server endpoint, raise on non-2xx, return parsed JSON.
    None-valued params are dropped so optional tool args don't clutter
    query strings."""
    clean = {k: v for k, v in params.items() if v is not None}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(f"{API_BASE}{path}", params=clean)
        r.raise_for_status()
        return r.json()


# =============================================================================
# Pace formatting (double-track: dec + str everywhere)
# =============================================================================

def _pace_str_from_dec(dec: float | None) -> str | None:
    if dec is None or not (dec > 0):
        return None
    return f"{int(dec)}:{int((dec % 1) * 60):02d}"


def _format_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds <= 0:
        return None
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _split_pace_dec(distance_m: float, duration_s: float) -> float | None:
    if distance_m <= 0 or duration_s <= 0:
        return None
    miles = distance_m / 1609.34
    return round(duration_s / 60 / miles, 2)


# =============================================================================
# 1. Profile / athlete identity
# =============================================================================

@mcp.tool()
async def get_athlete_profile() -> dict:
    """Composite athlete profile for AI coaching.

    Returns identity (age / sex / weight_kg / height_cm), fitness
    (vo2max_running, lactate_threshold_hr, lactate_threshold_pace),
    the user's `medium_term_hr_effort_map` (RPE-named HR bands from
    data/manual_inputs/user_zones.json — the user's *expected* HR ↔
    effort mapping over the current few months), the current cycle
    (block) with phase (base/build/peak/taper), and preferences /
    medical notes from semantic memory.

    `medium_term_hr_effort_map[].rpe_label` is the SAME vocabulary the
    user uses in `manual_meta.lap_categories` (e.g. "Steady Effort",
    "LT Effort"). They are NOT the same data, just same words:
      - effort_map entry = HR range for a band, stable for months
      - lap_categories entry = the user's per-lap label for THIS run
    The agent compares them: if the user's per-lap label says "Steady
    Effort" but the lap's avg_hr fell in the "LT Effort" band on the
    map, that's a coaching signal.
    """
    raw = await _get("/api/athlete/profile")
    # Project: rename `fitness.hr_zones` → `fitness.medium_term_hr_effort_map`
    # so the agent's prompt + tool output share one explicit name for
    # the same concept. The underlying api endpoint keeps the
    # `hr_zones` key — frontend (Setup tab) reads it directly, and we
    # don't want to ripple a rename out there.
    fitness = dict(raw.get("fitness") or {})
    if "hr_zones" in fitness:
        fitness["medium_term_hr_effort_map"] = fitness.pop("hr_zones")
    return {**raw, "fitness": fitness}


# =============================================================================
# 2. Recent state
# =============================================================================

@mcp.tool()
async def get_readiness(date: str | None = None) -> dict:
    """Readiness signal for a single day with green/yellow/red verdict +
    rationale.

    Optional `date` defaults to today (YYYY-MM-DD). Returns today's
    sleep/RHR/HRV/stress, the 7-day rolling baselines, percent deltas,
    and the 7-day history rows. Rule:
      green  - HRV ±5%, RHR ±5%, sleep ≥ 7h
      red    - HRV down >10% OR RHR up >10% OR sleep < 5h
      yellow - everything in between

    Use to decide whether the user can train hard today.
    """
    return await _get("/api/health/readiness", date=date)


@mcp.tool()
async def get_training_load(window_days: int = 28) -> dict:
    """Acute (7-day) vs chronic (window_days) training load + ACWR ratio
    + weekly mileage trend across the window.

    ACWR bands: < 0.8 detraining, 0.8-1.3 sweet, 1.3-1.5 caution,
    > 1.5 danger. Surfaces injury-risk before prescribing.

    Use to assess fitness trajectory and current load level — not for
    per-run detail (use list_runs for that).
    """
    return await _get("/api/training/load", window=window_days)


# =============================================================================
# 3. Runs — coach-curated, three-stream
# =============================================================================

def _trim_run_summary(r: dict) -> dict:
    """Compact list_runs entry. ~12 fields, three-stream nested."""
    miles = (r.get("distance") or 0) / 1609.34
    moving_s = r.get("movingDuration") or r.get("duration") or 0
    pace_dec = (moving_s / 60 / miles) if miles > 0 else None
    elev_ft = round((r.get("elevationGain") or 0) * 3.281)

    meta = r.get("manual_meta") or {}
    cat = meta.get("category_stats") or []
    perceived_breakdown = [
        {
            "category": c.get("category"),
            "miles": c.get("distance_mi"),
            "pace_str": c.get("pace"),
        }
        for c in cat
    ]

    type_key = (r.get("activityType") or {}).get("typeKey", "running")
    return {
        "id": r.get("activityId"),
        "name": r.get("activityName"),
        "date": (r.get("startTimeLocal") or "")[:10],
        "start_time": (r.get("startTimeLocal") or "")[11:16] or None,
        "type": type_key,
        "summary": {
            "distance_mi": round(miles, 2),
            "moving_time": _format_duration(moving_s),
            "avg_pace_str": _pace_str_from_dec(pace_dec),
            "avg_pace_dec": round(pace_dec, 2) if pace_dec else None,
            "elevation_gain_ft": elev_ft,
        },
        "objective": {
            # Raw sensor only. Garmin's interpretive label fields
            # (aerobicTrainingEffect, anaerobicTrainingEffect,
            # activityTrainingLoad, trainingEffectLabel) are filtered
            # at this layer per docs/IMPROVEMENTS.md §2 — the agent
            # reasons from HR + pace + the user's own perceived
            # labels, not Garmin's derived guesses.
            "avg_hr": r.get("averageHR"),
            "max_hr": r.get("maxHR"),
        },
        "perceived": {
            "category_breakdown": perceived_breakdown,
            "notes": (meta.get("notes") or "") or None,
            "labeled_at": meta.get("updated_at"),
        },
        "planned": None,
    }


@mcp.tool()
async def list_runs(start: str, end: str) -> dict:
    """List runs in [start, end] (YYYY-MM-DD inclusive) with three-stream
    summaries.

    Each run is ~12 fields: id/name/date/start_time/type, a summary block
    (distance/time/pace/elev), and the three streams `objective`
    (Garmin sensor truth), `perceived` (RPE labels + notes from
    manual_meta), `planned` (always null in Phase 1).

    Use to see the narrative of recent training. For aggregate load,
    use get_training_load instead.
    """
    raw = await _get("/api/runs", start=start, end=end)
    return {
        "start": raw.get("start"),
        "end": raw.get("end"),
        "runs": [_trim_run_summary(r) for r in raw.get("runs", [])],
    }


def _zones_time_min(
    telemetry_rows: list[dict],
    zones: list[dict],
) -> list[dict]:
    """Sum seconds per HR zone (RPE-named) across raw telemetry rows.
    Returns minutes-per-zone + percent-of-total."""
    if not telemetry_rows or not zones:
        return []
    counts = [0 for _ in zones]
    for row in telemetry_rows:
        hr = row.get("HeartRate")
        if hr is None or hr <= 0:
            continue
        for i, z in enumerate(zones):
            if z["low"] <= hr <= z["high"]:
                counts[i] += 1
                break
    total = sum(counts) or 1
    out = []
    for z, n in zip(zones, counts):
        out.append({
            "name": z["name"],
            "rpe_label": z["rpe_label"],
            "minutes": round(n / 60, 1),
            "pct": round(n / total * 100, 1),
        })
    return out


def _hr_drift(telemetry_rows: list[dict]) -> dict:
    """Coach-style drift block: HR change first-third → last-third with
    side-by-side elevation gain so the AI can attribute the rise."""
    rows = [r for r in telemetry_rows if r.get("HeartRate") is not None]
    if len(rows) < 30:
        return {}
    n = len(rows)
    first = rows[: n // 3]
    last = rows[-(n // 3):]

    def _avg_hr(group: list[dict]) -> float:
        vals = [r["HeartRate"] for r in group if r["HeartRate"]]
        return round(sum(vals) / len(vals), 1) if vals else 0.0

    def _elev_gain_m(group: list[dict]) -> float:
        # Ascending only — count positive deltas between consecutive
        # rows to mirror Garmin's elevationGain semantics.
        gain = 0.0
        prev = None
        for r in group:
            cur = r.get("Elevation")
            if cur is None:
                continue
            if prev is not None and cur > prev:
                gain += cur - prev
            prev = cur
        return round(gain, 1)

    def _duration_min(group: list[dict]) -> float:
        if not group:
            return 0.0
        secs = (group[-1].get("Second") or 0) - (group[0].get("Second") or 0)
        return round(secs / 60, 1)

    a, b = _avg_hr(first), _avg_hr(last)
    drift_pct = round((b - a) / a * 100, 1) if a else 0.0
    return {
        "hr_drift_pct": drift_pct,
        "first_third": {
            "hr_avg": a,
            "elev_gain_m": _elev_gain_m(first),
            "duration_min": _duration_min(first),
        },
        "last_third": {
            "hr_avg": b,
            "elev_gain_m": _elev_gain_m(last),
            "duration_min": _duration_min(last),
        },
    }


@mcp.tool()
async def get_run_detail(activity_id: int) -> dict:
    """Coach-curated single-run view. Three streams nested
    (objective/perceived/planned), inline weather, splits, HR zones
    against the user's RPE-named bands.

    For high-resolution per-bucket telemetry (HR-vs-elevation reasoning),
    call get_run_telemetry separately.
    """
    detail = await _get(f"/api/runs/{activity_id}")
    laps_payload = await _get(f"/api/runs/{activity_id}/laps")
    try:
        weather = await _get(f"/api/runs/{activity_id}/weather")
    except httpx.HTTPStatusError:
        weather = None
    profile = await _get("/api/athlete/profile")
    zones = (profile.get("fitness") or {}).get("hr_zones") or []

    # Pull telemetry to compute zones-against-user-bands and drift.
    try:
        tel = await _get(
            f"/api/runs/{activity_id}/telemetry", downsample_sec=10
        )
        tel_rows = tel.get("raw") or []
    except Exception:
        tel_rows = []

    run = detail.get("run") or {}
    laps = laps_payload.get("laps") or []
    meta = run.get("manual_meta") or {}

    miles = (run.get("distance") or 0) / 1609.34
    moving_s = run.get("movingDuration") or run.get("duration") or 0
    pace_dec = (moving_s / 60 / miles) if miles > 0 else None

    splits = []
    for i, lap in enumerate(laps):
        sp_dec = _split_pace_dec(
            lap.get("distance") or 0, lap.get("duration") or 0
        )
        splits.append({
            "lap": i + 1,
            "distance_mi": round((lap.get("distance") or 0) / 1609.34, 2),
            "pace_str": _pace_str_from_dec(sp_dec),
            "pace_dec": sp_dec,
            "hr_avg": lap.get("averageHR"),
            "hr_max": lap.get("maxHR"),
            "elev_gain_ft": round((lap.get("elevationGain") or 0) * 3.281),
            "rpe_label": lap.get("category"),
        })

    splits_pattern = None
    if len(splits) >= 4 and all(s.get("pace_dec") for s in splits):
        half = len(splits) // 2
        first_half_avg = sum(s["pace_dec"] for s in splits[:half]) / half
        last_half_avg = sum(s["pace_dec"] for s in splits[-half:]) / half
        delta = last_half_avg - first_half_avg
        if abs(delta) < 0.1:
            splits_pattern = "Even split"
        elif delta < 0:
            splits_pattern = (
                f"Negative split: avg {_pace_str_from_dec(first_half_avg)} "
                f"first half → {_pace_str_from_dec(last_half_avg)} last half"
            )
        else:
            splits_pattern = (
                f"Positive split: avg {_pace_str_from_dec(first_half_avg)} "
                f"first half → {_pace_str_from_dec(last_half_avg)} last half"
            )

    return {
        "id": run.get("activityId"),
        "name": run.get("activityName"),
        "date": (run.get("startTimeLocal") or "")[:10],
        "start_time": (run.get("startTimeLocal") or "")[11:16] or None,
        "type": (run.get("activityType") or {}).get("typeKey"),

        "summary": {
            "distance_mi": round(miles, 2),
            "moving_time": _format_duration(moving_s),
            "elapsed_time": _format_duration(run.get("elapsedDuration")),
            "avg_pace_str": _pace_str_from_dec(pace_dec),
            "avg_pace_dec": round(pace_dec, 2) if pace_dec else None,
            "elevation_gain_ft": round((run.get("elevationGain") or 0) * 3.281),
            "elevation_loss_ft": round((run.get("elevationLoss") or 0) * 3.281),
            "calories": run.get("calories"),
        },

        "objective": {
            "heart_rate": {
                "avg": run.get("averageHR"),
                "max": run.get("maxHR"),
                "zones_min": _zones_time_min(tel_rows, zones),
            },
            "drift": _hr_drift(tel_rows),
            # Garmin's training_effect block (aerobicTrainingEffect /
            # anaerobicTrainingEffect / activityTrainingLoad /
            # trainingEffectLabel / aerobicTrainingEffectMessage) used
            # to ship here. Filtered at the MCP layer per §2: the
            # agent reasons from HR zones + pace + the user's own
            # perceived labels, not Garmin's interpretive guesses.
            "power": {
                "avg": run.get("avgPower"),
                "max": run.get("maxPower"),
                "normalized": run.get("normPower"),
            },
            "form": {
                "cadence_avg": run.get(
                    "averageRunningCadenceInStepsPerMinute"
                ),
                "ground_contact_ms": run.get("avgGroundContactTime"),
                "stride_length_cm": run.get("avgStrideLength"),
                "vertical_oscillation_cm": run.get("avgVerticalOscillation"),
            },
            "splits": splits,
            "splits_pattern": splits_pattern,
        },

        "perceived": {
            "category_breakdown": [
                {
                    "category": c.get("category"),
                    "miles": c.get("distance_mi"),
                    "pace_str": c.get("pace"),
                    "avg_hr": c.get("avg_hr"),
                }
                for c in (meta.get("category_stats") or [])
            ],
            "lap_rpe": meta.get("lap_categories") or [],
            "notes": (meta.get("notes") or "") or None,
            "labeled_at": meta.get("updated_at"),
        },

        "planned": None,

        "weather": (
            {
                "temp_f": weather.get("temperature_f"),
                "feels_like_f": weather.get("apparent_temperature_f"),
                "humidity_pct": weather.get("humidity_pct"),
                "dew_point_f": weather.get("dew_point_f"),
                "wind_mph": weather.get("wind_mph"),
            }
            if weather else None
        ),
    }


def _summarise_bucket(rows: list[dict]) -> dict:
    """Aggregate one bucket of raw telemetry rows. Mirrors df_ai design
    from data_processor.get_activity_telemetry — pace_str +
    elevation_change_m are AI-friendly forms."""
    if not rows:
        return {}
    elev = [r.get("Elevation") for r in rows if r.get("Elevation") is not None]
    elev_change = round(elev[-1] - elev[0], 1) if len(elev) >= 2 else 0.0

    def _avg(field: str) -> float | None:
        vals = [r.get(field) for r in rows if r.get(field) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    pace_dec = _avg("Pace")
    return {
        "second_start": rows[0].get("Second"),
        "lap": rows[0].get("Lap"),
        "distance_mi": (
            round(rows[0].get("Distance"), 2)
            if rows[0].get("Distance") is not None else None
        ),
        "pace_str": _pace_str_from_dec(pace_dec),
        "pace_dec": round(pace_dec, 2) if pace_dec else None,
        "heart_rate": (
            int(_avg("HeartRate")) if _avg("HeartRate") is not None else None
        ),
        "cadence": (
            int(_avg("Cadence")) if _avg("Cadence") is not None else None
        ),
        "elevation_change_m": elev_change,
    }


@mcp.tool()
async def get_run_telemetry(
    activity_id: int, downsample_sec: int = 30
) -> dict:
    """High-resolution telemetry as a per-`downsample_sec` bucket frame:
    HR / pace / cadence / **elevation_change_m** (delta in this window,
    not absolute altitude). One row → 'climbed 4m, HR 158→168, pace 9:30'
    is directly readable for AI causal reasoning.

    Includes a `drift` block: HR change first-third → last-third with
    elevation gain side-by-side so AI can attribute rises to fatigue
    vs climbing.
    """
    raw = await _get(
        f"/api/runs/{activity_id}/telemetry", downsample_sec=downsample_sec
    )
    raw_rows = raw.get("raw") or []
    summary = raw.get("summary") or {}

    buckets: list[dict] = []
    if raw_rows:
        block: list[dict] = []
        block_idx = -1
        for row in raw_rows:
            sec = row.get("Second") or 0
            this_block = sec // downsample_sec
            if this_block != block_idx:
                if block:
                    buckets.append(_summarise_bucket(block))
                block = []
                block_idx = this_block
            block.append(row)
        if block:
            buckets.append(_summarise_bucket(block))

    pace_summary = summary.get("Pace") or {}
    return {
        "activity_id": activity_id,
        "downsample_sec": downsample_sec,
        "lap_count": max(
            (b.get("lap") or 0 for b in buckets), default=0
        ),
        "total_buckets": len(buckets),
        "summary": {
            "heart_rate": summary.get("HeartRate"),
            "pace": {
                "avg_dec": (
                    round(pace_summary.get("avg"), 2)
                    if pace_summary.get("avg") else None
                ),
                "avg_str": _pace_str_from_dec(pace_summary.get("avg")),
                "min_dec": (
                    round(pace_summary.get("min"), 2)
                    if pace_summary.get("min") else None
                ),
                "min_str": _pace_str_from_dec(pace_summary.get("min")),
                "max_dec": (
                    round(pace_summary.get("max"), 2)
                    if pace_summary.get("max") else None
                ),
                "max_str": _pace_str_from_dec(pace_summary.get("max")),
            },
            "cadence": summary.get("Cadence"),
            "elevation_m": summary.get("Elevation"),
        },
        "drift": _hr_drift(raw_rows),
        "buckets": buckets,
    }


@mcp.tool()
async def get_run_weather(activity_id: int) -> dict:
    """Weather snapshot for a run (temp / feels-like / humidity / dew point
    / wind, all in F/mph). Already inlined on get_run_detail — use this
    only when starting from an activity_id without other context."""
    raw = await _get(f"/api/runs/{activity_id}/weather")
    return {
        "activity_id": activity_id,
        "temp_f": raw.get("temperature_f"),
        "feels_like_f": raw.get("apparent_temperature_f"),
        "humidity_pct": raw.get("humidity_pct"),
        "dew_point_f": raw.get("dew_point_f"),
        "wind_mph": raw.get("wind_mph"),
        "source": raw.get("source"),
        "fetched_at": raw.get("fetched_at"),
    }


# =============================================================================
# 4. Training cycle / blocks / monthly
# =============================================================================

@mcp.tool()
async def list_blocks() -> dict:
    """List all training blocks (cycles) plus today's active block id."""
    return await _get("/api/training/blocks")


@mcp.tool()
async def get_cycle_stats(
    block_id: str, week_start: str, week_end: str
) -> dict:
    """Aggregate stats for the cycle containing `week_start..week_end`,
    plus the week-level summary. category_breakdown values come from
    `manual_meta.lap_categories` (perceived stream)."""
    raw = await _get(
        "/api/training/cycle-stats",
        block_id=block_id,
        week_start=week_start,
        week_end=week_end,
    )
    cy = raw.get("cycle") or {}
    if "category_breakdown" in cy:
        cy["category_breakdown"] = [
            {**row, "perceived_category": row.pop("effort", None)}
            for row in cy["category_breakdown"]
        ]
    return raw


@mcp.tool()
async def get_monthly_stats(activity_type: str = "running") -> dict:
    """Monthly aggregates over ALL of the user's history. activity_type:
    'running' (lumps all run-flavored types), 'lap_swimming',
    'stair_climbing', 'hiking', or 'all'."""
    return await _get(
        "/api/training/monthly-stats", activity_type=activity_type
    )


# =============================================================================
# 5. Manual activities
# =============================================================================

@mcp.tool()
async def list_manual_activities(start: str, end: str) -> dict:
    """User-logged non-Garmin activities in [start, end] (YYYY-MM-DD).
    Each entry has id/date/type/desc/duration_min/distance_mi and an
    optional start_time ('HH:MM') for calendar placement."""
    return await _get("/api/manual-activities", start=start, end=end)


@mcp.tool()
async def get_manual_activity(activity_id: str) -> dict:
    """Single manual activity by string id (e.g. 'manual_1777658163')."""
    return await _get(f"/api/manual-activities/{activity_id}")


# =============================================================================
# 6. Calendar
# =============================================================================

@mcp.tool()
async def get_calendar_events(start: str, end: str) -> dict:
    """Unified calendar events for [start, end] — Google Calendar (work,
    PT, etc.) + ManualActivity (timed) + Garmin runs. Each event has a
    `source` discriminator. Use to check user availability before
    suggesting workout times."""
    return await _get("/api/calendar/events", start=start, end=end)


@mcp.tool()
async def get_workout_plan(date: str) -> dict:
    """Planned workout for a specific date (YYYY-MM-DD), if any.

    Phase 1: always returns `{date, planned: null}` — the agent can rely
    on this stable shape. Phase 2 will populate from Google Calendar
    events tagged personalcoach.training=true.
    """
    return {"date": date, "planned": None}


# =============================================================================
# 7. Cognitive Memory Engine (CME)
# =============================================================================

@mcp.tool()
async def recall_topics(status: str = "active") -> dict:
    """List CME Topics. status: 'active' (Open + Testing — most useful),
    'resolved', 'conflicting' (highest priority — clarify before
    coaching), 'all'.

    Topics are state-machine-tracked threads of inquiry between user and
    coach. Use to follow up on past discussions ('how's the ITB feeling
    after last week's foam-rolling?')."""
    if status == "active":
        all_topics = (await _get("/api/memory/topics")).get("topics", [])
        topics = [
            t for t in all_topics if t.get("status") in ("Open", "Testing")
        ]
    elif status == "all":
        topics = (await _get("/api/memory/topics")).get("topics", [])
    else:
        api_status = status.capitalize()
        topics = (
            await _get("/api/memory/topics", status=api_status)
        ).get("topics", [])
    return {"topics": topics, "filter": status}


@mcp.tool()
async def search_episodes(keywords: list[str], limit: int = 10) -> dict:
    """Find past run / training episodes from Episodic Memory by keyword
    match. Returns 5W1H+E capsules with `lesson_learned` baked in.

    Use for 'have I had a similar run before?' recall — e.g.
    keywords=['hot', 'long run'] surfaces past hot-weather long runs
    with their lessons."""
    q = " ".join(keywords)
    return await _get("/api/memory/episodes/search", q=q, limit=limit)


@mcp.tool()
async def get_pending_clarifications() -> dict:
    """Unresolved clarification questions the agent owes the user.

    Always call at session start. If non-empty, ask the listed question
    BEFORE any other coaching — these are typically conflicts between
    the user's latest message and stored facts (e.g., "you said marathon
    pace 8:45 last week, now 8:30 — which is current?")."""
    return await _get("/api/memory/pending")


# =============================================================================
# Entry point
# =============================================================================

if __name__ == "__main__":
    mcp.run()
