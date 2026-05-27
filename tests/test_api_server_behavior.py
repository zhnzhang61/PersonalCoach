"""Per-domain behavior tests for backend/api_server.py.

Where test_endpoint_smoke.py just asserts "no 500" on every route as a
backstop, this file goes deeper on the endpoints that actually do
something interesting: mutation handlers (POST / PUT / DELETE),
action dispatch (/api/ai/action/{name}), and memory CRUD.

Each test:
  • Uses the `client` + `mock_app_deps` fixtures from conftest.py so
    `api_server.{processor, gcal, memory_engine, agent}` are MagicMocks
    isolated per-test.
  • Asserts that the handler called the right dependency method with the
    right kwargs (proves the handler→domain mapping is correct).
  • Asserts the response shape the frontend depends on.

Three test files for api_server, in order of value:
  • test_endpoint_smoke.py    — no-500 backstop, parametrized 65 routes
  • test_api_server_behavior.py (this file) — per-domain assertions
  • test_api_server_*.py per domain (later) — once any one domain
    grows past ~10 tests, split it out.
"""

from __future__ import annotations

import re
from subprocess import CompletedProcess

import pytest


# ---------------------------------------------------------------------------
# Helper for the sync subprocess mock (POST /api/sync/garmin* spawn scripts)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_subprocess_ok(monkeypatch):
    """Make subprocess.run return success without invoking garmin_sync."""
    def fake_run(cmd, **kwargs):
        return CompletedProcess(args=cmd, returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr("subprocess.run", fake_run)


@pytest.fixture
def mock_subprocess_token_expired(monkeypatch):
    """garmin_sync exited 2 → handler must surface token_expired path."""
    def fake_run(cmd, **kwargs):
        return CompletedProcess(
            args=cmd, returncode=2, stdout="", stderr="TOKEN_EXPIRED foo"
        )
    monkeypatch.setattr("subprocess.run", fake_run)


@pytest.fixture
def mock_subprocess_generic_error(monkeypatch):
    """Non-zero, non-2 exit: handler should return reason=error."""
    def fake_run(cmd, **kwargs):
        return CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="boom"
        )
    monkeypatch.setattr("subprocess.run", fake_run)


# ===========================================================================
# AI dispatch — /api/ai/action/{name}
# ===========================================================================
#
# The handler is a 5-way dispatch on `name` with two error paths
# (400 for review_workout without activity_id, 404 for an unknown
# action). Each branch hands off to a different agent method, so the
# tests check both: branch selection and arg forwarding.


class TestAiActionDispatch:
    """Verify each of the 5 actions hands off to the right agent
    method, with the right kwargs."""

    def _body(self, **extra):
        return {"thread_id": "coach_20260513T120000Z", **extra}

    def test_review_workout_requires_activity_id(self, client):
        # Without activity_id the handler raises HTTPException(400).
        # The handler catches `except HTTPException: raise`, so this
        # bubbles up as a real 400 (not the 200-with-error-body path).
        resp = client.post(
            "/api/ai/action/review_workout",
            json=self._body(),  # no activity_id
        )
        assert resp.status_code == 400
        assert "activity_id" in resp.json()["detail"]

    def test_review_workout_forwards_args(self, client):
        import backend.api_server as api_server
        api_server.agent.review_workout.return_value = "mock workout reply"
        resp = client.post(
            "/api/ai/action/review_workout",
            json=self._body(
                activity_id=12345,
                run_date="2026-05-05",
                message="extra context",
            ),
        )
        assert resp.status_code == 200
        api_server.agent.review_workout.assert_called_once_with(
            activity_id=12345,
            thread_id="coach_20260513T120000Z",
            run_date="2026-05-05",
            user_message="extra context",
        )
        assert resp.json() == {
            "thread_id": "coach_20260513T120000Z",
            "answer": "mock workout reply",
        }

    @pytest.mark.parametrize(
        "name,method",
        [
            ("make_plan", "make_plan"),
            ("review_health", "review_health"),
            ("follow_up_memory", "follow_up_memory"),
        ],
    )
    def test_three_message_actions_share_dispatch(self, client, name, method):
        """make_plan / review_health / follow_up_memory have identical
        signatures (thread_id + optional message) — verify each one
        actually routes to its own agent method, not e.g. all to
        make_plan by accident."""
        import backend.api_server as api_server
        getattr(api_server.agent, method).return_value = f"mock {method} reply"
        resp = client.post(
            f"/api/ai/action/{name}",
            json=self._body(message="hi"),
        )
        assert resp.status_code == 200
        getattr(api_server.agent, method).assert_called_once_with(
            thread_id="coach_20260513T120000Z",
            user_message="hi",
        )
        assert resp.json()["answer"] == f"mock {method} reply"

    def test_summarize_and_archive_returns_full_dict(self, client):
        import backend.api_server as api_server
        api_server.agent.summarize_and_archive.return_value = {
            "thread_id": "coach_20260513T120000Z",
            "summary": "a long day",
            "topics_added": 2,
            "episodes_added": 1,
            "closed_at": "2026-05-13T12:30:00Z",
        }
        resp = client.post(
            "/api/ai/action/summarize_and_archive",
            json=self._body(),
        )
        assert resp.status_code == 200
        # Archive returns the dict verbatim (not wrapped in {answer:...}).
        body = resp.json()
        assert body["summary"] == "a long day"
        assert body["topics_added"] == 2

    def test_unknown_action_returns_404(self, client):
        resp = client.post(
            "/api/ai/action/fly_to_mars",
            json=self._body(),
        )
        assert resp.status_code == 404
        assert "Unknown action" in resp.json()["detail"]

    def test_thread_id_required(self, client):
        resp = client.post("/api/ai/action/make_plan", json={})
        assert resp.status_code == 422  # pydantic validation

    def test_agent_exception_surfaces_as_200_with_error_body(self, client):
        """The handler wraps non-HTTPException in a try/except that
        returns 200 with `{error, traceback, action}` — that's how the
        frontend's rate-limit-aware retry helper expects to see
        provider 429s."""
        import backend.api_server as api_server
        api_server.agent.make_plan.side_effect = RuntimeError(
            "Error code: 429 — RESOURCE_EXHAUSTED"
        )
        resp = client.post(
            "/api/ai/action/make_plan",
            json=self._body(),
        )
        # Critically: NOT 500. The frontend uses 200+error to extract
        # rate-limit signal without seeing a stack trace.
        assert resp.status_code == 200
        body = resp.json()
        assert "429" in body["error"]
        assert body["action"] == "make_plan"
        assert "traceback" in body


