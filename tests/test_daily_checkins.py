"""Tests for the PR P3 daily check-in pipeline.

Three concerns:
1. DataProcessor CRUD (file format, upsert semantics, validation).
2. /api/checkins endpoint behavior (4 routes + CME dual-write).
3. MCP tool get_recent_checkins (path + default days).
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from backend.data_processor import DataProcessor


# ---------------------------------------------------------------------------
# DataProcessor CRUD
# ---------------------------------------------------------------------------


@pytest.fixture
def dp(tmp_path):
    """Per-test DataProcessor pointed at an isolated data/ tree."""
    return DataProcessor(data_dir=str(tmp_path / "data"))


class TestCheckinSchemaSetup:
    def test_path_registered(self, dp):
        assert "daily_checkins" in dp.paths
        assert dp.paths["daily_checkins"].endswith(
            "manual_inputs/daily_checkins.json"
        )

    def test_initialized_as_empty_list(self, dp):
        assert os.path.exists(dp.paths["daily_checkins"])
        with open(dp.paths["daily_checkins"]) as f:
            assert json.load(f) == []


class TestCheckinCRUD:
    def test_upsert_creates_new_row(self, dp):
        row = dp.upsert_checkin(
            "2026-05-27",
            sleep_quality=4,
            soreness=2,
            mood=4,
            motivation=5,
            notes="腿有点紧但还行",
        )
        assert row["date"] == "2026-05-27"
        assert row["sleep_quality"] == 4
        assert row["soreness"] == 2
        assert row["mood"] == 4
        assert row["motivation"] == 5
        assert row["notes"] == "腿有点紧但还行"
        assert "created_at" in row
        assert "updated_at" in row

    def test_upsert_same_day_merges_fields(self, dp):
        """Same-date re-submit updates only the fields the caller
        passed — other fields preserved (no full row replacement)."""
        dp.upsert_checkin("2026-05-27", sleep_quality=4, mood=4)
        row = dp.upsert_checkin("2026-05-27", soreness=1, notes="updated")
        assert row["sleep_quality"] == 4  # preserved
        assert row["mood"] == 4  # preserved
        assert row["soreness"] == 1  # new
        assert row["notes"] == "updated"
        # Only one row on disk (not two)
        with open(dp.paths["daily_checkins"]) as f:
            assert len(json.load(f)) == 1

    def test_upsert_bumps_updated_at_not_created_at(self, dp):
        import time
        first = dp.upsert_checkin("2026-05-27", sleep_quality=4)
        time.sleep(0.01)  # ensure ISO strings differ
        second = dp.upsert_checkin("2026-05-27", mood=4)
        assert first["created_at"] == second["created_at"]
        assert second["updated_at"] > first["updated_at"]

    def test_get_by_date_returns_row(self, dp):
        dp.upsert_checkin("2026-05-27", sleep_quality=4)
        row = dp.get_checkin_by_date("2026-05-27")
        assert row is not None
        assert row["sleep_quality"] == 4

    def test_get_by_date_missing_returns_none(self, dp):
        assert dp.get_checkin_by_date("2026-05-27") is None

    def test_list_range_inclusive_and_newest_first(self, dp):
        dp.upsert_checkin("2026-05-25", sleep_quality=5)
        dp.upsert_checkin("2026-05-26", sleep_quality=3)
        dp.upsert_checkin("2026-05-27", sleep_quality=4)
        dp.upsert_checkin("2026-05-28", sleep_quality=2)
        rows = dp.list_checkins_in_range("2026-05-26", "2026-05-27")
        assert [r["date"] for r in rows] == ["2026-05-27", "2026-05-26"]

    def test_list_empty_window_returns_empty(self, dp):
        dp.upsert_checkin("2026-05-25", sleep_quality=5)
        assert dp.list_checkins_in_range("2026-06-01", "2026-06-10") == []

    def test_delete_removes_row(self, dp):
        dp.upsert_checkin("2026-05-27", sleep_quality=4)
        assert dp.delete_checkin("2026-05-27") is True
        assert dp.get_checkin_by_date("2026-05-27") is None

    def test_delete_missing_returns_false(self, dp):
        assert dp.delete_checkin("2026-05-27") is False


class TestCheckinValidation:
    def test_out_of_range_rejected(self, dp):
        with pytest.raises(ValueError, match="out of range"):
            dp.upsert_checkin("2026-05-27", sleep_quality=99)

    def test_non_int_rejected(self, dp):
        with pytest.raises(ValueError, match="must be an int"):
            dp.upsert_checkin("2026-05-27", mood="happy")

    def test_notes_must_be_string(self, dp):
        with pytest.raises(ValueError, match="notes must be a string"):
            dp.upsert_checkin("2026-05-27", notes=123)

    def test_notes_truncated_to_1000_chars(self, dp):
        long_notes = "x" * 2000
        row = dp.upsert_checkin("2026-05-27", notes=long_notes)
        assert len(row["notes"]) == 1000

    def test_soreness_zero_accepted(self, dp):
        """soreness has a 0-5 range (0 = none); other fields are 1-5."""
        row = dp.upsert_checkin("2026-05-27", soreness=0)
        assert row["soreness"] == 0

    def test_partial_payload_allowed(self, dp):
        """User may fill only some sliders. Fields not passed don't
        show up on the row — caller / agent must interpret missing
        as 'didn't capture', not 'zero'."""
        row = dp.upsert_checkin("2026-05-27", sleep_quality=4)
        assert row["sleep_quality"] == 4
        assert "mood" not in row
        assert "soreness" not in row

    def test_upsert_can_clear_existing_notes(self, dp):
        """Codex P2 catch on PR #80. User saves a note, then edits
        the row to remove the note text — the upsert must accept
        notes='' as a clear signal and overwrite the previous value.
        Without this, cleared notes silently reappear after the
        query refetches (field-level merge keeps the old value when
        the request omits notes entirely)."""
        dp.upsert_checkin("2026-05-27", sleep_quality=4, notes="had a thought")
        # User clears the textarea → frontend sends notes=""
        row = dp.upsert_checkin("2026-05-27", notes="")
        assert row["notes"] == ""
        # Sanity: other fields not affected by the clear
        assert row["sleep_quality"] == 4


