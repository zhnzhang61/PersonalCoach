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
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agentic_coach import AgenticCoach
from cognitive_memory_engine import MemoryOS
from data_processor import DataProcessor
from google_calendar import GoogleCalendar


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse("webapp/index.html")


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
    cmd = [sys.executable, "garmin_sync.py", "--no-fallback"]
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
    cmd = [sys.executable, "garmin_ticket_login.py", "--ticket", body.ticket, "--compat"]
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
    # (curl smoke tests, future AI tools) often pass naive ISO. Default
    # to the server's local zone — matches the user's wall-clock intent
    # for "events in this date window".
    local_tz = datetime.datetime.now().astimezone().tzinfo
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=local_tz)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=local_tz)

    events: list[dict[str, Any]] = []

    # ---- Google Calendar (life events: work blocks, PT, sauna, etc.) ----
    google_connected = gcal.is_connected()
    if google_connected:
        try:
            events.extend(gcal.list_events(start_dt, end_dt))
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
    answer = agent.chat(user_input=body.message, thread_id=body.thread_id, system_context=body.system_context)
    return {"thread_id": body.thread_id, "answer": answer}


@app.get("/api/ai/history/{thread_id}")
def ai_history(thread_id: str) -> dict[str, Any]:
    messages = []
    for msg in agent.get_history(thread_id):
        content = msg.content
        if isinstance(content, list):
            content = "".join([block.get("text", "") for block in content if isinstance(block, dict)])
        messages.append({"type": msg.type, "content": str(content)})
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


@app.get("/api/memory/episodes/search")
def search_episodes(q: str, limit: int = 10) -> dict[str, Any]:
    keywords = q.split()
    episodes = memory_engine.search_episodes(keywords, limit=limit)
    return {"episodes": episodes}


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
