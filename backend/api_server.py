from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
# (FileResponse import removed alongside the legacy GET / route.)
from pydantic import BaseModel, Field

from backend.agentic_coach import AgenticCoach
from backend.cognitive_memory_engine import MemoryOS
from backend.data_processor import DataProcessor
from backend.google_calendar import GoogleCalendar
from backend.langsmith_setup import langsmith_status, startup_log_line
from backend.seed_models import (
    refit_aerobic_decoupling_baseline,
    refit_cadence_baseline,
    refit_cycle_weekly_volume_diff,
    refit_hrv_14d_baseline,
    refit_sleep_debt_14d,
)

# Registry of stat-derived models. Importable so a future nightly
# cron (or debug CLI) can iterate without rebuilding — see PR #88
# review feedback. Keep dict-shaped so the cron's for-loop stays
# trivial. Adding a new model means: write refit_* in seed_models,
# add one entry here, ship docs.
REFIT_REGISTRY: dict[str, Any] = {
    "recovery.hrv_14d_baseline": refit_hrv_14d_baseline,
    "aerobic.decoupling_baseline": refit_aerobic_decoupling_baseline,
    "cadence.baseline": refit_cadence_baseline,
    # PR P6 batch 2
    "sleep.debt_14d": refit_sleep_debt_14d,
    "cycle.weekly_volume_diff": refit_cycle_weekly_volume_diff,
}


app = FastAPI(title="PersonalCoach API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

processor = DataProcessor()
gcal = GoogleCalendar()
memory_engine = MemoryOS(
    db_path="data/cognition.db",
    semantic_profile_path=processor.paths["semantic_memory"],
)
agent = AgenticCoach(memory_engine=memory_engine)


def _default_window() -> tuple[str, str]:
    end = datetime.date.today()
    start = end - datetime.timedelta(days=14)
    return start.isoformat(), end.isoformat()


def _find_run_summary(activity_id: int) -> dict[str, Any] | None:
    path = Path(processor.paths["activities"])
    if not path.exists():
        return None

    for f in path.glob("*.json"):
        try:
            payload = json.loads(f.read_text())
        except Exception:
            continue
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if str(row.get("activityId")) == str(activity_id):
                return row
    return None


def _hrv_status(row: dict[str, Any], window: list[dict[str, Any]]) -> str:
    hrv_values = [r.get("hrv") for r in window if r.get("hrv") is not None]
    if not hrv_values or row.get("hrv") is None:
        return "Unknown"
    baseline = sum(hrv_values) / len(hrv_values)
    variance = sum((x - baseline) ** 2 for x in hrv_values) / max(len(hrv_values), 1)
    std = max(variance**0.5, 3.5)
    if row["hrv"] < baseline - std or row["hrv"] > baseline + std:
        return "Unbalanced"
    return "Balanced"


class LapsUpdate(BaseModel):
    week_num: int
    run_name: str
    categories: list[str] = Field(default_factory=list)
    notes: str = ""


class ChatInput(BaseModel):
    thread_id: str
    message: str
    system_context: str | None = None


class TopicCreate(BaseModel):
    name: str
    root_category: str
    status: str = "Open"
    working_conclusion: str | None = None


class TopicUpdate(BaseModel):
    status: str | None = None
    working_conclusion: str | None = None
    name: str | None = None


class EpisodeCreate(BaseModel):
    event_type: str
    context: dict[str, Any]
    lesson_learned: str | None = None
    related_topic_ids: list[str] = Field(default_factory=list)
    timestamp: str | None = None


class PendingResolve(BaseModel):
    user_answer: str


class ConsolidateInput(BaseModel):
    thread_id: str


class RunAnalysisInput(BaseModel):
    activity_id: int
    block_id: str = "block_001"
    thread_id: str | None = None
    downsample_sec: int = 10


class HealthAnalysisInput(BaseModel):
    thread_id: str = "unified_copilot_thread"


class ManualActivityCreate(BaseModel):
    date: str
    type: Literal["run", "swim", "gym", "other"]
    description: str = ""
    duration_min: float | None = None
    distance_mi: float | None = None
    # "HH:MM" — optional. Drives whether the calendar shows this as a
    # timed block or an all-day chip.
    start_time: str | None = None


class ManualActivityUpdate(BaseModel):
    # All optional — user might rename without touching duration etc.
    # Pydantic's exclude_unset preserves the difference between "not in
    # body" (no change) and "explicit null" (clear the field).
    date: str | None = None
    type: Literal["run", "swim", "gym", "other"] | None = None
    description: str | None = None
    duration_min: float | None = None
    distance_mi: float | None = None
    start_time: str | None = None


class BlockCreate(BaseModel):
    name: str
    start_date: str
    end_date: str
    primary_event: str = "running"


class BlockUpdate(BaseModel):
    name: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    primary_event: str | None = None


# NOTE: previously this module had `GET /` returning
# FileResponse("webapp/index.html"), but `webapp/` doesn't exist —
# that directory was the legacy Streamlit/static entry, retired
# when the frontend moved to Next.js under `web/`. The route was
# permanently broken (500 on every request) and surfaced by Phase
# 2 endpoint smoke. Removed; the Next.js dev server / production
# build serves the user-facing root on its own port.

SYNC_STATE_PATH = Path("data") / "sync_state.json"


def _latest_data_mtime() -> float | None:
    # Most recent file across the per-day Garmin folders. Tells us how fresh the
    # local cache is regardless of which dataset Garmin actually returned.
    root = Path("data")
    if not root.is_dir():
        return None
    latest = 0.0
    for sub in root.iterdir():
        if not sub.is_dir() or not sub.name.startswith("get_"):
            continue
        for f in sub.iterdir():
            if f.is_file() and f.suffix == ".json":
                latest = max(latest, f.stat().st_mtime)
    return latest or None


def _read_sync_state() -> dict[str, Any]:
    if not SYNC_STATE_PATH.is_file():
        return {}
    try:
        return json.loads(SYNC_STATE_PATH.read_text())
    except Exception:
        return {}


def _write_sync_state(outcome: str, detail: str = "") -> None:
    SYNC_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYNC_STATE_PATH.write_text(
        json.dumps(
            {
                "last_attempt": datetime.datetime.now(
                    tz=datetime.timezone.utc
                ).isoformat(),
                "outcome": outcome,
                "detail": detail[:500],
            }
        )
    )


@app.get("/api/sync/garmin/status")
def sync_garmin_status() -> dict[str, Any]:
    mtime = _latest_data_mtime()
    state = _read_sync_state()
    return {
        "last_sync": (
            datetime.datetime.fromtimestamp(mtime, tz=datetime.timezone.utc).isoformat()
            if mtime
            else None
        ),
        "last_attempt": state.get("last_attempt"),
        "outcome": state.get("outcome"),
    }


@app.post("/api/sync/garmin")
def sync_garmin() -> dict[str, Any]:
    cmd = [sys.executable, "-m", "backend.garmin_sync", "--no-fallback"]
    # /dev/null on stdin so any prompt-based fallback can't hang the request.
    result = subprocess.run(
        cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if result.returncode == 2 or "TOKEN_EXPIRED" in (result.stderr or ""):
        _write_sync_state("token_expired", result.stderr or "")
        return {
            "ok": False,
            "reason": "token_expired",
            "stdout": result.stdout[-2000:],
            "stderr": result.stderr[-2000:],
        }
    if result.returncode == 0:
        # The dashboard reads from the per-day ledger, not the raw Garmin
        # JSON dump. Refresh it here so users don't have to call two endpoints.
        # If the rebuild fails the user is left on stale data even though the
        # Garmin pull succeeded — that has to surface as a failure, otherwise
        # the Setup UI shows green and the dashboard quietly serves yesterday.
        try:
            processor.compile_health_ledger(days_back=120)
        except Exception as exc:
            detail = f"Garmin pull OK, but ledger rebuild failed: {exc}"
            _write_sync_state("error", detail)
            return {
                "ok": False,
                "reason": "error",
                "stdout": result.stdout[-2000:],
                "stderr": detail,
            }
        _write_sync_state("ok")
        return {"ok": True, "reason": None, "stdout": result.stdout[-4000:]}
    _write_sync_state("error", (result.stderr or result.stdout)[-500:])
    return {
        "ok": False,
        "reason": "error",
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }


class TicketRefresh(BaseModel):
    ticket: str  # accepts ST-...-sso bare ticket OR full redirect URL


@app.post("/api/sync/garmin/refresh-token")
def refresh_garmin_token(body: TicketRefresh) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "backend.garmin_ticket_login", "--ticket", body.ticket, "--compat"]
    # No browser, no input prompts — we already have the ticket.
    result = subprocess.run(
        cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if result.returncode == 0:
        # Clear the token-expired sticky banner; the user will Sync next.
        _write_sync_state("ok", "token refreshed via UI")
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-2000:],
        "stderr": result.stderr[-2000:],
    }