# ---------------------------------------------------------------------------
# API behavior — exercise the 4 endpoints via TestClient
# ---------------------------------------------------------------------------


class TestCheckinAPI:
    def test_get_list_default_14_days(self, client):
        import backend.api_server as api_server
        api_server.processor.list_checkins_in_range.return_value = [
            {"date": "2026-05-27", "sleep_quality": 4},
        ]
        resp = client.get("/api/checkins")
        assert resp.status_code == 200
        body = resp.json()
        assert body["days"] == 14
        assert body["checkins"][0]["sleep_quality"] == 4

    def test_get_list_custom_days(self, client):
        import backend.api_server as api_server
        api_server.processor.list_checkins_in_range.return_value = []
        resp = client.get("/api/checkins?days=7")
        assert resp.status_code == 200
        assert resp.json()["days"] == 7

    def test_get_list_rejects_zero_or_negative(self, client):
        resp = client.get("/api/checkins?days=0")
        assert resp.status_code == 422

    def test_get_by_date_404_when_missing(self, client):
        import backend.api_server as api_server
        api_server.processor.get_checkin_by_date.return_value = None
        resp = client.get("/api/checkins/2026-05-27")
        assert resp.status_code == 404

    def test_get_by_date_returns_row(self, client):
        import backend.api_server as api_server
        api_server.processor.get_checkin_by_date.return_value = {
            "date": "2026-05-27", "mood": 5,
        }
        resp = client.get("/api/checkins/2026-05-27")
        assert resp.status_code == 200
        assert resp.json()["mood"] == 5

    def test_post_upserts_and_dual_writes_episode(self, client):
        """Successful save dual-writes a 'daily_checkin' episode into
        CME so the agent can search/cluster perceived state later."""
        import backend.api_server as api_server
        api_server.processor.upsert_checkin.return_value = {
            "date": "2026-05-27",
            "sleep_quality": 4,
            "notes": "felt great",
            "updated_at": "2026-05-27T13:00:00Z",
        }
        resp = client.post(
            "/api/checkins",
            json={
                "date": "2026-05-27",
                "sleep_quality": 4,
                "notes": "felt great",
            },
        )
        assert resp.status_code == 200
        # upsert_checkin called with the right kwargs (exclude date,
        # exclude None-valued fields)
        api_server.processor.upsert_checkin.assert_called_once_with(
            "2026-05-27",
            sleep_quality=4,
            notes="felt great",
        )
        # CME episode dual-write fired
        api_server.memory_engine.create_episode.assert_called_once()
        call = api_server.memory_engine.create_episode.call_args
        assert call.kwargs["event_type"] == "daily_checkin"
        assert call.kwargs["context"]["sleep_quality"] == 4
        assert call.kwargs["context"]["notes"] == "felt great"

    def test_post_validation_400(self, client):
        import backend.api_server as api_server
        api_server.processor.upsert_checkin.side_effect = ValueError(
            "sleep_quality=99 out of range [0, 5]"
        )
        resp = client.post(
            "/api/checkins",
            json={"date": "2026-05-27", "sleep_quality": 99},
        )
        assert resp.status_code == 400
        assert "out of range" in resp.json()["detail"]

    def test_post_cme_failure_does_not_500(self, client):
        """A CME write failure must NOT take down the check-in save —
        the JSON file is the canonical store, the episode is a nice
        side effect."""
        import backend.api_server as api_server
        api_server.processor.upsert_checkin.return_value = {
            "date": "2026-05-27", "sleep_quality": 4,
        }
        api_server.memory_engine.create_episode.side_effect = RuntimeError(
            "CME exploded"
        )
        resp = client.post(
            "/api/checkins",
            json={"date": "2026-05-27", "sleep_quality": 4},
        )
        # Still 200 — the CME failure was swallowed
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_forwards_date(self, client):
        import backend.api_server as api_server
        api_server.processor.delete_checkin.return_value = True
        resp = client.delete("/api/checkins/2026-05-27")
        assert resp.status_code == 200
        api_server.processor.delete_checkin.assert_called_once_with(
            "2026-05-27"
        )

    def test_delete_missing_404(self, client):
        import backend.api_server as api_server
        api_server.processor.delete_checkin.return_value = False
        resp = client.delete("/api/checkins/2026-05-27")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------


class TestGetRecentCheckinsMCP:
    """The MCP wrapper just forwards to GET /api/checkins?days=N."""

    @pytest.fixture
    def fake_get(self):
        import asyncio
        from unittest.mock import AsyncMock
        rec = AsyncMock(return_value={"days": 7, "checkins": []})
        with patch("backend.personal_coach_mcp._get", new=rec):
            yield rec, asyncio.run

    def test_default_days_is_7(self, fake_get):
        rec, run = fake_get
        from backend import personal_coach_mcp as mcp
        run(mcp.get_recent_checkins())
        rec.assert_called_once_with("/api/checkins", days=7)

    def test_custom_days_passed_through(self, fake_get):
        rec, run = fake_get
        from backend import personal_coach_mcp as mcp
        run(mcp.get_recent_checkins(days=30))
        rec.assert_called_once_with("/api/checkins", days=30)