# ===========================================================================
# AI chat + session lifecycle
# ===========================================================================


class TestAiChat:
    def test_chat_calls_agent_with_kwargs(self, client):
        import backend.api_server as api_server
        api_server.agent.chat.return_value = "I remember your last run."
        resp = client.post(
            "/api/ai/chat",
            json={
                "thread_id": "coach_20260513T120000Z",
                "message": "hi",
                "system_context": "extra",
            },
        )
        assert resp.status_code == 200
        api_server.agent.chat.assert_called_once_with(
            user_input="hi",
            thread_id="coach_20260513T120000Z",
            system_context="extra",
        )
        assert resp.json() == {
            "thread_id": "coach_20260513T120000Z",
            "answer": "I remember your last run.",
        }


class TestAiSessions:
    def test_new_session_mints_coach_timestamp_id(self, client):
        """POST /api/ai/sessions mints a fresh thread_id following the
        documented `coach_yyyymmddTHHMMSSZ` format. The Coach UI uses
        this format for filtering its session list, so the contract
        is load-bearing."""
        resp = client.post("/api/ai/sessions")
        assert resp.status_code == 200
        tid = resp.json()["thread_id"]
        assert re.match(r"^coach_\d{8}T\d{6}Z$", tid), tid

    def test_delete_session_forwards_thread_id(self, client):
        import backend.api_server as api_server
        # The autouse fixture in conftest gave `delete_session` a
        # side_effect (the coach_*Z guard). MagicMock prefers
        # side_effect over return_value, so we clear it before pinning
        # a specific return.
        api_server.agent.delete_session.side_effect = None
        api_server.agent.delete_session.return_value = {
            "thread_id": "coach_20260513T120000Z",
            "checkpoints_deleted": 7,
            "writes_deleted": 12,
            "session_meta_deleted": 1,
        }
        resp = client.delete("/api/ai/sessions/coach_20260513T120000Z")
        assert resp.status_code == 200
        api_server.agent.delete_session.assert_called_once_with(
            "coach_20260513T120000Z"
        )
        assert resp.json()["checkpoints_deleted"] == 7

    def test_delete_session_rejects_non_coach_id(self, client):
        """The autouse `mock_app_deps` fixture in conftest gives
        `agent.delete_session` a side_effect that raises ValueError on
        non-`coach_*Z` ids. The handler maps ValueError → 400."""
        resp = client.delete("/api/ai/sessions/random_thread_xyz")
        assert resp.status_code == 400
        assert "non-coach" in resp.json()["detail"]