@app.post("/api/sync/health-ledger")
def sync_health_ledger(days_back: int = 120) -> dict[str, Any]:
    rows = processor.compile_health_ledger(days_back=days_back)
    return {"ok": True, "rows": len(rows)}


@app.get("/api/health/ledger")
def health_ledger(days: int = Query(default=14, ge=1, le=365)) -> dict[str, Any]:
    """Recent daily health metrics — sleep, RHR, HRV, stress, run miles —
    for the last `days` days. Reads the cached ledger CSV; recompute via
    POST /api/sync/health-ledger if it's stale."""
    rows = processor.get_health_stats() or []
    # rows are date-sorted ascending. Tail the requested window.
    return {"days": days, "rows": rows[-days:]}


@app.get("/api/profile")
def user_profile_endpoint() -> dict[str, Any]:
    """User's semantic-memory baseline profile (HR zones, goals, etc.)
    that the coach prompts already inject. Exposed so MCP / AI tools can
    read the same source of truth without duplicating the file path.

    Note: returns the *raw* on-disk profile. For the AI-coach-shaped view
    (unit-converted, RPE-named HR zones, current cycle phase) use
    /api/athlete/profile instead.
    """
    return processor.get_semantic_memory() or {}


@app.get("/api/athlete/profile")
def athlete_profile_full() -> dict[str, Any]:
    """Composite athlete profile for AI coaching: identity (kg/cm),
    fitness (VO2max, LT pace, RPE-named HR zones from
    data/manual_inputs/user_zones.json), current cycle phase,
    preferences, medical notes. See docs/mcp_tools_design.md §1.
    """
    return processor.get_athlete_profile_full()


@app.get("/api/health/readiness")
def health_readiness(date: str | None = Query(default=None)) -> dict[str, Any]:
    """Single-day readiness signal (green/yellow/red) with 7-day baselines
    + deltas + rationale. Replaces the raw 7-day ledger dump for AI
    consumption — see docs/mcp_tools_design.md §2.
    """
    return processor.get_readiness(target_date=date)


@app.get("/api/training/load")
def training_load(window: int = Query(default=28, ge=7, le=120)) -> dict[str, Any]:
    """Acute (7d) / chronic (Nd) training load + ACWR ratio + weekly miles
    trend, in a single payload. Used by the AI coach to assess injury
    risk and fitness trajectory. See docs/mcp_tools_design.md §3.
    """
    return processor.get_training_load(window_days=window)


@app.get("/api/training/blocks")
def training_blocks() -> dict[str, Any]:
    blocks = processor.get_blocks()
    today_iso = datetime.date.today().isoformat()
    active_id = next(
        (b["id"] for b in blocks if b["start_date"] <= today_iso <= b["end_date"]),
        blocks[0]["id"] if blocks else None,
    )
    return {"blocks": blocks, "active_block_id": active_id}


@app.post("/api/training/blocks")
def create_block(body: BlockCreate) -> dict[str, Any]:
    try:
        new_id = processor.create_block(
            name=body.name,
            start_date=body.start_date,
            end_date=body.end_date,
            primary_event=body.primary_event,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": new_id}


@app.put("/api/training/blocks/{block_id}")
def update_block(block_id: str, body: BlockUpdate) -> dict[str, Any]:
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update")
    try:
        ok = processor.update_block(block_id, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, f"Block {block_id} not found")
    return {"ok": True, "id": block_id}


@app.delete("/api/training/blocks/{block_id}")
def delete_block(block_id: str) -> dict[str, Any]:
    ok = processor.delete_block(block_id)
    if not ok:
        raise HTTPException(404, f"Block {block_id} not found")
    return {"ok": True, "id": block_id}


@app.get("/api/training/weeks")
def training_weeks(block_id: str | None = None) -> dict[str, Any]:
    blocks = processor.get_blocks()
    if not blocks:
        raise HTTPException(404, "No training blocks found")
    selected = block_id or blocks[0]["id"]
    weeks = processor.get_weeks_for_block(selected)
    return {"block_id": selected, "weeks": weeks}


@app.get("/api/training/cycle-stats")
def training_cycle_stats(
    block_id: str = Query(...),
    week_start: str = Query(...),
    week_end: str = Query(...),
) -> dict[str, Any]:
    stats = processor.compute_cycle_and_week_stats(block_id, week_start, week_end)
    if stats is None:
        raise HTTPException(404, f"Block {block_id} not found")
    return stats


@app.get("/api/training/monthly-stats")
def training_monthly_stats(
    activity_type: str = Query(default="all"),
) -> dict[str, Any]:
    months = processor.get_monthly_activity_stats(activity_type=activity_type)
    return {"activity_type": activity_type, "months": months}


# ==========================================
# Google Calendar OAuth + unified calendar events
# ==========================================
# OAuth flow: frontend hits /oauth/google/start which 302s to Google's
# consent screen; Google calls /oauth/google/callback with the code and
# state we issued; we exchange + persist creds, then bounce the user
# back to /training with a ?connected=1 query for the UI to pick up.

from fastapi.responses import RedirectResponse


@app.get("/api/oauth/google/status")
def oauth_google_status() -> dict[str, Any]:
    return {"connected": gcal.is_connected()}


@app.get("/oauth/google/start")
def oauth_google_start() -> RedirectResponse:
    url, _state = gcal.authorization_url()
    # google_auth_oauthlib threads state through the URL itself, so we
    # don't need to round-trip it via cookies/session. The library
    # validates it on callback.
    return RedirectResponse(url)


@app.get("/oauth/google/callback")
def oauth_google_callback(
    state: str = Query(...),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"http://localhost:3000/training?gcal_error={error}")
    # Reconstruct the full URL fastapi saw — fetch_token wants the same
    # query string Google bounced back with.
    auth_response = (
        f"{os.environ.get('GOOGLE_OAUTH_REDIRECT_URI', 'http://localhost:8765/oauth/google/callback')}"
        f"?state={state}&code={code}"
    )
    try:
        gcal.finish_flow(auth_response, state)
    except Exception as e:
        return RedirectResponse(f"http://localhost:3000/training?gcal_error={e}")
    return RedirectResponse("http://localhost:3000/training?gcal_connected=1")


@app.post("/api/oauth/google/disconnect")
def oauth_google_disconnect() -> dict[str, bool]:
    gcal.disconnect()
    return {"ok": True}


@app.get("/api/calendar/events")
def calendar_events(
    start: str = Query(..., description="ISO 8601 start datetime"),
    end: str = Query(..., description="ISO 8601 end datetime"),
) -> dict[str, Any]:
    """Unified calendar events: Google Calendar + ManualActivity (with
    start_time) + Garmin runs, all in [start, end].

    Designed to feed both the UI calendar and the AI's availability tool.
    Each event has a `source` discriminator so the AI can reason about
    "this is a real-life commitment vs a planned workout".
    """
    try:
        start_dt = datetime.datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"Bad datetime: {e}")

    # Google Calendar API rejects naive timeMin/timeMax with HTTP 400.
    # FullCalendar sends timezone-aware ISO strings, but ad-hoc callers
    # (curl smoke tests, future AI tools) often pass naive ISO. Treat
    # those as local wall-clock time. naive_dt.astimezone() interprets
    # the value as local and resolves the offset against the system's
    # TZ rules *for that specific date*, so a January window queried in
    # June still gets the EST offset rather than EDT (which would shift
    # the window an hour off and clip events at the edges).
    if start_dt.tzinfo is None:
        start_dt = start_dt.astimezone()
    if end_dt.tzinfo is None:
        end_dt = end_dt.astimezone()

    events: list[dict[str, Any]] = []

    # ---- Google Calendar (life events: work blocks, PT, sauna, etc.) ----
    google_connected = gcal.is_connected()
    if google_connected:
        try:
            from backend.google_calendar import PLANNED_WORKOUT_MARKER
            google_events = gcal.list_events(start_dt, end_dt)
            # Re-classify AI-authored planned workouts so the UI can
            # dye them distinctly from generic life events. We detect
            # them by the marker line we embed in the description
            # during _plan_to_cal_payload. /api/planned-workouts
            # currently returns JSON-only rows (the Cal-side merge
            # was punted to a future PR per its own docstring); if
            # that merge lands, it should reuse this exact heuristic
            # so the two surfaces stay consistent.
            for ev in google_events:
                desc = ev.get("description") or ""
                if PLANNED_WORKOUT_MARKER in desc:
                    ev["source"] = "planned_workout"
            events.extend(google_events)
        except Exception as e:
            # Don't fail the whole request — surface the error so the UI
            # can show "Google calendar disconnected" without losing the
            # other event sources.
            events.append({
                "source": "google_error",
                "id": "google_error",
                "title": f"Google Calendar error: {e}",
                "start": start,
                "end": end,
                "all_day": True,
            })

    # ---- ManualActivity (logged "I did 20min stretching" entries) ----
    start_date = start_dt.date().isoformat()
    end_date = end_dt.date().isoformat()
    for a in processor.get_manual_activities_in_range(start_date, end_date):
        ev_start, ev_end, all_day = _manual_activity_window(a)
        events.append({
            "source": "manual",
            "id": f"manual:{a['id']}",
            "title": _manual_activity_title(a),
            "start": ev_start,
            "end": ev_end,
            "all_day": all_day,
            "description": a.get("desc"),
            "manual_activity": a,
        })

    # ---- Garmin runs (auto-synced runs become calendar events) ----
    for r in processor.get_activities_in_range(start_date, end_date):
        if "running" not in (r.get("activityType") or {}).get("typeKey", ""):
            continue
        # startTimeLocal is "YYYY-MM-DD HH:MM:SS" in the user's local
        # zone, no offset suffix. Treat it as wall-clock time matching
        # the request's local view.
        local_start = r.get("startTimeLocal")
        if not local_start:
            continue
        try:
            run_start_dt = datetime.datetime.fromisoformat(local_start)
        except Exception:
            continue
        dur_s = r.get("movingDuration") or r.get("duration") or 0
        run_end_dt = run_start_dt + datetime.timedelta(seconds=dur_s)
        meta = r.get("manual_meta") or {}
        title = meta.get("name") or r.get("activityName") or "Run"
        events.append({
            "source": "garmin_run",
            "id": f"run:{r.get('activityId')}",
            "title": title,
            "start": run_start_dt.isoformat(),
            "end": run_end_dt.isoformat(),
            "all_day": False,
            "activity_id": r.get("activityId"),
        })

    return {
        "start": start,
        "end": end,
        "google_connected": google_connected,
        "events": events,
    }


