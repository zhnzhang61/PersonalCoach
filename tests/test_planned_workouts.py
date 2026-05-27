"""Tests for the PR P4a planned-workouts pipeline.

Four concerns:
1. DataProcessor storage + CRUD (JSON file shape, upsert/patch
   semantics, validation).
2. GoogleCalendar write methods (insert/update/delete event) — mocked
   googleapiclient so no network. Locks in the call shape passed to
   the API.
3. /api/planned-workouts endpoint behavior + Cal dual-write
   coordination + degradation when Cal not connected / fails.
4. MCP tools: get_planned_workouts (read path), propose_workout_plan
   (batch write with cal_synced flag).
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.data_processor import DataProcessor


# ---------------------------------------------------------------------------
# DataProcessor CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def dp(tmp_path):
    return DataProcessor(data_dir=str(tmp_path / "data"))


class TestPlannedWorkoutSchemaSetup:
    def test_path_registered(self, dp):
        assert "planned_workouts" in dp.paths
        assert dp.paths["planned_workouts"].endswith(
            "manual_inputs/planned_workouts.json"
        )

    def test_initialized_as_empty_list(self, dp):
        assert os.path.exists(dp.paths["planned_workouts"])
        with open(dp.paths["planned_workouts"]) as f:
            assert json.load(f) == []


class TestPlannedWorkoutCRUD:
    def test_create_returns_row_with_id_and_timestamps(self, dp):
        p = dp.upsert_planned_workout(
            date="2026-05-30",
            type="tempo",
            target_pace_min_mi=7.5,
            target_hr=160,
            distance_mi=5.0,
            notes="8x400m at 5K pace",
        )
        assert p["id"].startswith("plan_")
        assert p["date"] == "2026-05-30"
        assert p["type"] == "tempo"
        assert p["target_pace_min_mi"] == 7.5
        assert p["target_hr"] == 160
        assert p["distance_mi"] == 5.0
        assert p["notes"] == "8x400m at 5K pace"
        assert "created_at" in p
        assert "updated_at" in p

    def test_patch_preserves_untouched_fields(self, dp):
        p = dp.upsert_planned_workout(
            date="2026-05-30", type="tempo",
            target_pace_min_mi=7.5, target_hr=160, distance_mi=5.0,
        )
        patched = dp.upsert_planned_workout(
            p["id"], target_pace_min_mi=7.3, notes="tightened pace"
        )
        assert patched["target_pace_min_mi"] == 7.3
        assert patched["notes"] == "tightened pace"
        # untouched
        assert patched["target_hr"] == 160
        assert patched["distance_mi"] == 5.0
        # timestamp bumped
        assert patched["updated_at"] >= p["updated_at"]

    def test_patch_can_clear_optional_field(self, dp):
        """Passing None on an optional field removes it from the row.
        Mirrors the manual_activities nullable-field convention so
        callers can edit-to-blank intentionally."""
        p = dp.upsert_planned_workout(
            date="2026-05-30", type="tempo", target_hr=160, notes="placeholder"
        )
        patched = dp.upsert_planned_workout(p["id"], notes=None)
        assert "notes" not in patched
        # target_hr untouched
        assert patched["target_hr"] == 160

    def test_patch_cannot_clear_required_field(self, dp):
        p = dp.upsert_planned_workout(date="2026-05-30", type="tempo")
        with pytest.raises(ValueError, match="required field"):
            dp.upsert_planned_workout(p["id"], date=None)

    def test_patch_missing_id_raises_keyerror(self, dp):
        with pytest.raises(KeyError):
            dp.upsert_planned_workout("plan_doesnotexist", notes="x")

    def test_get_returns_row_by_id(self, dp):
        p = dp.upsert_planned_workout(date="2026-05-30", type="easy")
        got = dp.get_planned_workout(p["id"])
        assert got is not None
        assert got["id"] == p["id"]

    def test_get_missing_returns_none(self, dp):
        assert dp.get_planned_workout("plan_missing") is None

    def test_list_range_oldest_first(self, dp):
        """Plans look FORWARD — list is ascending so the user sees
        "next workout is X" naturally. Opposite of check-ins which
        are descending (most-recent-first)."""
        dp.upsert_planned_workout(date="2026-06-01", type="long")
        dp.upsert_planned_workout(date="2026-05-28", type="easy")
        dp.upsert_planned_workout(date="2026-05-30", type="tempo")
        rows = dp.list_planned_workouts_in_range("2026-05-28", "2026-06-01")
        assert [r["date"] for r in rows] == ["2026-05-28", "2026-05-30", "2026-06-01"]

    def test_delete_removes_row(self, dp):
        p = dp.upsert_planned_workout(date="2026-05-30", type="easy")
        assert dp.delete_planned_workout(p["id"]) is True
        assert dp.get_planned_workout(p["id"]) is None

    def test_delete_missing_returns_false(self, dp):
        assert dp.delete_planned_workout("plan_missing") is False


class TestPlannedWorkoutValidation:
    def test_bad_date_format_rejected(self, dp):
        with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
            dp.upsert_planned_workout(date="2026/05/30", type="run")

    def test_non_numeric_target_hr_rejected(self, dp):
        with pytest.raises(ValueError, match="target_hr"):
            dp.upsert_planned_workout(date="2026-05-30", type="run", target_hr="high")

    def test_missing_required_on_create_rejected(self, dp):
        with pytest.raises(ValueError, match="date \\+ type"):
            dp.upsert_planned_workout(date="2026-05-30")  # missing type

    def test_numeric_field_coerces_str_to_float(self, dp):
        """JSON / form payloads sometimes arrive as strings; coerce
        rather than reject."""
        p = dp.upsert_planned_workout(
            date="2026-05-30", type="tempo", target_pace_min_mi="7.5"
        )
        assert p["target_pace_min_mi"] == 7.5
        assert isinstance(p["target_pace_min_mi"], float)

    def test_unknown_type_accepted(self, dp):
        """Coaches invent workout types — don't reject. Just store as-is."""
        p = dp.upsert_planned_workout(date="2026-05-30", type="threshold-by-feel")
        assert p["type"] == "threshold-by-feel"


