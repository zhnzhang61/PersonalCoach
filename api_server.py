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
agent = AgenticCoach()


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


@app.post("/api/sync/garmin")
def sync_garmin() -> dict[str, Any]:
    cmd = [sys.executable, "garmin_sync.py"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
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


@app.get("/healthz")
def healthz() -> dict[str, Literal["ok"]]:
    return {"status": "ok"}
