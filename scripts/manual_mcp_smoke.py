"""Manual smoke-test for the personal-coach MCP server via stdio.

This is a dev tool, NOT a pytest target — it spawns a real subprocess,
talks to a live api_server, and prints output for a human to eyeball.
It used to live as `test_mcp_tools.py` at the repo root where pytest
auto-collected it and choked on the network + subprocess side effects.
Moved to `scripts/` and renamed during Phase 2 testability work.

Spawns `personal_coach_mcp` as a subprocess, lists its tools, and calls
each with a sensible default. Prints a compact summary so a human can
eyeball the output before we wire any agent up to it.

Usage:
    PERSONAL_COACH_API_BASE=http://127.0.0.1:8766 \
        uv run python scripts/manual_mcp_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _short(payload, limit: int = 280) -> str:
    """Trim long responses so the smoke output stays readable while still
    showing structure. Pretty-print top-level keys + a hint at value types."""
    s = json.dumps(payload, default=str)
    if len(s) <= limit:
        return s
    return s[:limit] + f" …({len(s)} chars)"


# Each entry is (tool_name, args_dict, what_to_check_in_one_line).
# Args are tuned to today's data (May 7 2026). The smoke run will print
# a compact one-line summary per tool plus a few sample fields.
CALLS: list[tuple[str, dict, str]] = [
    # Profile + state
    ("get_athlete_profile", {}, "identity + RPE-named HR zones + cycle phase"),
    ("get_readiness", {}, "today's green/yellow/red"),
    ("get_training_load", {"window_days": 28}, "ACWR + weekly trend"),
    # Runs (three-stream coach view)
    ("list_runs", {"start": "2026-05-01", "end": "2026-05-09"}, "compact run list"),
    ("get_run_detail", {"activity_id": 22739453672}, "Weehawken 10mi (objective + perceived)"),
    ("get_run_telemetry", {"activity_id": 22739453672, "downsample_sec": 60}, "buckets + drift"),
    ("get_run_weather", {"activity_id": 22739453672}, "5/2 weather"),
    # Cycle / monthly
    ("list_blocks", {}, "3 blocks + active id"),
    ("get_cycle_stats",
     {"block_id": "block_002", "week_start": "2026-05-04", "week_end": "2026-05-10"},
     "Pre Fall 2026 Build, week 2"),
    ("get_monthly_stats", {"activity_type": "running"}, "23 months running"),
    # Manuals + calendar + plan
    ("list_manual_activities",
     {"start": "2026-04-25", "end": "2026-05-09"},
     "steam room entries"),
    ("get_manual_activity", {"activity_id": "manual_1777658163"}, "single steam room"),
    ("get_calendar_events",
     {"start": "2026-05-05T00:00:00", "end": "2026-05-12T23:59:59"},
     "google + manual + run merge"),
    ("get_workout_plan", {"date": "2026-05-09"}, "Phase 1 always null"),
    # CME
    ("recall_topics", {"status": "active"}, "active CME topics"),
    ("search_episodes", {"keywords": ["hot", "long run"], "limit": 3}, "episodic recall"),
    ("get_pending_clarifications", {}, "unresolved questions"),
]


async def run_smoke() -> None:
    api_base = os.environ.get("PERSONAL_COACH_API_BASE", "http://127.0.0.1:8765")
    print(f"== personal-coach MCP smoke test ==")
    print(f"   API base: {api_base}")
    print()

    params = StdioServerParameters(
        command="uv",
        args=["run", "python", "-m", "backend.personal_coach_mcp"],
        env={
            **os.environ,
            "PERSONAL_COACH_API_BASE": api_base,
        },
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_resp = await session.list_tools()
            tool_names = [t.name for t in tools_resp.tools]
            print(f"tools registered ({len(tool_names)}): {', '.join(tool_names)}")
            print()

            for name, args, hint in CALLS:
                try:
                    result = await session.call_tool(name, args)
                    # FastMCP returns CallToolResult.content as a list of
                    # TextContent blocks; first block holds the JSON payload.
                    text = result.content[0].text if result.content else ""
                    parsed = json.loads(text) if text else None
                    if result.isError:
                        print(f"❌ {name}({args})")
                        print(f"   error: {_short(parsed)}")
                    else:
                        print(f"✅ {name}({args}) — {hint}")
                        print(f"   {_short(parsed)}")
                except Exception as e:
                    print(f"💥 {name}({args}) — exception: {e}")
                print()


if __name__ == "__main__":
    asyncio.run(run_smoke())
