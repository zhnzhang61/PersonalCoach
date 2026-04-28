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


app = FastAPI(title="PersonalCoach API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

processor = DataProcessor()
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


@app.get("/api/training/weeks")
def training_weeks(block_id: str | None = None) -> dict[str, Any]:
    blocks = processor.get_blocks()
    if not blocks:
        raise HTTPException(404, "No training blocks found")
    selected = block_id or blocks[0]["id"]
    weeks = processor.get_weeks_for_block(selected)
    return {"block_id": selected, "weeks": weeks}


@app.get("/api/runs")
def runs(
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
) -> dict[str, Any]:
    if not start or not end:
        start, end = _default_window()
    rows = processor.get_activities_in_range(start, end)
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
        "summary": summary,
        "meta": meta,
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
    }


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
