"""Personal Coach MCP server — internal data tools.

Exposes the user's training, health, and calendar data to LLM agents via
the Model Context Protocol. Tools are thin httpx wrappers around the
existing FastAPI endpoints in api_server.py — single source of truth for
data access logic.

Why HTTP rather than direct DataProcessor import:
- Two live DataProcessor instances (the api-server's and ours) on the
  same JSON files invites concurrency bugs.
- HTTP endpoints already handle errors/format consistently. When the
  endpoint changes, this server inherits the change automatically.
- localhost roundtrip ~5ms; AI calls aren't hot-loop frequency.

Run: `uv run python -m personal_coach_mcp` (stdio transport).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("PERSONAL_COACH_API_BASE", "http://127.0.0.1:8765")
HTTP_TIMEOUT = 30.0  # seconds — generous so a slow telemetry decode doesn't kill a turn

mcp = FastMCP("personal-coach")


async def _get(path: str, **params: Any) -> Any:
    """Thin helper: GET, raise on non-2xx, parse JSON. params with None
    values are dropped so optional tool args don't clutter query strings."""
    clean = {k: v for k, v in params.items() if v is not None}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(f"{API_BASE}{path}", params=clean)
        r.raise_for_status()
        return r.json()


# =============================================================================
# Runs (Garmin-synced)
# =============================================================================

@mcp.tool()
async def list_runs(start: str, end: str) -> dict:
    """List Garmin running activities in [start, end] inclusive (YYYY-MM-DD).

    Returns: { start, end, runs: [...] } where each run has activityId,
    activityName, startTimeLocal (no offset), distance (metres),
    movingDuration (seconds), duration (seconds), elevationGain (metres),
    averageHR, activityType.typeKey, and manual_meta with the user's
    effort categories / lap labels / notes.
    """
    return await _get("/api/runs", start=start, end=end)


@mcp.tool()
async def get_run_detail(activity_id: int) -> dict:
    """Single run summary, manual_meta merged in, plus laps and chat history.

    Returns: { run: { activityId, manual_meta: {...}, ... }, laps: [...],
    chat_history: [...] }. Laps are objects with distance (m), duration
    (s), averageHR, elevationGain (m), category (effort label).
    """
    return await _get(f"/api/runs/{activity_id}")


@mcp.tool()
async def get_run_laps(activity_id: int) -> dict:
    """Per-lap breakdown for a single run. Same lap objects as
    get_run_detail's `laps` field, plus a `meta` payload of the user's
    saved effort categories. Use for fine-grained analysis when
    get_run_detail's other fields aren't needed."""
    return await _get(f"/api/runs/{activity_id}/laps")


@mcp.tool()
async def get_run_telemetry(
    activity_id: int, downsample_sec: int = 10
) -> dict:
    """High-resolution per-second telemetry — HR, pace, cadence,
    elevation, distance, etc. Server-side downsamples to one point per
    `downsample_sec` to keep payloads tractable.

    Returns: { raw: [...], ai: [...], summary: {...}, pace_clip: [low, high] }.
    `raw` is the chart-grade series (one point ~ every downsample_sec
    seconds); `ai` is the AI-grade aggregated frame. `summary` has
    avg/min/max per metric.
    """
    return await _get(
        f"/api/runs/{activity_id}/telemetry", downsample_sec=downsample_sec
    )


# =============================================================================
# Training cycles + blocks
# =============================================================================

@mcp.tool()
async def list_blocks() -> dict:
    """List all training blocks (cycles) the user has defined, plus a
    `selected` field pointing to today's active block.

    Returns: { blocks: [{id, name, start_date, end_date, primary_event}],
    selected: id }.
    """
    return await _get("/api/training/blocks")