class TestAiHistory:
    def test_history_wire_shape_uses_role_not_type(self, client):
        """Front-end's CoachMessage type keys on `role`, not LangChain's
        internal `.type`. Lock in the on-the-wire conversion in the
        helper so a backend refactor can't silently break the chat UI.

        Since PR A the endpoint forwards `get_history_with_ts` output 1:1
        — the helper already aligns the shape, so this test mocks the
        helper rather than building a fake checkpointer."""
        import backend.api_server as api_server

        api_server.agent.get_history_with_ts.return_value = [
            {"role": "human", "content": "hi", "ts": "2026-05-13T10:00:00Z"},
            {"role": "ai", "content": "hello", "ts": "2026-05-13T10:00:01Z"},
        ]
        resp = client.get("/api/ai/history/coach_20260513T120000Z")
        assert resp.status_code == 200
        body = resp.json()
        assert body["messages"][0]["role"] == "human"
        assert body["messages"][0]["content"] == "hi"
        # No `type` field — the rename was deliberate.
        assert "type" not in body["messages"][0]

    def test_history_includes_ts_per_message(self, client):
        """PR A — fix-coach-multi-day-timeline: per-message `ts` powers
        the UI's day-boundary dividers. The endpoint must pass through
        whatever the helper returns, including ts."""
        import backend.api_server as api_server

        api_server.agent.get_history_with_ts.return_value = [
            {"role": "human", "content": "q1", "ts": "2026-05-11T15:00:00Z"},
            {"role": "ai", "content": "a1", "ts": "2026-05-11T15:00:01Z"},
            {"role": "human", "content": "q2", "ts": "2026-05-12T09:00:00Z"},
        ]
        resp = client.get("/api/ai/history/coach_20260513T120000Z")
        body = resp.json()
        assert [m["ts"] for m in body["messages"]] == [
            "2026-05-11T15:00:00Z",
            "2026-05-11T15:00:01Z",
            "2026-05-12T09:00:00Z",
        ]

    def test_history_null_ts_passes_through(self, client):
        """Legacy checkpoints without ts get null — UI treats null as
        'no day anchor' (no divider). Endpoint must NOT drop the field."""
        import backend.api_server as api_server

        api_server.agent.get_history_with_ts.return_value = [
            {"role": "human", "content": "legacy", "ts": None},
        ]
        resp = client.get("/api/ai/history/coach_20260513T120000Z")
        body = resp.json()
        assert body["messages"][0]["ts"] is None
        assert "ts" in body["messages"][0]  # field present, not omitted


# ===========================================================================
# Training blocks CRUD
# ===========================================================================


class TestTrainingBlocks:
    def test_create_block_forwards_body(self, client):
        import backend.api_server as api_server
        api_server.processor.create_block.return_value = "block_mock_id"
        resp = client.post(
            "/api/training/blocks",
            json={
                "name": "Fall Marathon Build",
                "start_date": "2026-09-01",
                "end_date": "2026-10-31",
                "primary_event": "running",
            },
        )
        assert resp.status_code == 200
        # Handler unpacks the pydantic model into create_block kwargs.
        call = api_server.processor.create_block.call_args
        assert call.kwargs.get("name") == "Fall Marathon Build"
        assert call.kwargs.get("start_date") == "2026-09-01"
        assert call.kwargs.get("end_date") == "2026-10-31"

    def test_update_block_only_passes_explicit_fields(self, client):
        """Pydantic exclude-unset → unset fields don't reach
        processor.update_block. Tests the "partial update" contract."""
        import backend.api_server as api_server
        api_server.processor.update_block.return_value = True
        resp = client.put(
            "/api/training/blocks/block_001",
            json={"name": "Renamed"},  # only name; other fields untouched
        )
        assert resp.status_code == 200
        api_server.processor.update_block.assert_called_once()
        block_id, *_args = api_server.processor.update_block.call_args.args
        kwargs = api_server.processor.update_block.call_args.kwargs
        assert block_id == "block_001"
        assert kwargs == {"name": "Renamed"}  # no None-valued fields

    def test_delete_block_forwards_id(self, client):
        import backend.api_server as api_server
        api_server.processor.delete_block.return_value = True
        resp = client.delete("/api/training/blocks/block_001")
        assert resp.status_code == 200
        api_server.processor.delete_block.assert_called_once_with("block_001")


# ===========================================================================
# Manual activities CRUD
# ===========================================================================