# ---------------------------------------------------------------------------
# Google Calendar write methods (mocked googleapiclient)
# ---------------------------------------------------------------------------


@pytest.fixture
def gcal_with_creds(tmp_path):
    """Build a GoogleCalendar pointed at a per-test data dir +
    pre-load a fake authorized-user file so `_load_creds` returns
    truthy without actually hitting Google. Subsequent build() →
    service calls are then monkeypatched per-test."""
    from backend.google_calendar import GoogleCalendar

    gc = GoogleCalendar(data_dir=str(tmp_path / "data"))
    # Write a minimal Credentials JSON. Real loader uses
    # Credentials.from_authorized_user_file which we'll mock around.
    gc.token_path.parent.mkdir(parents=True, exist_ok=True)
    gc.token_path.write_text(json.dumps({
        "token": "fake", "refresh_token": "fake_refresh",
        "client_id": "x", "client_secret": "y",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        ],
    }))
    return gc


class TestGoogleCalWriteMethods:
    def test_insert_event_passes_summary_start_end_description(
        self, gcal_with_creds
    ):
        from backend import google_calendar as gc_module

        # Mock _load_creds to short-circuit token refresh
        gcal_with_creds._load_creds = MagicMock(return_value=MagicMock())
        # Mock googleapiclient.discovery.build → service.events().insert
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {
            "id": "evt_new", "summary": "Tempo workout",
            "start": {"dateTime": "2026-05-30T09:00:00"},
            "end": {"dateTime": "2026-05-30T10:00:00"},
            "description": "x",
        }
        with patch.object(gc_module, "build", return_value=mock_service):
            r = gcal_with_creds.insert_event(
                summary="Tempo workout",
                start="2026-05-30T09:00:00",
                end="2026-05-30T10:00:00",
                description="personalcoach.training=true\ntype: tempo",
            )
        assert r["id"] == "evt_new"
        # Check the body we sent
        insert_call = mock_service.events.return_value.insert.call_args
        body = insert_call.kwargs["body"]
        assert body["summary"] == "Tempo workout"
        assert body["start"] == {"dateTime": "2026-05-30T09:00:00"}
        assert body["end"] == {"dateTime": "2026-05-30T10:00:00"}
        assert "personalcoach.training=true" in body["description"]

    def test_insert_event_all_day_when_date_only(self, gcal_with_creds):
        """Date-only ISO string ('2026-05-30') becomes an all-day event
        on Google's side via the `date` key (vs `dateTime`)."""
        from backend import google_calendar as gc_module

        gcal_with_creds._load_creds = MagicMock(return_value=MagicMock())
        mock_service = MagicMock()
        mock_service.events.return_value.insert.return_value.execute.return_value = {
            "id": "evt_allday",
            "start": {"date": "2026-05-30"}, "end": {"date": "2026-05-31"},
        }
        with patch.object(gc_module, "build", return_value=mock_service):
            gcal_with_creds.insert_event(
                summary="Easy run",
                start="2026-05-30",
                end="2026-05-31",
            )
        body = mock_service.events.return_value.insert.call_args.kwargs["body"]
        assert body["start"] == {"date": "2026-05-30"}
        assert body["end"] == {"date": "2026-05-31"}

    def test_update_event_patches_only_provided_fields(self, gcal_with_creds):
        from backend import google_calendar as gc_module

        gcal_with_creds._load_creds = MagicMock(return_value=MagicMock())
        mock_service = MagicMock()
        mock_service.events.return_value.patch.return_value.execute.return_value = {
            "id": "evt_existing", "summary": "Tempo (updated)",
            "start": {"dateTime": "2026-05-30T09:00:00"},
            "end": {"dateTime": "2026-05-30T10:00:00"},
        }
        with patch.object(gc_module, "build", return_value=mock_service):
            gcal_with_creds.update_event(
                "evt_existing", summary="Tempo (updated)"
            )
        body = mock_service.events.return_value.patch.call_args.kwargs["body"]
        assert body == {"summary": "Tempo (updated)"}

    def test_update_event_with_no_fields_calls_get_not_patch(
        self, gcal_with_creds
    ):
        """Pre-empt the empty-body case — calling .patch() with {} is a
        wasted API hit. Verify we route to a read instead."""
        from backend import google_calendar as gc_module

        gcal_with_creds._load_creds = MagicMock(return_value=MagicMock())
        mock_service = MagicMock()
        mock_service.events.return_value.get.return_value.execute.return_value = {
            "id": "evt_existing", "summary": "Tempo",
            "start": {}, "end": {},
        }
        with patch.object(gc_module, "build", return_value=mock_service):
            gcal_with_creds.update_event("evt_existing")  # no fields
        mock_service.events.return_value.patch.assert_not_called()
        mock_service.events.return_value.get.assert_called_once()

    def test_delete_event_returns_true_on_success(self, gcal_with_creds):
        from backend import google_calendar as gc_module

        gcal_with_creds._load_creds = MagicMock(return_value=MagicMock())
        mock_service = MagicMock()
        mock_service.events.return_value.delete.return_value.execute.return_value = None
        with patch.object(gc_module, "build", return_value=mock_service):
            assert gcal_with_creds.delete_event("evt_to_kill") is True

    def test_delete_event_returns_false_on_404(self, gcal_with_creds):
        """Idempotent delete — if the event's already gone, don't
        crash a cleanup sweep."""
        from googleapiclient.errors import HttpError

        from backend import google_calendar as gc_module

        gcal_with_creds._load_creds = MagicMock(return_value=MagicMock())
        mock_service = MagicMock()
        not_found = HttpError(
            resp=MagicMock(status=404), content=b'{"error":"not found"}'
        )
        not_found.resp = MagicMock(status=404)
        mock_service.events.return_value.delete.return_value.execute.side_effect = not_found
        with patch.object(gc_module, "build", return_value=mock_service):
            assert gcal_with_creds.delete_event("evt_already_gone") is False

    def test_write_when_not_connected_raises(self, tmp_path):
        from backend.google_calendar import GoogleCalendar

        gc = GoogleCalendar(data_dir=str(tmp_path / "data"))
        # No token file → _load_creds returns None
        with pytest.raises(RuntimeError, match="not connected"):
            gc.insert_event(
                summary="x", start="2026-05-30", end="2026-05-30"
            )