@mcp.tool()
async def get_cycle_stats(
    block_id: str, week_start: str, week_end: str
) -> dict:
    """Aggregate stats for the cycle containing this week, plus the
    week-level summary for [week_start, week_end] (YYYY-MM-DD).

    Returns: { cycle: { total_runs, total_miles, total_hours, avg_pace,
    avg_hr, elevation_ft, longest_run, avg_weekly_miles,
    category_breakdown: [...] }, week: { week_num, runs, miles, hours,
    avg_pace, avg_hr, elevation_ft, vs_avg }, weekly_miles: [...] }.
    avg_weekly_miles is total / weeks-elapsed-by-today, stable across
    week selections.
    """
    return await _get(
        "/api/training/cycle-stats",
        block_id=block_id,
        week_start=week_start,
        week_end=week_end,
    )


@mcp.tool()
async def get_monthly_stats(activity_type: str = "running") -> dict:
    """Monthly aggregates over ALL of the user's history.

    activity_type:
      - "running" → all run-flavored typeKeys lumped (treadmill / track / etc.)
      - "lap_swimming" / "stair_climbing" / "hiking" → exact match
      - "all" → every activity

    Returns: { activity_type, months: [{month: 'YYYY-MM', count, miles,
    hours, elevation_ft, avg_pace_dec (numeric min/mi),
    avg_pace ('M:SS' string), avg_hr}] }. avg_pace_dec for charts;
    avg_pace string is pre-formatted for prompt sentences.
    """
    return await _get("/api/training/monthly-stats", activity_type=activity_type)


# =============================================================================
# Manual activities (user-logged: gym, swim, stretching, etc.)
# =============================================================================

@mcp.tool()
async def list_manual_activities(start: str, end: str) -> dict:
    """List user-logged non-Garmin activities in [start, end] (YYYY-MM-DD).

    Returns: { activities: [{ id, date, type ('run'/'swim'/'gym'/'other'),
    desc, duration_min, distance_mi, start_time ('HH:MM' optional) }] }.
    These are post-hoc logs; planned future activities live in Google
    Calendar (use get_calendar_events).
    """
    return await _get("/api/manual-activities", start=start, end=end)


@mcp.tool()
async def get_manual_activity(activity_id: str) -> dict:
    """Single manual activity by string id (e.g. "manual_1777658163").
    Same shape as list_manual_activities entries."""
    return await _get(f"/api/manual-activities/{activity_id}")


# =============================================================================
# Calendar (Google + manuals + Garmin runs unified)
# =============================================================================

@mcp.tool()
async def get_calendar_events(start: str, end: str) -> dict:
    """Unified calendar feed for [start, end] (ISO datetime — naive is OK,
    treated as the server's local zone with DST applied per date).

    Returns: { google_connected: bool, events: [...] } where each event
    has: source ('google' / 'manual' / 'garmin_run' / 'google_error'),
    id, title, start, end, all_day, optional location/description, and
    `manual_activity` (full ManualActivity dict) for source=manual.

    Use this to check the user's availability — work blocks, PT sessions,
    travel, etc. all flow through 'source: google'.
    """
    return await _get("/api/calendar/events", start=start, end=end)


# =============================================================================
# Health + profile
# =============================================================================

@mcp.tool()
async def get_health_metrics(days: int = 14) -> dict:
    """Recent daily health metrics from Garmin: sleep, RHR, HRV, stress,
    plus daily run miles/minutes.

    Returns: { days, rows: [{date, sleep_score, sleep_hours, rhr, hrv,
    stress, run_miles, run_mins}, ...] }. Rows ascending by date.
    """
    return await _get("/api/health/ledger", days=days)


@mcp.tool()
async def get_user_profile() -> dict:
    """User's baseline profile (HR zones, race goals, history, free-form
    facts). Same JSON the coach already gets pre-injected into its
    system prompt — exposed so the agent can re-read it on demand
    instead of relying on stale prompt context."""
    return await _get("/api/profile")


if __name__ == "__main__":
    # stdio transport. Run via `uv run python -m personal_coach_mcp` or
    # let an MCP client spawn it.
    mcp.run()