def _manual_activity_title(a: dict[str, Any]) -> str:
    """Build a short title for a manual activity event. Type as the lead
    word (Run / Swim / Gym / Other), description in parens when present.
    Keeps the calendar grid scannable on a phone."""
    type_label = (a.get("type") or "Other").replace("_", " ").title()
    desc = (a.get("desc") or "").strip()
    if desc:
        return f"{type_label} — {desc}" if len(desc) <= 24 else f"{type_label}"
    return type_label


def _manual_activity_window(a: dict[str, Any]) -> tuple[str, str, bool]:
    """Resolve a ManualActivity's calendar window.

    With start_time + duration_min: a real timed event window.
    Without start_time: an all-day event on `date`.
    """
    date_str = a.get("date")
    start_time = a.get("start_time")  # "HH:MM" (optional)
    duration_min = a.get("duration_min")
    if not start_time:
        # All-day. FullCalendar wants the date as start, end as next day.
        next_day = (
            datetime.date.fromisoformat(date_str) + datetime.timedelta(days=1)
        ).isoformat()
        return date_str, next_day, True
    # Timed event. Default to 30 min when duration unknown so the block
    # renders visibly.
    start_iso = f"{date_str}T{start_time}:00"
    start_dt = datetime.datetime.fromisoformat(start_iso)
    end_dt = start_dt + datetime.timedelta(minutes=duration_min or 30)
    return start_dt.isoformat(), end_dt.isoformat(), False


@app.get("/api/manual-activities")
def list_manual_activities(
    start: str = Query(...),
    end: str = Query(...),
) -> dict[str, Any]:
    return {"start": start, "end": end, "activities": processor.get_manual_activities_in_range(start, end)}


@app.get("/api/manual-activities/{activity_id}")
def get_manual_activity(activity_id: str) -> dict[str, Any]:
    rows = processor.load_json_safe(processor.paths["aux"])
    if isinstance(rows, dict):
        rows = []
    for r in rows:
        if r.get("id") == activity_id:
            return r
    raise HTTPException(404, f"Manual activity {activity_id} not found")


@app.post("/api/manual-activities")
def create_manual_activity(body: ManualActivityCreate) -> dict[str, Any]:
    entry = processor.add_manual_activity(
        date_str=body.date,
        activity_type=body.type,
        description=body.description,
        duration_min=body.duration_min,
        distance_mi=body.distance_mi,
        start_time=body.start_time,
    )
    return {"ok": True, "activity": entry}