class TestManualActivities:
    def test_create_uses_pydantic_field_names(self, client):
        """Handler unpacks ManualActivityCreate into add_manual_activity
        kwargs. Check the field-name mapping (`type` → `activity_type`,
        `duration_min` → `duration_min`)."""
        import backend.api_server as api_server
        api_server.processor.add_manual_activity.return_value = {"id": "ma_1"}
        resp = client.post(
            "/api/manual-activities",
            json={
                "date": "2026-05-13",
                "type": "gym",
                "description": "leg day",
                "duration_min": 60,
            },
        )
        assert resp.status_code == 200
        call = api_server.processor.add_manual_activity.call_args
        assert call.kwargs["activity_type"] == "gym"
        assert call.kwargs["description"] == "leg day"
        assert call.kwargs["duration_min"] == 60

    def test_update_passes_only_set_fields(self, client):
        import backend.api_server as api_server
        api_server.processor.update_manual_activity.return_value = {"id": "ma_1"}
        resp = client.put(
            "/api/manual-activities/ma_1",
            json={"description": "leg + core"},
        )
        assert resp.status_code == 200
        # First positional = id; kwargs only the field user set.
        call = api_server.processor.update_manual_activity.call_args
        assert call.args[0] == "ma_1"
        assert call.kwargs == {"description": "leg + core"}

    def test_delete_forwards_id(self, client):
        import backend.api_server as api_server
        api_server.processor.delete_manual_activity.return_value = True
        resp = client.delete("/api/manual-activities/ma_1")
        assert resp.status_code == 200
        api_server.processor.delete_manual_activity.assert_called_once_with(
            "ma_1"
        )


# ===========================================================================
# Runs — PUT laps is the only mutation here
# ===========================================================================


class TestRunsLapsUpdate:
    def test_put_laps_saves_metadata(self, client):
        """PUT /api/runs/{id}/laps stores per-run metadata (week_num,
        run_name, categories per lap, free-form notes) into manual_meta.
        Handler delegates to processor.save_run_metadata."""
        import backend.api_server as api_server
        # Handler 404s up front if get_run_laps() returns empty. Seed
        # 11 laps so the handler proceeds and we get to assert the
        # save_run_metadata call.
        api_server.processor.get_run_laps.return_value = [
            {"distance": 1609, "duration": 540, "averageHR": 150}
            for _ in range(11)
        ]
        api_server.processor.calculate_category_stats.return_value = [
            {"category": "Steady Effort", "distance_mi": 10.0, "pace": "9:00", "avg_hr": 150},
            {"category": "Marathon", "distance_mi": 1.0, "pace": "7:30", "avg_hr": 170},
        ]
        resp = client.put(
            "/api/runs/9999/laps",
            json={
                "week_num": 1,
                "run_name": "Sunday Long",
                "categories": ["Steady Effort"] * 10 + ["Marathon"],
                "notes": "felt strong",
            },
        )
        assert resp.status_code == 200
        # save_run_metadata called with kwargs unpacked from body +
        # the derived category_stats.
        call = api_server.processor.save_run_metadata.call_args
        assert call.kwargs["activity_id"] == 9999
        assert call.kwargs["week_num"] == 1
        assert call.kwargs["run_name"] == "Sunday Long"
        assert call.kwargs["notes"] == "felt strong"
        assert call.kwargs["lap_categories"] == ["Steady Effort"] * 10 + ["Marathon"]

    def test_put_laps_404_when_no_laps_exist(self, client):
        """If processor has no laps for this activity, handler 404s
        before touching anything."""
        import backend.api_server as api_server
        api_server.processor.get_run_laps.return_value = []
        resp = client.put(
            "/api/runs/9999/laps",
            json={
                "week_num": 1, "run_name": "x",
                "categories": [], "notes": "",
            },
        )
        assert resp.status_code == 404
        api_server.processor.save_run_metadata.assert_not_called()

    def test_put_laps_400_when_categories_length_mismatches(self, client):
        """Categories array length must match lap count or the handler
        400s before saving — prevents partial-write data corruption."""
        import backend.api_server as api_server
        api_server.processor.get_run_laps.return_value = [
            {"distance": 1609, "duration": 540} for _ in range(5)
        ]
        resp = client.put(
            "/api/runs/9999/laps",
            json={
                "week_num": 1, "run_name": "x",
                "categories": ["Steady Effort"] * 3,  # 3 categories for 5 laps
                "notes": "",
            },
        )
        assert resp.status_code == 400
        api_server.processor.save_run_metadata.assert_not_called()


# ===========================================================================
# Memory CRUD
# ===========================================================================