# ---------------------------------------------------------------------------
# API endpoint behavior
# ---------------------------------------------------------------------------


class TestPlannedWorkoutsAPI:
    def test_get_list_forwards_range(self, client):
        import backend.api_server as api_server
        api_server.processor.list_planned_workouts_in_range.return_value = [
            {"id": "plan_a", "date": "2026-05-30", "type": "tempo"},
        ]
        resp = client.get(
            "/api/planned-workouts?start=2026-05-28&end=2026-06-01"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["start"] == "2026-05-28"
        assert body["end"] == "2026-06-01"
        assert body["planned_workouts"][0]["id"] == "plan_a"

    def test_get_by_id_404_when_missing(self, client):
        import backend.api_server as api_server
        api_server.processor.get_planned_workout.return_value = None
        resp = client.get("/api/planned-workouts/plan_does_not_exist")
        assert resp.status_code == 404

    def test_create_without_cal_connected_returns_cal_synced_false(self, client):
        """User hasn't connected Google Cal. We still create the JSON
        row but `cal_synced=false` so the UI can hint to reconnect."""
        import backend.api_server as api_server
        api_server.gcal.is_connected.return_value = False
        api_server.processor.upsert_planned_workout.return_value = {
            "id": "plan_new", "date": "2026-05-30", "type": "tempo",
        }
        resp = client.post(
            "/api/planned-workouts",
            json={"date": "2026-05-30", "type": "tempo", "distance_mi": 5.0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["cal_synced"] is False
        # No Cal insert attempted
        api_server.gcal.insert_event.assert_not_called()

    def test_create_with_cal_connected_dual_writes_and_captures_event_id(
        self, client
    ):
        import backend.api_server as api_server
        api_server.gcal.is_connected.return_value = True
        api_server.gcal.insert_event.return_value = {
            "id": "evt_xyz", "title": "Tempo workout",
        }
        # processor returns the row both on first create AND on the
        # follow-up patch that attaches cal_event_id.
        api_server.processor.upsert_planned_workout.side_effect = [
            {"id": "plan_new", "date": "2026-05-30", "type": "tempo"},
            {"id": "plan_new", "date": "2026-05-30", "type": "tempo",
             "cal_event_id": "evt_xyz"},
        ]
        resp = client.post(
            "/api/planned-workouts",
            json={"date": "2026-05-30", "type": "tempo"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cal_synced"] is True
        assert body["planned_workout"]["cal_event_id"] == "evt_xyz"
        # Verify the second processor call attached cal_event_id
        second_call = api_server.processor.upsert_planned_workout.call_args_list[1]
        assert second_call.args[0] == "plan_new"
        assert second_call.kwargs["cal_event_id"] == "evt_xyz"

    def test_cal_payload_carries_timezone_offset(self, client):
        """Google Cal's dateTime field requires either a UTC offset or
        an explicit timeZone. A naked '2026-05-30T09:00:00' is rejected
        with HTTP 400 and silently flips cal_synced=false. Regression
        guard: the payload we hand insert_event must include an offset
        like '-04:00' or '+09:00' on both start and end."""
        import re
        import backend.api_server as api_server
        api_server.gcal.is_connected.return_value = True
        api_server.gcal.insert_event.return_value = {"id": "evt_tz"}
        api_server.processor.upsert_planned_workout.side_effect = [
            {"id": "plan_tz", "date": "2026-05-30", "type": "tempo",
             "duration_min": 45},
            {"id": "plan_tz", "date": "2026-05-30", "type": "tempo",
             "duration_min": 45, "cal_event_id": "evt_tz"},
        ]
        resp = client.post(
            "/api/planned-workouts",
            json={"date": "2026-05-30", "type": "tempo", "duration_min": 45},
        )
        assert resp.status_code == 200
        kwargs = api_server.gcal.insert_event.call_args.kwargs
        offset_re = re.compile(r"[+-]\d{2}:\d{2}$")
        assert offset_re.search(kwargs["start"]), (
            f"start lacks tz offset: {kwargs['start']!r}"
        )
        assert offset_re.search(kwargs["end"]), (
            f"end lacks tz offset: {kwargs['end']!r}"
        )
        # Date and start hour should still be 09:00 local.
        assert kwargs["start"].startswith("2026-05-30T09:00:00")
        # End = start + 45min = 09:45 local.
        assert kwargs["end"].startswith("2026-05-30T09:45:00")

    def test_create_cal_failure_falls_back_to_json_only(self, client):
        """Network blip or Cal API error — JSON row was saved, Cal
        wasn't. Response says cal_synced=false but ok=true. Frontend
        can surface a "Cal sync degraded" badge."""
        import backend.api_server as api_server
        api_server.gcal.is_connected.return_value = True
        api_server.gcal.insert_event.side_effect = RuntimeError("Cal API down")
        api_server.processor.upsert_planned_workout.return_value = {
            "id": "plan_new", "date": "2026-05-30", "type": "tempo",
        }
        resp = client.post(
            "/api/planned-workouts",
            json={"date": "2026-05-30", "type": "tempo"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["cal_synced"] is False

    def test_create_validation_400(self, client):
        import backend.api_server as api_server
        api_server.processor.upsert_planned_workout.side_effect = ValueError(
            "date must be YYYY-MM-DD"
        )
        resp = client.post(
            "/api/planned-workouts",
            json={"date": "2026/05/30", "type": "tempo"},
        )
        assert resp.status_code == 400

    def test_put_with_cal_event_id_syncs_to_cal(self, client):
        import backend.api_server as api_server
        api_server.gcal.is_connected.return_value = True
        api_server.processor.upsert_planned_workout.return_value = {
            "id": "plan_a", "date": "2026-05-30", "type": "tempo",
            "cal_event_id": "evt_xyz", "target_pace_min_mi": 7.3,
        }
        resp = client.put(
            "/api/planned-workouts/plan_a",
            json={"target_pace_min_mi": 7.3},
        )
        assert resp.status_code == 200
        assert resp.json()["cal_synced"] is True
        api_server.gcal.update_event.assert_called_once()
        # First positional = event id
        assert api_server.gcal.update_event.call_args.args[0] == "evt_xyz"

    def test_put_without_cal_event_id_skips_cal(self, client):
        """Row exists locally but was never synced to Cal (created
        while disconnected). PUT shouldn't try to update an event
        that doesn't exist."""
        import backend.api_server as api_server
        api_server.gcal.is_connected.return_value = True
        api_server.processor.upsert_planned_workout.return_value = {
            "id": "plan_a", "date": "2026-05-30", "type": "tempo",
            # no cal_event_id
        }
        resp = client.put(
            "/api/planned-workouts/plan_a", json={"notes": "x"}
        )
        assert resp.status_code == 200
        assert resp.json()["cal_synced"] is False
        api_server.gcal.update_event.assert_not_called()

    def test_put_404_when_missing(self, client):
        import backend.api_server as api_server
        api_server.processor.upsert_planned_workout.side_effect = KeyError("missing")
        resp = client.put(
            "/api/planned-workouts/plan_missing", json={"notes": "x"}
        )
        assert resp.status_code == 404

    def test_put_empty_body_400(self, client):
        resp = client.put(
            "/api/planned-workouts/plan_a", json={}
        )
        assert resp.status_code == 400

    def test_delete_removes_cal_event_when_linked(self, client):
        import backend.api_server as api_server
        api_server.processor.get_planned_workout.return_value = {
            "id": "plan_a", "date": "2026-05-30", "type": "tempo",
            "cal_event_id": "evt_xyz",
        }
        api_server.gcal.is_connected.return_value = True
        resp = client.delete("/api/planned-workouts/plan_a")
        assert resp.status_code == 200
        assert resp.json()["cal_synced"] is True
        api_server.gcal.delete_event.assert_called_once_with("evt_xyz")
        api_server.processor.delete_planned_workout.assert_called_once_with("plan_a")

    def test_delete_404_when_missing(self, client):
        import backend.api_server as api_server
        api_server.processor.get_planned_workout.return_value = None
        resp = client.delete("/api/planned-workouts/plan_missing")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


class TestPlannedWorkoutsMCP:
    @pytest.fixture
    def fake_get_post(self):
        """Mock both _get and _post — propose_workout_plan uses POST."""
        get_rec = AsyncMock(return_value={"planned_workouts": []})
        post_rec = AsyncMock(return_value={
            "ok": True, "cal_synced": True,
            "planned_workout": {"id": "plan_x"},
        })
        with patch("backend.personal_coach_mcp._get", new=get_rec), \
             patch("backend.personal_coach_mcp._post", new=post_rec):
            yield get_rec, post_rec

    def test_get_planned_workouts_forwards_range(self, fake_get_post):
        get_rec, _ = fake_get_post
        from backend import personal_coach_mcp as mcp
        asyncio.run(mcp.get_planned_workouts(
            start="2026-05-28", end="2026-06-01"
        ))
        get_rec.assert_called_once_with(
            "/api/planned-workouts",
            start="2026-05-28",
            end="2026-06-01",
        )

    def test_propose_workout_plan_batches_posts(self, fake_get_post):
        _, post_rec = fake_get_post
        from backend import personal_coach_mcp as mcp
        workouts = [
            {"date": "2026-05-28", "type": "easy"},
            {"date": "2026-05-30", "type": "tempo"},
            {"date": "2026-06-01", "type": "long"},
        ]
        r = asyncio.run(mcp.propose_workout_plan(workouts=workouts))
        assert post_rec.call_count == 3
        assert r["n_total"] == 3
        assert r["n_synced"] == 3
        assert r["cal_synced"] is True
        assert r["ok"] is True

    def test_propose_workout_plan_partial_sync_flagged(self):
        """When Cal write succeeds for some workouts but not others,
        cal_synced=false at the aggregate level so the agent can
        warn the user."""
        from backend import personal_coach_mcp as mcp
        responses = [
            {"ok": True, "cal_synced": True,  "planned_workout": {"id": "p1"}},
            {"ok": True, "cal_synced": False, "planned_workout": {"id": "p2"}},
        ]
        with patch(
            "backend.personal_coach_mcp._post",
            new=AsyncMock(side_effect=responses),
        ):
            r = asyncio.run(mcp.propose_workout_plan(workouts=[
                {"date": "2026-05-28", "type": "easy"},
                {"date": "2026-05-30", "type": "tempo"},
            ]))
        assert r["n_synced"] == 1
        assert r["n_total"] == 2
        assert r["cal_synced"] is False

    def test_propose_workout_plan_empty_returns_cal_synced_false(self):
        """Edge case: agent calls with [] — `len == 0` and
        n_synced == 0 should produce cal_synced=false, not the
        ambiguous `0 == 0` true case."""
        from backend import personal_coach_mcp as mcp
        with patch(
            "backend.personal_coach_mcp._post",
            new=AsyncMock(return_value={"ok": True}),
        ):
            r = asyncio.run(mcp.propose_workout_plan(workouts=[]))
        assert r["cal_synced"] is False
        assert r["n_synced"] == 0
        assert r["n_total"] == 0