@app.put("/api/manual-activities/{activity_id}")
def update_manual_activity_endpoint(
    activity_id: str, body: ManualActivityUpdate
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    try:
        entry = processor.update_manual_activity(activity_id, **fields)
    except ValueError as e:
        # Trying to null out a required field (date/type/desc) — caught here
        # rather than letting a malformed record land on disk.
        raise HTTPException(400, str(e))
    if entry is None:
        raise HTTPException(404, f"Manual activity {activity_id} not found")
    return {"ok": True, "activity": entry}


@app.delete("/api/manual-activities/{activity_id}")
def delete_manual_activity_endpoint(activity_id: str) -> dict[str, Any]:
    ok = processor.delete_manual_activity(activity_id)
    if not ok:
        raise HTTPException(404, f"Manual activity {activity_id} not found")
    return {"ok": True, "id": activity_id}


# ===========================================================================
# Daily check-ins (PR P3 — perceived layer §2)
# ===========================================================================
#
# One row per calendar date. The 4 ordinal fields (sleep_quality /
# soreness / mood / motivation, each 0-5) plus optional notes give the
# agent its first window into "how does the user feel today?" — until
# now everything was Garmin sensors (objective) or per-lap RPE
# (perceived but post-hoc + workout-bound). Each upsert also writes a
# `daily_checkin` episode into CME so search_episodes / consolidate
# pick up the signal.


class DailyCheckinInput(BaseModel):
    """Body for POST /api/checkins. All scale fields optional — the
    UI may submit only what changed since the user's last save.
    Validation (0-5 range, int type) happens in DataProcessor."""
    date: str  # YYYY-MM-DD; treated as the row key (upsert semantics)
    sleep_quality: int | None = None
    soreness: int | None = None
    mood: int | None = None
    motivation: int | None = None
    notes: str | None = None


@app.get("/api/checkins")
def list_checkins(
    days: int = Query(14, ge=1, le=365),
) -> dict[str, Any]:
    """Return last `days` calendar days of check-ins, newest first.
    Used by both the Health-tab card (today only) and the agent (via
    MCP get_recent_checkins)."""
    today = datetime.date.today()
    start = (today - datetime.timedelta(days=days - 1)).isoformat()
    end = today.isoformat()
    return {
        "days": days,
        "start": start,
        "end": end,
        "checkins": processor.list_checkins_in_range(start, end),
    }


@app.get("/api/checkins/{date_str}")
def get_checkin(date_str: str) -> dict[str, Any]:
    row = processor.get_checkin_by_date(date_str)
    if not row:
        raise HTTPException(404, f"No check-in for {date_str}")
    return row


@app.post("/api/checkins")
def upsert_checkin(body: DailyCheckinInput) -> dict[str, Any]:
    """Create or update a check-in by date. Same-day re-submit
    overrides (no version history). Also dual-writes a 'daily_checkin'
    episode into CME so the agent can search/cluster perceived state
    over time."""
    payload = body.model_dump(exclude={"date"}, exclude_none=True)
    try:
        entry = processor.upsert_checkin(body.date, **payload)
    except ValueError as e:
        raise HTTPException(400, str(e))

    # CME dual-write: capture this check-in as an episode so it lands
    # in search_episodes results + consolidate_memory_background can
    # cluster perceived-state patterns into models down the line.
    # Best-effort: a CME write failure must not 500 the check-in
    # save (the canonical store is the JSON file).
    try:
        memory_engine.create_episode(
            event_type="daily_checkin",
            context={
                "what": "daily check-in",
                "when": body.date,
                "sleep_quality": entry.get("sleep_quality"),
                "soreness": entry.get("soreness"),
                "mood": entry.get("mood"),
                "motivation": entry.get("motivation"),
                "notes": entry.get("notes"),
            },
            lesson_learned=entry.get("notes") or None,
            timestamp=entry.get("updated_at"),
        )
    except Exception:
        # Tracing this would be nice (PR B) but tracer lives on the
        # AgenticCoach, not memory_engine itself. Silent here matches
        # the "tracing never breaks a turn" contract.
        pass

    return {"ok": True, "checkin": entry}


@app.delete("/api/checkins/{date_str}")
def delete_checkin(date_str: str) -> dict[str, Any]:
    """Drop the check-in row. Does NOT delete the corresponding CME
    episode — that's evidence the agent already used; deleting it
    after the fact would falsify history. The check-in row goes,
    the episode stays."""
    ok = processor.delete_checkin(date_str)
    if not ok:
        raise HTTPException(404, f"No check-in for {date_str}")
    return {"ok": True, "date": date_str}


# ===========================================================================
# Planned workouts (PR P4a — intent layer §3)
# ===========================================================================
#
# Closes the "AI proposes a plan → it lands on user's phone" loop.
# Storage is `data/manual_inputs/planned_workouts.json`; Google Cal
# is the user-facing surface (reminders, mobile, etc.). The two are
# kept in sync by dual-write on POST/PUT/DELETE.
#
# Cal failures (not connected, network blip, scope refused) degrade
# gracefully — the JSON row is the canonical store, the Cal event is
# the nice-to-have surface. Caller sees `cal_synced: false` on the
# response when the Cal write didn't happen so the UI can hint to
# reconnect.


class PlannedWorkoutInput(BaseModel):
    """Body for POST /api/planned-workouts. `date` + `type` required
    on create. All targets optional (a casual "easy 5 mi" plan
    doesn't need explicit pace/HR; a tempo plan probably does)."""
    date: str  # YYYY-MM-DD
    type: str
    target_pace_min_mi: float | None = None
    target_hr: int | None = None
    distance_mi: float | None = None
    duration_min: int | None = None
    notes: str | None = None


class PlannedWorkoutPatch(BaseModel):
    """PUT body — every field optional. None clears optional fields
    (target_pace_min_mi / target_hr / etc.) but not required ones
    (date / type) — DataProcessor raises ValueError if a required
    field is set to None, which the handler turns into 400."""
    date: str | None = None
    type: str | None = None
    target_pace_min_mi: float | None = None
    target_hr: int | None = None
    distance_mi: float | None = None
    duration_min: int | None = None
    notes: str | None = None


def _plan_to_cal_payload(plan: dict) -> dict[str, Any]:
    """Turn a planned-workout row into the Google Cal event payload
    we want to write. Title = workout type (capitalized);
    description embeds the marker line + structured fields so the
    read merge in /api/planned-workouts can recognize it. Time:
    default to a 1-hour block at 09:00 local — gives the event a
    concrete slot the user can drag around in Google Cal.

    Notifications are silenced (`reminders.useDefault=False` + empty
    overrides). Reasoning: AI-proposed workouts shouldn't fire native
    phone alarms on top of whatever flow the user has — the user
    already engaged via chat to schedule this. The create path
    spreads this whole dict into insert_event so the silence lands;
    the update path explicitly cherry-picks {summary, start, end,
    description} and intentionally drops `reminders`, so if the user
    re-enables reminders on Google's side our subsequent edits won't
    stomp on that choice."""
    title = plan["type"].capitalize() + " workout"
    desc_lines = [
        "personalcoach.training=true",
        f"type: {plan['type']}",
    ]
    for field in ("target_pace_min_mi", "target_hr", "distance_mi", "duration_min"):
        v = plan.get(field)
        if v is not None:
            desc_lines.append(f"{field}: {v}")
    if plan.get("notes"):
        desc_lines.append("")
        desc_lines.append(plan["notes"])
    # Default 09:00–10:00 unless duration_min suggests something else.
    # Google Cal's `dateTime` field requires either a UTC offset embedded
    # in the string OR an explicit `start.timeZone`/`end.timeZone`. A
    # naked `"2026-05-30T09:00:00"` is rejected with HTTP 400, which
    # would silently flip cal_synced=false. Build a tz-aware local
    # datetime so .isoformat() emits the offset (e.g.
    # "2026-05-30T09:00:00-04:00").
    duration = int(plan.get("duration_min") or 60)
    date_obj = datetime.datetime.strptime(plan["date"], "%Y-%m-%d").date()
    start_local = datetime.datetime.combine(
        date_obj, datetime.time(9, 0)
    ).astimezone()
    end_local = start_local + datetime.timedelta(minutes=duration)
    return {
        "summary": title,
        "start": start_local.isoformat(),
        "end": end_local.isoformat(),
        "description": "\n".join(desc_lines),
        "reminders": {"useDefault": False, "overrides": []},
    }


@app.get("/api/planned-workouts")
def list_planned_workouts(
    start: str = Query(...),
    end: str = Query(...),
) -> dict[str, Any]:
    """Inclusive date-range planned workouts. JSON-stored rows only
    for now — Cal-sourced events (those tagged
    `personalcoach.training=true` but created outside our app) are
    surfaced separately via /api/calendar/events. P4b will merge
    the two views; this PR keeps the read side simple."""
    return {
        "start": start,
        "end": end,
        "planned_workouts": processor.list_planned_workouts_in_range(start, end),
    }


@app.get("/api/planned-workouts/{plan_id}")
def get_planned_workout(plan_id: str) -> dict[str, Any]:
    row = processor.get_planned_workout(plan_id)
    if not row:
        raise HTTPException(404, f"No planned workout {plan_id}")
    return row


@app.post("/api/planned-workouts")
def create_planned_workout(body: PlannedWorkoutInput) -> dict[str, Any]:
    """Create a planned workout. Dual-writes to Google Cal if
    connected; the returned event_id lands on the JSON row as
    `cal_event_id` so edits/deletes sync. Cal failure degrades to
    JSON-only — response carries `cal_synced` so the caller can
    decide whether to surface a "connect Google Cal" hint."""
    payload = body.model_dump(exclude_none=True)
    try:
        plan = processor.upsert_planned_workout(plan_id=None, **payload)
    except ValueError as e:
        raise HTTPException(400, str(e))

    cal_synced = False
    if gcal.is_connected():
        try:
            ev = gcal.insert_event(**_plan_to_cal_payload(plan))
            plan = processor.upsert_planned_workout(
                plan["id"], cal_event_id=ev["id"]
            )
            cal_synced = True
        except Exception:
            # Best-effort: JSON row is canonical. Frontend can show
            # a degraded badge from the cal_synced flag.
            pass

    return {"ok": True, "cal_synced": cal_synced, "planned_workout": plan}


@app.put("/api/planned-workouts/{plan_id}")
def update_planned_workout(
    plan_id: str, body: PlannedWorkoutPatch
) -> dict[str, Any]:
    """Patch fields. If the row carries a `cal_event_id`, mirror the
    edit back to Google Cal. Cal failures degrade to JSON-only."""
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(400, "No fields to update")
    try:
        plan = processor.upsert_planned_workout(plan_id, **fields)
    except KeyError:
        raise HTTPException(404, f"No planned workout {plan_id}")
    except ValueError as e:
        raise HTTPException(400, str(e))

    cal_synced = False
    if plan.get("cal_event_id") and gcal.is_connected():
        try:
            cal_payload = _plan_to_cal_payload(plan)
            gcal.update_event(
                plan["cal_event_id"],
                summary=cal_payload["summary"],
                start=cal_payload["start"],
                end=cal_payload["end"],
                description=cal_payload["description"],
            )
            cal_synced = True
        except Exception:
            pass

    return {"ok": True, "cal_synced": cal_synced, "planned_workout": plan}


def _parse_updated_at(ts: str | None) -> datetime.datetime:
    """Parse a stored `updated_at` / `created_at` ISO string to a
    tz-aware datetime for sound comparison. Accepts `+00:00` suffix
    (what `upsert_planned_workout` writes today), `Z` suffix (Google
    Cal format), and naive strings (legacy / hand-edited data). Falls
    back to `datetime.min` (UTC) on parse failure so a malformed row
    sorts to the bottom of `max()` rather than crashing the request."""
    if not ts:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    try:
        # fromisoformat doesn't accept "Z" until Python 3.11+; cover
        # the case explicitly so we don't depend on interpreter version.
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    if dt.tzinfo is None:
        # Legacy naive timestamps: assume UTC (that's what
        # upsert_planned_workout has always written; only manually
        # edited rows might be naive).
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def _compute_plan_deviation_for_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Plan-vs-actual deviation for a given run summary (P4b).

    Strategy: match the planned workout by date. If multiple plans
    exist on the same date (e.g., AM/PM doubles, or a re-issued plan),
    the most-recently-updated one wins. Fancier link-via-cal_event_id
    matching could come later — date matching covers the dominant case
    (one workout per day) and degrades gracefully on edge cases.

    Returns:
      {
        matched: bool,
        planned: <plan row> | None,
        actual:  <normalized run summary> | None,
        deltas:  {pace_min_mi, hr, distance_mi, duration_min} | None,
      }
    `actual` is populated whenever the run summary is valid (even when
    no plan exists for that date) so the agent can still describe what
    the user did.  `deltas` keys are present only for fields the plan
    specified — if a plan didn't pin `target_hr`, deltas won't have an
    `hr` key. `actual - planned` is the convention (positive pace =
    slower than planned, positive HR = harder, positive duration =
    went longer)."""
    run_date = (summary.get("startTimeLocal") or "")[:10]

    distance_m = summary.get("distance") or 0
    distance_mi = distance_m / 1609.34 if distance_m else 0.0
    duration_s = summary.get("movingDuration") or summary.get("duration") or 0
    duration_min = duration_s / 60 if duration_s else 0.0
    pace_min_mi: float | None = None
    if distance_mi > 0 and duration_s > 0:
        pace_min_mi = (duration_s / 60) / distance_mi

    actual = {
        "date": run_date,
        "distance_mi": round(distance_mi, 2),
        "duration_min": round(duration_min, 1),
        "pace_min_mi": round(pace_min_mi, 2) if pace_min_mi is not None else None,
        "avg_hr": summary.get("averageHR"),
    }

    if not run_date:
        return {"matched": False, "planned": None, "actual": actual, "deltas": None}

    plans = processor.list_planned_workouts_in_range(run_date, run_date)
    if not plans:
        return {"matched": False, "planned": None, "actual": actual, "deltas": None}

    # Multiple plans same day (AM/PM doubles, re-issued plan). Pick
    # the most-recently-touched. Two reasons to parse rather than
    # lexicographic-compare the raw strings:
    #   1. Mixed tz formats (`+00:00`, `Z`, naive) compare wrong as
    #      strings — `+` (0x2B) < `Z` (0x5A) < digits, so a naive
    #      stamp would beat a `+00:00` stamp for the same instant.
    #   2. Ties on identical timestamps fall through to a stable
    #      secondary key (`id`) so the choice doesn't depend on
    #      whatever order list_planned_workouts_in_range happened to
    #      return.
    plan = max(
        plans,
        key=lambda p: (
            _parse_updated_at(p.get("updated_at") or p.get("created_at")),
            p.get("id") or "",
        ),
    )

    deltas: dict[str, Any] = {}
    if plan.get("target_pace_min_mi") is not None and actual["pace_min_mi"] is not None:
        deltas["pace_min_mi"] = round(
            actual["pace_min_mi"] - plan["target_pace_min_mi"], 2
        )
    if plan.get("target_hr") is not None and actual["avg_hr"] is not None:
        # round() — not int(). int() truncates toward zero, biasing
        # the delta toward zero in magnitude: actual 158.4 vs target
        # 160 would report -1 instead of -2. round() returns int when
        # called without ndigits so the wire shape stays integer.
        deltas["hr"] = round(actual["avg_hr"] - plan["target_hr"])
    if plan.get("distance_mi") is not None:
        deltas["distance_mi"] = round(actual["distance_mi"] - plan["distance_mi"], 2)
    if plan.get("duration_min") is not None:
        deltas["duration_min"] = round(actual["duration_min"] - plan["duration_min"], 1)

    return {"matched": True, "planned": plan, "actual": actual, "deltas": deltas}


@app.get("/api/runs/{activity_id}/plan-deviation")
def run_plan_deviation(activity_id: int) -> dict[str, Any]:
    """Plan-vs-actual deviation for a run. Agent calls this on
    post-run review to power the "did you do what we said?" coaching
    turn. Returns 404 if the run doesn't exist; matched=False (with
    actual still populated) if no plan was on that date."""
    summary = _find_run_summary(activity_id)
    if not summary:
        raise HTTPException(404, f"Run {activity_id} not found")
    return _compute_plan_deviation_for_summary(summary)


@app.delete("/api/planned-workouts/{plan_id}")
def delete_planned_workout(plan_id: str) -> dict[str, Any]:
    """Hard-delete the planned workout. If it has a `cal_event_id`,
    delete the Cal event too — leaving an orphan event on the user's
    calendar (when they explicitly cleared it from our side) would
    be confusing."""
    plan = processor.get_planned_workout(plan_id)
    if not plan:
        raise HTTPException(404, f"No planned workout {plan_id}")

    cal_id = plan.get("cal_event_id")
    cal_synced = False
    if cal_id and gcal.is_connected():
        try:
            gcal.delete_event(cal_id)
            cal_synced = True
        except Exception:
            pass

    processor.delete_planned_workout(plan_id)
    return {"ok": True, "cal_synced": cal_synced, "id": plan_id}


@app.get("/api/runs")
def runs(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict[str, Any]:
    if not start or not end:
        start, end = _default_window()
    rows = processor.get_activities_in_range(start, end)
    # The endpoint is /api/runs so filter to running here — keeps the
    # frontend out of Garmin's typeKey schema.
    rows = [
        r for r in rows
        if "running" in ((r.get("activityType") or {}).get("typeKey") or "")
    ]
    return {"start": start, "end": end, "runs": rows}


@app.get("/api/runs/{activity_id}")
def run_detail(activity_id: int) -> dict[str, Any]:
    summary = _find_run_summary(activity_id)
    if not summary:
        raise HTTPException(404, "Run not found")

    meta_path = Path(processor.paths["manual"]) / f"run_{activity_id}_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}

    laps = processor.get_run_laps(activity_id)
    return {
        "run": {**summary, "manual_meta": meta},
        "laps": laps,
        "chat_history": processor.get_run_chat_history(activity_id),
    }


@app.get("/api/runs/{activity_id}/telemetry")
def run_telemetry(activity_id: int, downsample_sec: int = 10) -> dict[str, Any]:
    laps = processor.get_run_laps(activity_id)
    raw, ai = processor.get_activity_telemetry(activity_id, laps=laps, downsample_sec=downsample_sec)
    if raw is None:
        raise HTTPException(404, "Telemetry not found")
    return {
        "raw": raw.to_dict(orient="records"),
        "ai": ai.to_dict(orient="records") if ai is not None else [],
        "summary": processor.compute_telemetry_summary(raw),
        "pace_clip": list(processor.PACE_CLIP_MIN_PER_MI),
    }


@app.get("/api/runs/{activity_id}/weather")
def run_weather(activity_id: int) -> dict[str, Any]:
    w = processor.get_run_weather(activity_id)
    if w is None:
        raise HTTPException(
            404,
            "Weather unavailable (run has no GPS, missing details, or fetch failed)",
        )
    return w


@app.get("/api/runs/{activity_id}/route-profile")
def run_route_profile(activity_id: int) -> dict[str, Any]:
    """Grade distribution + terrain summary (P5 — external context §4).

    Powers the agent's "this run was 60% rolling-up terrain — your HR
    drift makes sense" line. Distinct from /route (which returns raw
    GPS points for the map) — this one collapses to a 5-band
    distribution + scalar climb/loss/grade-range, AI-friendly shape.
    """
    profile = processor.compute_route_profile(activity_id)
    if profile is None:
        raise HTTPException(
            404, "Route profile unavailable (treadmill / no GPS / not synced)"
        )
    return profile


@app.get("/api/runs/{activity_id}/route")
def run_route(
    activity_id: int,
    # Need ≥2 to render a line; cap at 5000 to keep payloads sane on phone.
    # FastAPI returns 422 outside the range, so bad input doesn't 500 the API.
    max_points: int = Query(default=500, ge=2, le=5000),
) -> dict[str, Any]:
    route = processor.get_run_route(activity_id, max_points=max_points)
    if route is None:
        raise HTTPException(
            404, "Route unavailable (treadmill / no GPS recorded)"
        )
    return route


@app.get("/api/runs/{activity_id}/laps")
def get_laps(activity_id: int) -> dict[str, Any]:
    laps = processor.get_run_laps(activity_id)
    meta_path = Path(processor.paths["manual"]) / f"run_{activity_id}_meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    saved = meta.get("lap_categories", [])

    for i, lap in enumerate(laps):
        lap["category"] = saved[i] if i < len(saved) else "Hold Back Easy"

    return {"activity_id": activity_id, "laps": laps, "meta": meta}


@app.put("/api/runs/{activity_id}/laps")
def update_laps(activity_id: int, body: LapsUpdate) -> dict[str, Any]:
    laps = processor.get_run_laps(activity_id)
    if not laps:
        raise HTTPException(404, "No lap data found")

    if len(body.categories) != len(laps):
        raise HTTPException(400, "categories length must equal lap count")

    for idx, lap in enumerate(laps):
        lap["category"] = body.categories[idx]

    stats = processor.calculate_category_stats(laps)
    processor.save_run_metadata(
        activity_id=activity_id,
        week_num=body.week_num,
        run_name=body.run_name,
        category_stats=stats,
        notes=body.notes,
        lap_categories=body.categories,
    )
    return {"ok": True, "activity_id": activity_id, "category_stats": stats}


@app.get("/api/health/today")
def health_today() -> dict[str, Any]:
    rows = processor.get_health_stats()
    if not rows:
        raise HTTPException(404, "No health data")
    today = rows[-1]
    window = rows[-21:]
    status = _hrv_status(today, window)
    return {"today": today, "hrv_status": status}


@app.get("/api/health/timeline")
def health_timeline(days: int = 30) -> dict[str, Any]:
    rows = processor.get_health_stats()
    if not rows:
        raise HTTPException(404, "No health data")
    clipped = rows[-days:]
    return {"days": days, "timeline": clipped}


@app.get("/api/health/sleep")
def health_sleep() -> dict[str, Any]:
    detail = processor.get_last_night_sleep()
    if not detail:
        raise HTTPException(404, "No recent sleep data")
    return detail


@app.get("/api/health/snapshot")
def health_snapshot(baseline_days: int = 14) -> dict[str, Any]:
    snap = processor.get_health_snapshot(baseline_days=baseline_days)
    if not snap:
        raise HTTPException(404, "No health data")
    return snap


@app.post("/api/ai/run-analysis")
def ai_run_analysis(body: RunAnalysisInput) -> dict[str, Any]:
    ctx = processor.build_ai_context(body.activity_id, body.block_id)
    if not ctx:
        raise HTTPException(404, "Run metadata context not found")

    laps = processor.get_run_laps(body.activity_id)
    _, df_ai = processor.get_activity_telemetry(body.activity_id, laps=laps, downsample_sec=body.downsample_sec)

    thread_id = body.thread_id or f"run_analysis_{body.activity_id}"
    report = agent.analyze_run(ctx, thread_id=thread_id, telemetry_df=df_ai)
    processor.save_run_chat_message(body.activity_id, "assistant", report)
    return {"thread_id": thread_id, "report": report}


@app.post("/api/ai/health-analysis")
def ai_health_analysis(body: HealthAnalysisInput) -> dict[str, Any]:
    rows = processor.get_health_stats()
    if not rows:
        raise HTTPException(404, "No health data")

    import pandas as pd

    df = pd.DataFrame(rows)
    if df.empty:
        raise HTTPException(404, "No health data")

    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    raw_sleep = processor.load_json_safe(processor.paths["sleep"], f"{yesterday}.json")

    report = agent.analyze_health(history_df=df.tail(14), yesterday_raw=raw_sleep, thread_id=body.thread_id)
    return {"thread_id": body.thread_id, "report": report}


@app.post("/api/ai/chat")
def ai_chat(body: ChatInput) -> dict[str, Any]:
    answer = agent.chat(
        user_input=body.message,
        thread_id=body.thread_id,
        system_context=body.system_context,
    )
    return {"thread_id": body.thread_id, "answer": answer}


@app.post("/api/ai/chat/stream")
async def ai_chat_stream(body: ChatInput):
    """SSE variant of /api/ai/chat. Yields LLM tokens as they arrive
    so the Coach UI can show "live typing" instead of blocking ~10s
    on tool-using turns.

    Frame format follows the EventSource spec:
        event: <type>\\n
        data: <json>\\n
        \\n

    Event types (see AgenticCoach.chat_stream for the source):
      token      — {"type":"token", "content":"..."}
      tool_call  — {"type":"tool_call", "name":"..."}
      done       — {"type":"done"}
      error      — {"type":"error", "message":"..."}

    On `done`, the frontend should invalidate /api/ai/history to pick
    up the canonical message rows (with per-message ts from PR #71).
    The full state — including tool calls and the final AI message —
    has been committed to the checkpointer by then.

    Note: cancellation mid-stream is NOT yet wired up. If the client
    disconnects, the underlying agent run continues to completion in
    the background. Add an abort path here when we need it (separate
    PR).
    """
    async def event_source():
        async for ev in agent.chat_stream(
            user_input=body.message,
            thread_id=body.thread_id,
            system_context=body.system_context,
        ):
            payload = json.dumps(ev, ensure_ascii=False)
            yield f"event: {ev['type']}\ndata: {payload}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            # Prevent proxies from buffering — kills the streaming UX.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ==========================================================================
# Default actions — Phase 2 prebuilt flows. Each pre-fetches a tuned set
# of MCP tools in parallel and feeds the JSON to the LLM as system
# context (option A from the design discussion). Free chat continues to
# go through /api/ai/chat with no pre-fetch.
# ==========================================================================


class ActionInput(BaseModel):
    # thread_id is REQUIRED for all session-bound actions (the 4
    # utility actions append to the active session; archive closes the
    # active session). The frontend tracks current_session_id in
    # localStorage and passes it on every request.
    thread_id: str
    message: str | None = None  # optional freeform extra question
    # Action-specific args (only `review_workout` uses these today):
    activity_id: int | None = None
    run_date: str | None = None


def _coach_session_thread_id() -> str:
    """Generate a fresh coach-session thread_id. Frontend normally
    owns this, but the api can mint one as a fallback."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"coach_{ts}"


@app.post("/api/ai/action/{name}")
def ai_action(name: str, body: ActionInput) -> dict[str, Any]:
    """Run one of the 5 default actions inside the given session.

    Each utility action (review_workout / make_plan / review_health /
    follow_up_memory) pre-fetches the relevant MCP tools in parallel
    and APPENDS its result to the existing thread_id so multiple
    actions stack into a coherent session.

    summarize_and_archive closes the session — the frontend should
    rotate to a new session_id on the next user message.
    """
    try:
        tid = body.thread_id

        if name == "review_workout":
            if body.activity_id is None:
                raise HTTPException(400, "review_workout requires activity_id")
            answer = agent.review_workout(
                activity_id=body.activity_id,
                thread_id=tid,
                run_date=body.run_date,
                user_message=body.message,
            )
            return {"thread_id": tid, "answer": answer}

        if name == "make_plan":
            answer = agent.make_plan(thread_id=tid, user_message=body.message)
            return {"thread_id": tid, "answer": answer}

        if name == "review_health":
            answer = agent.review_health(thread_id=tid, user_message=body.message)
            return {"thread_id": tid, "answer": answer}

        if name == "follow_up_memory":
            answer = agent.follow_up_memory(thread_id=tid, user_message=body.message)
            return {"thread_id": tid, "answer": answer}

        if name == "summarize_and_archive":
            return agent.summarize_and_archive(thread_id=tid)

        raise HTTPException(404, f"Unknown action: {name}")
    except HTTPException:
        raise
    except Exception as e:
        # Surface failures with stack-trace context — these flows can
        # fail in the LLM call, the MCP subprocess, or the prefetch.
        import traceback

        return {
            "error": str(e),
            "traceback": traceback.format_exc(),
            "action": name,
        }


# --- Session listing ---


@app.get("/api/ai/sessions")
def ai_sessions(
    limit: int = Query(default=10, ge=1, le=100),
    before: str | None = Query(default=None),
) -> dict[str, Any]:
    """List Coach sessions in reverse-chronological order (newest
    first). `before` is a thread_id used as a paging cursor — only
    sessions older than that are returned. See
    docs/coach_chat_design.md.
    """
    return {
        "sessions": agent.list_sessions(limit=limit, before=before),
        "limit": limit,
        "before": before,
    }


@app.post("/api/ai/sessions")
def ai_sessions_new() -> dict[str, str]:
    """Mint a fresh session thread_id. Frontend can also generate this
    client-side; the endpoint exists so non-web clients (curl, future
    integrations) don't have to know the thread_id format."""
    return {"thread_id": _coach_session_thread_id()}


@app.delete("/api/ai/sessions/{thread_id}")
def ai_session_delete(thread_id: str) -> dict[str, Any]:
    """Wipe the verbatim history of one Coach session.

    Removes the LangGraph checkpoints + writes for `thread_id` plus
    its session_meta sidecar row. Long-term memories that were
    consolidated out of the session (topics/episodes in the CME) are
    deliberately NOT removed — those are commingled with other
    sessions' lessons and can't be cleanly separated.

    Used today for two flows: (1) the user explicitly clicking a trash
    icon on an archived-session divider in /coach, and (2) ad-hoc
    cleanup of smoke-test pollution during dev.
    """
    try:
        return agent.delete_session(thread_id)
    except ValueError as e:
        # thread_id doesn't pass the coach_*Z guard
        raise HTTPException(400, str(e))


@app.get("/api/ai/history/{thread_id}")
def ai_history(thread_id: str) -> dict[str, Any]:
    # get_history_with_ts walks all checkpoints for the thread and
    # tags each message with the ts of its first-seen checkpoint.
    # The front-end uses ts to insert day-boundary dividers in long
    # sessions that span multiple calendar days. ts can be null for
    # legacy checkpoints without the field — the UI treats null as
    # "no anchor."
    #
    # NOTE: emit `role` (LangChain calls it `.type` internally —
    # "human"/"ai"/"system"/"tool" — but the front-end TS type and
    # filters key on `role`). The helper already aligns the wire on
    # `role` so the response shape matches CoachMessage 1:1.
    messages = agent.get_history_with_ts(thread_id)
    return {"thread_id": thread_id, "messages": messages}


# ==================================================================
# 🧠 Cognitive Memory Engine endpoints
# ==================================================================


@app.get("/api/memory/stats")
def memory_stats() -> dict[str, Any]:
    return memory_engine.stats()


@app.get("/api/memory/context")
def memory_context(query: str = "", metrics: str | None = None) -> dict[str, Any]:
    current_metrics = json.loads(metrics) if metrics else None
    ctx = memory_engine.retrieve_working_context(query, current_metrics)
    return {"context": ctx}


@app.get("/api/memory/concierge")
def memory_concierge() -> dict[str, Any]:
    prompts = memory_engine.get_active_concierge_prompts()
    return {"prompts": prompts}


# --- Topics ---


@app.get("/api/memory/topics")
def list_topics(status: str | None = None) -> dict[str, Any]:
    topics = memory_engine.list_topics(status=status)
    return {"topics": topics}


@app.get("/api/memory/topics/{topic_id}")
def get_topic(topic_id: str) -> dict[str, Any]:
    topic = memory_engine.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Topic not found")
    return topic


@app.post("/api/memory/topics")
def create_topic(body: TopicCreate) -> dict[str, Any]:
    tid = memory_engine.create_topic(
        name=body.name,
        root_category=body.root_category,
        status=body.status,
        working_conclusion=body.working_conclusion,
    )
    return {"ok": True, "topic_id": tid}


@app.put("/api/memory/topics/{topic_id}")
def update_topic(topic_id: str, body: TopicUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    ok = memory_engine.update_topic(topic_id, **updates)
    if not ok:
        raise HTTPException(404, "Topic not found or no valid updates")
    return {"ok": True}


# --- Episodes ---


@app.get("/api/memory/episodes")
def list_episodes(
    limit: int = 20, event_type: str | None = None
) -> dict[str, Any]:
    episodes = memory_engine.list_episodes(limit=limit, event_type=event_type)
    return {"episodes": episodes}


@app.post("/api/memory/episodes")
def create_episode(body: EpisodeCreate) -> dict[str, Any]:
    eid = memory_engine.create_episode(
        event_type=body.event_type,
        context=body.context,
        lesson_learned=body.lesson_learned,
        related_topic_ids=body.related_topic_ids,
        timestamp=body.timestamp,
    )
    return {"ok": True, "episode_id": eid}


class ExternalEventCreate(BaseModel):
    """Quick-add payload for travel / illness / life_stress (P5 §4).

    `event_type` MUST be one of `MemoryOS.EXTERNAL_EVENT_TYPES`. Dates
    are inclusive YYYY-MM-DD. `description` is free text that the
    agent will see verbatim — keep it short and specific
    ("stomach bug, low energy", "demo prep crunch", "Tokyo 13h ahead").
    """
    event_type: str
    start_date: str
    end_date: str
    description: str


@app.get("/api/memory/external-events")
def list_external_events(
    start: str = Query(..., description="YYYY-MM-DD inclusive"),
    end: str = Query(..., description="YYYY-MM-DD inclusive"),
) -> dict[str, Any]:
    """Travel / illness / life_stress episodes overlapping [start, end].
    Feeds both the Health-tab `<ExternalEvents>` card and the agent's
    `get_external_events` MCP tool — single source of truth."""
    events = memory_engine.list_external_events(start, end)
    return {"start": start, "end": end, "events": events}


@app.post("/api/memory/external-events")
def create_external_event(body: ExternalEventCreate) -> dict[str, Any]:
    """Create a travel / illness / life_stress episode. Same CME row
    shape as other episodes — context carries {start_date, end_date,
    description} so list_external_events can range-filter without
    parsing prose."""
    from backend.cognitive_memory_engine import MemoryOS as _MO
    if body.event_type not in _MO.EXTERNAL_EVENT_TYPES:
        raise HTTPException(
            400,
            f"event_type must be one of {_MO.EXTERNAL_EVENT_TYPES}",
        )
    # Strict ISO-date validation. The shape-check we used originally
    # (len==10 + dashes at pos 4,7) lets garbage like "abcd-ef-gh" or
    # "2026-99-99" through, which then breaks lexicographic sorting +
    # range-overlap in list_external_events. fromisoformat catches
    # invalid month/day and non-digit chars in one shot.
    for fld, val in [("start_date", body.start_date), ("end_date", body.end_date)]:
        try:
            datetime.date.fromisoformat(val)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{fld} must be YYYY-MM-DD")
    if body.end_date < body.start_date:
        raise HTTPException(400, "end_date must be >= start_date")
    if not body.description.strip():
        raise HTTPException(400, "description required")
    eid = memory_engine.create_episode(
        event_type=body.event_type,
        context={
            "start_date": body.start_date,
            "end_date": body.end_date,
            "description": body.description.strip(),
        },
        # lesson_learned mirrors description so search_episodes
        # (keyword grep over context_json + lesson_learned) finds it
        # whether the agent searches by phrase or by event_type.
        lesson_learned=body.description.strip(),
    )
    return {"ok": True, "episode_id": eid}


@app.delete("/api/memory/external-events/{episode_id}")
def delete_external_event(episode_id: str) -> dict[str, Any]:
    """Hard-delete an external event. Idempotent — re-clicking
    delete returns ok=true even when nothing remained (matches
    delete_planned_workout's UX)."""
    removed = memory_engine.delete_episode(episode_id)
    return {"ok": True, "removed": removed, "episode_id": episode_id}


@app.get("/api/memory/episodes/search")
def search_episodes(q: str, limit: int = 10) -> dict[str, Any]:
    keywords = q.split()
    episodes = memory_engine.search_episodes(keywords, limit=limit)
    return {"episodes": episodes}


# --- Models (PR P1 — pattern store) ---


@app.get("/api/memory/models")
def list_models(
    category: str | None = None, status: str | None = None
) -> dict[str, Any]:
    models = memory_engine.list_models(category=category, status=status)
    return {"models": models, "filter": {"category": category, "status": status}}


@app.get("/api/memory/models/{model_key}")
def get_model(model_key: str) -> dict[str, Any]:
    m = memory_engine.get_model(model_key)
    if not m:
        raise HTTPException(404, f"model {model_key!r} not found")
    return m


class DecisionResolveInput(BaseModel):
    """Input for /api/memory/decisions/{id}/resolve. action enum
    matches MemoryOS.resolve_topic_decision's action arg."""
    action: Literal["merge", "create_new", "link", "reject"]
    target_topic_id: str | None = None
    target_topic_ids: list[str] | None = None


@app.get("/api/memory/decisions")
def list_decisions() -> dict[str, Any]:
    """List pending topic_decisions queue items.

    PR P2 added kind='new_model' so agents (via MCP) and any future
    UI can iterate over pending model proposals + existing
    pending new_topic / conflict / episode_linking items together.
    Filter client-side on `kind` for narrow views."""
    return {"decisions": memory_engine.list_pending_decisions()}


@app.post("/api/memory/decisions/{decision_id}/resolve")
def resolve_decision(
    decision_id: str, body: DecisionResolveInput
) -> dict[str, Any]:
    """Apply user's verdict on a parked decision.

    For kind='new_model':
      - action='create_new' → creates a model + links to source topic;
        returns model_id in `result`.
      - action='reject' → marks the row resolved with no side effects.
    """
    try:
        result = memory_engine.resolve_topic_decision(
            decision_id,
            action=body.action,
            target_topic_id=body.target_topic_id,
            target_topic_ids=body.target_topic_ids,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    if result == "":
        raise HTTPException(
            404, f"decision {decision_id!r} not found or already resolved"
        )
    return {"ok": True, "result": result}


@app.post("/api/memory/topics/{topic_id}/propose_model")
def propose_model(topic_id: str) -> dict[str, Any]:
    """PR P2 manual trigger. Ask the LLM whether `topic_id`'s
    accumulated episodes are parametrically generalizable; on yes,
    parks a 'new_model' decision the user can confirm via
    /api/memory/decisions/{id}/resolve (or MCP `resolve_decision`).

    Response shape mirrors propose_model_from_topic:
      {status: 'parked',     decision_id, proposal}
      {status: 'skipped',    reason, ...}
      {status: 'llm_error',  raw, reason}
    """
    return memory_engine.propose_model_from_topic(topic_id)


@app.post("/api/memory/models/refit/{model_key}")
def refit_model(model_key: str) -> dict[str, Any]:
    """Manually trigger a stat-derived model refit. A background
    cron will eventually replace the manual trigger; until then this
    is the single way to refresh a model's params after new data
    syncs.

    Status codes:
      • 200 — refit ran, model updated.
      • 404 — unknown `model_key`.
      • 422 — refit ran but the underlying data window was too thin
              to characterize (Garmin sync stopped, fresh install,
              etc.). Distinct from 200 so cron / monitoring can
              alert when this lingers — `curl -f` and HTTP-status-
              based health checks will see it as a non-success.
              Body still carries the `{ok: false, reason}` shape so
              human callers can parse the explanation."""
    fn = REFIT_REGISTRY.get(model_key)
    if fn is None:
        raise HTTPException(
            404,
            f"no stat refit fn registered for model_key={model_key!r}. "
            f"Known stat-derived: {sorted(REFIT_REGISTRY)}.",
        )
    result = fn(memory_engine, processor)
    if result is None:
        raise HTTPException(
            422,
            detail={"ok": False, "reason": "insufficient_data"},
        )
    return {
        "ok": True,
        "model_key": result,
        "model": memory_engine.get_model(result),
    }


# --- Pending Clarifications ---


@app.get("/api/memory/pending")
def list_pending(resolved: bool = False) -> dict[str, Any]:
    items = memory_engine.list_pending(resolved=resolved)
    return {"pending": items}


@app.post("/api/memory/pending/{pending_id}/resolve")
def resolve_pending(pending_id: str, body: PendingResolve) -> dict[str, Any]:
    ok = memory_engine.resolve_pending_question(pending_id, body.user_answer)
    if not ok:
        raise HTTPException(404, "Pending question not found or already resolved")
    return {"ok": True}


# --- Consolidation ---


@app.post("/api/memory/consolidate")
def consolidate_memory(body: ConsolidateInput) -> dict[str, Any]:
    agent.consolidate_and_learn(body.thread_id)
    return {"ok": True, "thread_id": body.thread_id}


@app.get("/healthz")
def healthz() -> dict[str, Literal["ok"]]:
    return {"status": "ok"}


@app.get("/api/debug/observability")
def observability_status() -> dict[str, Any]:
    """LangSmith tracing status (PR E). Lets the operator / agent
    check whether spans are flowing without having to grep env vars
    or restart the server. Body shape matches `langsmith_status()`
    and never echoes the API key value (would leak a secret on a
    forgotten port-forward)."""
    return langsmith_status()


# Module-level emit so the wiring is obvious in uvicorn's startup
# output. Three states: ON / MISCONFIGURED / OFF — see
# langsmith_setup.startup_log_line for the contract. Using print()
# rather than logging because uvicorn's default logger doesn't
# surface app-side logger.info() in `--reload` mode, and we want
# this line to be hard to miss.
print(startup_log_line(), flush=True)