class TestMemoryTopics:
    def test_list_topics_passes_status_filter(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.list_topics.return_value = []
        resp = client.get("/api/memory/topics?status=Open")
        assert resp.status_code == 200
        api_server.memory_engine.list_topics.assert_called_once_with(
            status="Open"
        )

    def test_get_topic_404_when_missing(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.get_topic.return_value = None
        resp = client.get("/api/memory/topics/tpc_unknown")
        # The handler returns 404 when get_topic returns None.
        assert resp.status_code == 404

    def test_create_topic_forwards_pydantic_fields(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.create_topic.return_value = "tpc_new"
        resp = client.post(
            "/api/memory/topics",
            json={
                "name": "Right knee discomfort",
                "root_category": "Injury",
                "status": "Testing",
                "working_conclusion": "monitoring after foam roll",
            },
        )
        assert resp.status_code == 200
        kw = api_server.memory_engine.create_topic.call_args.kwargs
        assert kw["name"] == "Right knee discomfort"
        assert kw["root_category"] == "Injury"
        assert kw["status"] == "Testing"
        assert kw["working_conclusion"] == "monitoring after foam roll"

    def test_update_topic_strips_nones(self, client):
        """Update model has all-optional fields; handler must NOT pass
        None-valued fields to update_topic (would clobber existing
        data). Verified by checking exact kwargs."""
        import backend.api_server as api_server
        api_server.memory_engine.update_topic.return_value = True
        resp = client.put(
            "/api/memory/topics/tpc_001",
            json={"name": "Renamed"},
        )
        assert resp.status_code == 200
        call = api_server.memory_engine.update_topic.call_args
        # Positional arg is topic_id; kwargs is ONLY name (no
        # status=None, no working_conclusion=None).
        assert call.args == ("tpc_001",)
        assert call.kwargs == {"name": "Renamed"}

    def test_update_topic_404_when_engine_returns_false(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.update_topic.return_value = False
        resp = client.put(
            "/api/memory/topics/tpc_unknown",
            json={"name": "x"},
        )
        assert resp.status_code == 404


class TestMemoryEpisodes:
    def test_create_episode_forwards_fields(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.create_episode.return_value = "epi_1"
        resp = client.post(
            "/api/memory/episodes",
            json={
                "event_type": "long_run",
                "context": {"what": "20-miler", "where": "Central Park"},
                "lesson_learned": "fuel earlier",
                "related_topic_ids": ["tpc_a", "tpc_b"],
            },
        )
        assert resp.status_code == 200
        api_server.memory_engine.create_episode.assert_called_once()
        kw = api_server.memory_engine.create_episode.call_args.kwargs
        assert kw["event_type"] == "long_run"
        assert kw["lesson_learned"] == "fuel earlier"
        assert kw["related_topic_ids"] == ["tpc_a", "tpc_b"]

    def test_search_requires_q(self, client):
        # FastAPI Query(...) without default → 422 when missing
        resp = client.get("/api/memory/episodes/search")
        assert resp.status_code == 422


class TestMemoryPendingResolve:
    def test_forwards_user_answer(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.resolve_pending_question.return_value = True
        resp = client.post(
            "/api/memory/pending/pnd_001/resolve",
            json={"user_answer": "yes, that's me"},
        )
        assert resp.status_code == 200
        api_server.memory_engine.resolve_pending_question.assert_called_once_with(
            "pnd_001", "yes, that's me"
        )


class TestMemoryConsolidate:
    def test_calls_agent_consolidate_with_thread_id(self, client):
        import backend.api_server as api_server
        resp = client.post(
            "/api/memory/consolidate",
            json={"thread_id": "coach_20260513T120000Z"},
        )
        assert resp.status_code == 200
        api_server.agent.consolidate_and_learn.assert_called_once_with(
            "coach_20260513T120000Z"
        )


# ===========================================================================
# Garmin sync — three subprocess outcomes
# ===========================================================================


class TestGarminSync:
    """POST /api/sync/garmin spawns `python -m backend.garmin_sync` and
    classifies the result into ok / token_expired / error. The mocked
    subprocess.run lets us drive each branch."""

    def test_sync_success_returns_ok(self, client, mock_subprocess_ok):
        resp = client.post("/api/sync/garmin")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["reason"] is None

    def test_sync_token_expired_classified(
        self, client, mock_subprocess_token_expired
    ):
        resp = client.post("/api/sync/garmin")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["reason"] == "token_expired"

    def test_sync_generic_failure_classified(
        self, client, mock_subprocess_generic_error
    ):
        resp = client.post("/api/sync/garmin")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["reason"] == "error"
        assert body["returncode"] == 1


class TestGarminRefreshToken:
    def test_refresh_success(self, client, mock_subprocess_ok):
        resp = client.post(
            "/api/sync/garmin/refresh-token",
            json={"ticket": "ST-mock-ticket"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_refresh_requires_ticket(self, client):
        resp = client.post("/api/sync/garmin/refresh-token", json={})
        assert resp.status_code == 422
