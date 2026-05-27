"""pytest config — registers --integration flag + shared fixtures.

Fixtures here are used by every test under tests/. The big ones:

  • `mock_app_deps` (autouse): replaces the four module-level instances
    inside `api_server` (processor / gcal / memory_engine / agent) with
    MagicMocks pre-configured to return sensible defaults. Lets us run
    the FastAPI app through `TestClient` without touching real DBs, the
    file system, the MCP subprocess, or external services.

  • `client`: a `TestClient` bound to the (mocked) FastAPI app. The
    endpoint smoke test in test_endpoint_smoke.py uses this to walk
    every route.

  • `tmp_chat_db`, `tmp_cme_db`: per-test SQLite paths so direct unit
    tests of AgenticCoach / MemoryOS don't share state.

CRITICAL design note (see codex P2 review on PR #61):
  `api_server` builds four heavy singletons at module-import time
  (DataProcessor / GoogleCalendar / MemoryOS / AgenticCoach). Each
  of those constructors touches the file system or opens a SQLite
  connection — `DataProcessor.__init__` calls `_ensure_infrastructure`
  which creates `data/blocks/`, `data/memory/user_profile.json`, etc.;
  `MemoryOS.__init__` opens `data/cognition.db` and runs migrations;
  `AgenticCoach.__init__` opens `data/chat_memory.db`. Running pytest
  with the real classes can therefore mutate the developer's live
  data and races against an open dev server.

  We avoid that by patching the four classes at conftest module load
  BEFORE `import api_server`, so the singletons it constructs are
  shape-correct mocks. The patches are then stopped so direct-class
  tests (`test_cme_v2`, `test_agentic_coach_basics`) still see the
  real implementations.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# --integration flag plumbing (unchanged from before).
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that make real LLM API calls.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: marks tests that hit real external APIs (deselect by default; enable with --integration)",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip_integration = pytest.mark.skip(reason="need --integration flag")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


# ---------------------------------------------------------------------------
# Tmp-DB fixtures for AgenticCoach / MemoryOS unit tests.
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_chat_db(tmp_path):
    """Per-test chat_memory.db path. Caller passes to AgenticCoach."""
    return str(tmp_path / "chat.db")


@pytest.fixture
def tmp_cme_db(tmp_path):
    """Per-test cognition.db path. Caller passes to MemoryOS."""
    return str(tmp_path / "cme.db")


# ---------------------------------------------------------------------------
# api_server module-level singleton mocking.
# ---------------------------------------------------------------------------
#
# api_server.py builds four heavy singletons at module import:
#   processor      = DataProcessor()
#   gcal           = GoogleCalendar()
#   memory_engine  = MemoryOS(db_path="data/cognition.db", ...)
#   agent          = AgenticCoach(memory_engine=memory_engine)
#
# These each touch real files / DBs / subprocesses. For test runs we
# tolerate the import (read-only) and swap them out before any test
# fires a request so handlers see deterministic mock returns.
#
# Defaults: empty everything. Tests that need a specific shape reach
# into `api_server.processor` / `api_server.agent` etc. and override
# the relevant method's return_value inline.


def _build_processor_mock(tmp_dir: str = "/tmp/personalcoach_test") -> MagicMock:
    """DataProcessor mock. Method names + `paths` keys match the real
    class (see data_processor.py:159 self.paths definition + the
    public methods used by api_server)."""
    import pandas as pd

    m = MagicMock(name="DataProcessor")
    # Match the real path keyset so handlers indexing into paths work.
    m.paths = {
        "activities": f"{tmp_dir}/get_activities",
        "splits": f"{tmp_dir}/get_activity_splits",
        "hr_zones": f"{tmp_dir}/get_activity_hr_in_timezones",
        "sleep": f"{tmp_dir}/get_sleep_data",
        "rhr": f"{tmp_dir}/get_rhr_day",
        "hrv": f"{tmp_dir}/get_hrv_data",
        "stress": f"{tmp_dir}/get_stress_data",
        "details": f"{tmp_dir}/get_activity_details",
        "stats_body": f"{tmp_dir}/get_stats_and_body",
        "training_readiness": f"{tmp_dir}/get_training_readiness",
        "training_status": f"{tmp_dir}/get_training_status",
        "respiration": f"{tmp_dir}/get_respiration_data",
        "fitness_age": f"{tmp_dir}/get_fitnessage_data",
        "intensity_min": f"{tmp_dir}/get_intensity_minutes_data",
        "manual": f"{tmp_dir}/manual_inputs",
        "blocks": f"{tmp_dir}/blocks/training_blocks.json",
        "aux": f"{tmp_dir}/blocks/auxiliary_log.json",
        "ledger": f"{tmp_dir}/derived/daily_health_metrics.csv",
        "weather": f"{tmp_dir}/weather",
        "user_zones": f"{tmp_dir}/manual_inputs/user_zones.json",
        "semantic_memory": f"{tmp_dir}/memory/user_profile.json",
        "episodic_memory": f"{tmp_dir}/memory/episodic_logs.json",
    }
    # Class constant a couple of handlers reach into.
    m.PACE_CLIP_MIN_PER_MI = (4.0, 20.0)

    # Defaults shaped to JSON-serialize as the real return types.
    # When `get_health_stats()` returns a non-empty list, the
    # health/today and health/timeline handlers return 200; on empty
    # list they 404. We default to one synthetic day so 200 is the
    # default — tests that want the 404 path override inline.
    m.get_health_stats.return_value = [
        {
            "date": "2026-05-11",
            "sleep_score": 75, "sleep_hours": 7.0,
            "rhr": 50, "hrv": 70, "stress": 25,
            "run_miles": 0.0, "run_mins": 0,
        }
    ]
    m.get_semantic_memory.return_value = {}
    m.get_athlete_profile_full.return_value = {
        "athlete": {}, "fitness": {"hr_zones": []},
        "current_block": None, "preferences": {}, "medical_notes": [],
    }
    m.get_readiness.return_value = {"status": "Green", "score": 80}
    m.get_last_night_sleep.return_value = {
        "date": "2026-05-11", "sleep_score": 75, "sleep_hours": 7.0,
    }
    m.get_health_snapshot.return_value = {"metrics": [], "baseline_days": 14}
    m.get_manual_activities_in_range.return_value = []
    m.load_json_safe.return_value = []
    m.add_manual_activity.return_value = {"id": "manual_mock", "ok": True}
    m.update_manual_activity.return_value = {"id": "manual_mock", "ok": True}
    m.delete_manual_activity.return_value = True
    # PR P3 — daily check-ins. Empty list by default → GET returns
    # nothing, GET /{date} returns None → handler 404s. Tests that
    # care override inline.
    m.list_checkins_in_range.return_value = []
    m.get_checkin_by_date.return_value = None
    m.upsert_checkin.return_value = {
        "date": "2026-05-27", "sleep_quality": 4,
        "created_at": "2026-05-27T12:00:00Z",
        "updated_at": "2026-05-27T12:00:00Z",
    }
    m.delete_checkin.return_value = True
    # PR P4a — planned workouts.
    m.list_planned_workouts_in_range.return_value = []
    m.get_planned_workout.return_value = None
    m.upsert_planned_workout.return_value = {
        "id": "plan_mock", "date": "2026-05-30", "type": "tempo",
        "created_at": "2026-05-27T12:00:00Z",
        "updated_at": "2026-05-27T12:00:00Z",
    }
    m.delete_planned_workout.return_value = True
    # PR P5 — route profile + external events.
    m.compute_route_profile.return_value = None
    m.get_training_load.return_value = {
        "window_days": 28, "acute_load_mi": 0.0, "chronic_load_mi": 0.0,
        "acwr": None,
    }
    m.get_blocks.return_value = []
    m.create_block.return_value = "block_mock"
    m.update_block.return_value = True
    m.delete_block.return_value = True
    m.get_weeks_for_block.return_value = []
    m.compute_cycle_and_week_stats.return_value = {}
    m.get_monthly_activity_stats.return_value = []
    m.get_activities_in_range.return_value = []
    m.list_runs.return_value = []
    m.list_manual_activities.return_value = []
    m.get_run_laps.return_value = []
    # get_activity_telemetry returns (raw_df, ai_df); raw=None → 404.
    m.get_activity_telemetry.return_value = (
        pd.DataFrame(), pd.DataFrame(),
    )
    m.compute_telemetry_summary.return_value = {}
    m.get_run_chat_history.return_value = []
    m.get_run_route.return_value = None
    m.get_run_weather.return_value = None
    m.build_ai_context.return_value = None
    m.save_run_chat_message.return_value = None
    m.compile_health_ledger.return_value = []
    return m


def _build_agent_mock() -> MagicMock:
    """AgenticCoach mock. Action methods return short canned strings;
    session helpers return empty lists. Method names match what
    api_server actually calls (analyze_run, analyze_health,
    consolidate_and_learn)."""
    m = MagicMock(name="AgenticCoach")
    m.chat.return_value = "mock chat reply"
    m.review_workout.return_value = "mock review_workout reply"
    m.make_plan.return_value = "mock make_plan reply"
    m.review_health.return_value = "mock review_health reply"
    m.follow_up_memory.return_value = "mock follow_up_memory reply"
    m.summarize_and_archive.return_value = {
        "thread_id": "coach_20260511T000000Z",
        "summary": "mock summary",
        "topics_added": 0,
        "episodes_added": 0,
        "closed_at": "2026-05-11T00:00:00Z",
    }
    m.list_sessions.return_value = []
    m.get_history.return_value = []

    # delete_session has a thread_id guard in production (refuses
    # anything that doesn't look like `coach_*Z`); the FastAPI handler
    # turns the ValueError into a 400. Mirror that here so the smoke
    # test for the bad-thread-id 400 path actually exercises the
    # guard, not the canned return.
    def _fake_delete_session(thread_id: str):
        if not (thread_id.startswith("coach_") and thread_id.endswith("Z")):
            raise ValueError(
                f"refusing to delete non-coach thread_id: {thread_id!r}"
            )
        return {
            "thread_id": thread_id,
            "checkpoints_deleted": 0,
            "writes_deleted": 0,
            "session_meta_deleted": 0,
        }

    m.delete_session.side_effect = _fake_delete_session
    m.analyze_run.return_value = "mock run analysis"
    m.analyze_health.return_value = "mock health analysis"
    m.consolidate_and_learn.return_value = None
    return m


def _build_memory_engine_mock() -> MagicMock:
    """MemoryOS mock. Method names match the real class — `stats()`
    not `get_stats`, `list_pending` not `get_pending_clarifications`,
    `resolve_pending_question` not `resolve_pending`."""
    m = MagicMock(name="MemoryOS")
    m.stats.return_value = {"topics": 0, "episodes": 0, "pending": 0}
    m.retrieve_working_context.return_value = ""
    m.get_active_concierge_prompts.return_value = ""
    m.list_topics.return_value = []
    m.get_topic.return_value = None
    m.create_topic.return_value = "tpc_mock"
    m.update_topic.return_value = True
    m.list_episodes.return_value = []
    m.search_episodes.return_value = []
    m.create_episode.return_value = "epi_mock"
    m.list_pending.return_value = []
    m.resolve_pending_question.return_value = True
    # PR P5 — external context events (§4).
    m.list_external_events.return_value = []
    m.delete_episode.return_value = True
    return m


def _build_gcal_mock() -> MagicMock:
    """GoogleCalendar mock. Method names match real class:
    authorization_url returns a (url, state) tuple, is_connected /
    list_events / finish_flow / disconnect."""
    m = MagicMock(name="GoogleCalendar")
    m.authorization_url.return_value = (
        "https://accounts.google.com/o/oauth2/auth?mock=1", "mock_state",
    )
    m.is_connected.return_value = False
    m.list_events.return_value = []
    m.finish_flow.return_value = None
    m.disconnect.return_value = None
    # PR P4a — Cal write methods. Default to "not connected" so any
    # test exercising the write path must explicitly opt in by
    # setting m.is_connected.return_value = True. Each write returns
    # the same normalized shape list_events emits.
    m.insert_event.return_value = {
        "source": "google", "id": "evt_mock", "title": "mock",
        "start": "2026-05-30T09:00:00", "end": "2026-05-30T10:00:00",
        "all_day": False, "calendar_id": "primary",
    }
    m.update_event.return_value = {
        "source": "google", "id": "evt_mock", "title": "mock",
        "start": "2026-05-30T09:00:00", "end": "2026-05-30T10:00:00",
        "all_day": False, "calendar_id": "primary",
    }
    m.delete_event.return_value = True
    return m


# ---------------------------------------------------------------------------
# Class-level patches activated BEFORE importing api_server.
# ---------------------------------------------------------------------------
#
# Order matters. Module-level execution sequence in this file is:
#
#   1. patch("backend.data_processor.DataProcessor", …).start()        ← active
#   2. patch("backend.google_calendar.GoogleCalendar", …).start()      ← active
#   3. patch("backend.cognitive_memory_engine.MemoryOS", …).start()    ← active
#   4. patch("backend.agentic_coach.AgenticCoach", …).start()          ← active
#   5. import api_server   ← module-load constructs singletons,
#                            sees the patched (mock) classes, never
#                            touches real data/.
#   6. for p in patches:  p.stop()   ← real classes restored.
#
# After step 6, `api_server.{processor,gcal,memory_engine,agent}` are
# bound to shape-correct MagicMocks, and the *classes* themselves are
# back to their real implementations — so the CME and AgenticCoach
# direct-construction tests (test_cme_v2, test_agentic_coach_basics)
# still get the real code paths.

_class_patches = [
    patch("backend.data_processor.DataProcessor", return_value=_build_processor_mock()),
    patch("backend.google_calendar.GoogleCalendar", return_value=_build_gcal_mock()),
    patch("backend.cognitive_memory_engine.MemoryOS", return_value=_build_memory_engine_mock()),
    patch("backend.agentic_coach.AgenticCoach", return_value=_build_agent_mock()),
]
for _p in _class_patches:
    _p.start()

# Construct singletons under the mocked classes. After this import the
# api_server module's globals reference our mock instances.
import backend.api_server as api_server  # noqa: E402

for _p in _class_patches:
    _p.stop()


@pytest.fixture(autouse=True)
def mock_app_deps(monkeypatch):
    """Replace api_server's four module-level singletons with FRESH
    per-test mocks.

    The module-level patches above already installed mock instances
    at import time, but those persist for the whole pytest session —
    they'd accumulate `.call_args_list` state across tests. This
    autouse fixture swaps in a freshly-built mock per test so each
    test starts with a clean slate. Tests that want a specific return
    value override the relevant method directly:

        api_server.agent.chat.return_value = "custom"
    """
    monkeypatch.setattr(api_server, "processor", _build_processor_mock())
    monkeypatch.setattr(api_server, "agent", _build_agent_mock())
    monkeypatch.setattr(api_server, "memory_engine", _build_memory_engine_mock())
    monkeypatch.setattr(api_server, "gcal", _build_gcal_mock())

    # Redirect the sync-state sidecar to /tmp so the smoke for
    # POST /api/sync/garmin (which writes the file on success) doesn't
    # leave a `data/sync_state.json` behind in real working trees.
    import tempfile
    from pathlib import Path

    monkeypatch.setattr(
        api_server,
        "SYNC_STATE_PATH",
        Path(tempfile.gettempdir()) / "personalcoach_test_sync_state.json",
    )

    # Pin PERSONAL_COACH_API_BASE so anything reading it doesn't
    # accidentally try a real loopback call.
    monkeypatch.setenv("PERSONAL_COACH_API_BASE", "http://test.invalid")


@pytest.fixture
def client():
    """FastAPI TestClient bound to the (mocked) api_server.app."""
    from fastapi.testclient import TestClient

    return TestClient(api_server.app)
