"""External-context events (PR P5 — §4).

Covers:
  • MemoryOS.list_external_events: type filter, date-range overlap,
    fallback to timestamp when context dates missing, ASC ordering.
  • MemoryOS.delete_episode: idempotent on missing rows; removes
    topic links too.
  • POST /api/memory/external-events: validation (event_type whitelist,
    date format, range order, description required).
  • GET /api/memory/external-events: shape + range filter.
  • DELETE /api/memory/external-events/{id}: success + 404 idempotent.
  • MCP tool get_external_events: forwards path + passes through.
  • compute_route_profile: bands sum to total, climb/loss math, max
    grade, no-elev / single-sample / no-distance edge cases.
  • GET /api/runs/{id}/route-profile: 404 when None.
  • MCP get_run_route_profile: success path + missing-data fallback.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# Fixture: fresh MemoryOS
# --------------------------------------------------------------------------


@pytest.fixture
def mem(tmp_path):
    from backend.cognitive_memory_engine import MemoryOS

    return MemoryOS(
        db_path=str(tmp_path / "cme.db"),
        semantic_profile_path=str(tmp_path / "sem.json"),
    )


def _make_event(mem, event_type, start, end, description):
    return mem.create_episode(
        event_type=event_type,
        context={
            "start_date": start,
            "end_date": end,
            "description": description,
        },
        lesson_learned=description,
    )


# --------------------------------------------------------------------------
# MemoryOS layer
# --------------------------------------------------------------------------


class TestListExternalEvents:
    def test_filters_by_type_whitelist(self, mem):
        """Only travel / illness / life_stress surface here; other
        event types (daily_checkin, post_run, etc.) are excluded
        so the agent doesn't get the wrong context flavor."""
        _make_event(mem, "travel", "2026-05-20", "2026-05-22", "TYO trip")
        _make_event(mem, "illness", "2026-05-25", "2026-05-26", "flu")
        _make_event(mem, "life_stress", "2026-05-27", "2026-05-30", "deadline")
        # Decoy: a check-in episode shouldn't show up here.
        mem.create_episode(
            event_type="daily_checkin",
            context={"sleep_quality": 4, "mood": 3},
        )
        events = mem.list_external_events("2026-05-01", "2026-06-01")
        types = {e["event_type"] for e in events}
        assert types == {"travel", "illness", "life_stress"}

    def test_range_overlap_inclusive_both_ends(self, mem):
        """Event overlaps when event.start_date <= range.end AND
        event.end_date >= range.start. Pin both inclusive edges."""
        _make_event(mem, "illness", "2026-05-10", "2026-05-12", "bug")
        # Range ending exactly on event start_date — must include.
        evs = mem.list_external_events("2026-05-01", "2026-05-10")
        assert len(evs) == 1
        # Range starting exactly on event end_date — must include.
        evs = mem.list_external_events("2026-05-12", "2026-05-20")
        assert len(evs) == 1
        # Range strictly before — exclude.
        evs = mem.list_external_events("2026-05-01", "2026-05-09")
        assert evs == []
        # Range strictly after — exclude.
        evs = mem.list_external_events("2026-05-13", "2026-05-20")
        assert evs == []

    def test_falls_back_to_timestamp_when_context_dates_missing(self, mem):
        """Legacy / partially-filled rows may not have
        context.start_date — derive from the episode's timestamp
        column so they still surface (graceful upgrade)."""
        mem.create_episode(
            event_type="travel",
            context={"description": "vague legacy row"},
            timestamp="2026-05-15T12:00:00+00:00",
        )
        evs = mem.list_external_events("2026-05-14", "2026-05-16")
        assert len(evs) == 1
        # Derived range matches the timestamp date.
        assert evs[0]["start_date"] == "2026-05-15"
        assert evs[0]["end_date"] == "2026-05-15"

    def test_orders_earliest_first(self, mem):
        """Agent reads the timeline forward; ASC by start_date so
        the model sees events in causal order."""
        _make_event(mem, "travel", "2026-05-22", "2026-05-25", "C")
        _make_event(mem, "illness", "2026-05-10", "2026-05-12", "A")
        _make_event(mem, "life_stress", "2026-05-15", "2026-05-20", "B")
        evs = mem.list_external_events("2026-05-01", "2026-06-01")
        dates = [e["start_date"] for e in evs]
        assert dates == ["2026-05-10", "2026-05-15", "2026-05-22"]


class TestDeleteEpisode:
    def test_removes_row(self, mem):
        eid = _make_event(mem, "travel", "2026-05-10", "2026-05-11", "x")
        assert mem.delete_episode(eid) is True
        assert mem.list_episodes(event_type="travel") == []

    def test_missing_id_returns_false_idempotent(self, mem):
        """UI may re-fire DELETE on a re-click; idempotent return
        prevents a confusing 'already gone' error in the toast."""
        assert mem.delete_episode("epi_nonexistent") is False

    def test_cleans_up_topic_links(self, mem):
        """Topic links FK back to episode_id. Make sure deleting an
        episode also clears the dangling link rows (otherwise
        get_topic_episodes returns a topic_id with no episode)."""
        # Use create_episode to wire a link.
        eid = mem.create_episode(
            event_type="travel",
            context={"description": "x"},
            related_topic_ids=["topic_fake"],
        )
        # link row exists
        before = mem.conn.execute(
            "SELECT COUNT(*) FROM topic_episode_links WHERE episode_id = ?",
            (eid,),
        ).fetchone()[0]
        assert before == 1
        mem.delete_episode(eid)
        after = mem.conn.execute(
            "SELECT COUNT(*) FROM topic_episode_links WHERE episode_id = ?",
            (eid,),
        ).fetchone()[0]
        assert after == 0


# --------------------------------------------------------------------------
# /api/memory/external-events endpoints
# --------------------------------------------------------------------------


class TestExternalEventsAPI:
    def test_create_happy_path(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.create_episode.return_value = "epi_1"
        resp = client.post(
            "/api/memory/external-events",
            json={
                "event_type": "travel",
                "start_date": "2026-05-20",
                "end_date": "2026-05-22",
                "description": "Tokyo",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["episode_id"] == "epi_1"
        # Verify the context dict shape we hand the engine.
        kwargs = api_server.memory_engine.create_episode.call_args.kwargs
        assert kwargs["event_type"] == "travel"
        assert kwargs["context"]["start_date"] == "2026-05-20"
        assert kwargs["context"]["end_date"] == "2026-05-22"
        assert kwargs["context"]["description"] == "Tokyo"
        # lesson_learned mirrors description for keyword-search reach.
        assert kwargs["lesson_learned"] == "Tokyo"

    def test_create_rejects_unknown_event_type(self, client):
        """Whitelist is the source of truth on the server. A renamed/
        invented event_type from the UI shouldn't quietly land as a
        new bucket the agent can't query."""
        resp = client.post(
            "/api/memory/external-events",
            json={
                "event_type": "vacation",  # not in whitelist
                "start_date": "2026-05-20", "end_date": "2026-05-21",
                "description": "x",
            },
        )
        assert resp.status_code == 400
        assert "event_type" in resp.json()["detail"]

    def test_create_rejects_bad_date_format(self, client):
        resp = client.post(
            "/api/memory/external-events",
            json={
                "event_type": "travel",
                "start_date": "2026/05/20",  # slashes, not dashes
                "end_date": "2026-05-22",
                "description": "x",
            },
        )
        assert resp.status_code == 400

    def test_create_rejects_inverted_range(self, client):
        resp = client.post(
            "/api/memory/external-events",
            json={
                "event_type": "travel",
                "start_date": "2026-05-22",
                "end_date": "2026-05-20",
                "description": "x",
            },
        )
        assert resp.status_code == 400
        assert "end_date" in resp.json()["detail"]

    def test_create_rejects_empty_description(self, client):
        """Description is the only field the agent gets to read.
        Empty = the row is just noise."""
        resp = client.post(
            "/api/memory/external-events",
            json={
                "event_type": "travel",
                "start_date": "2026-05-20", "end_date": "2026-05-21",
                "description": "   ",
            },
        )
        assert resp.status_code == 400

    def test_list_forwards_range_and_returns_shape(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.list_external_events.return_value = [
            {
                "episode_id": "epi_1",
                "event_type": "travel",
                "start_date": "2026-05-20",
                "end_date": "2026-05-22",
                "context": {"description": "x"},
            }
        ]
        resp = client.get(
            "/api/memory/external-events?start=2026-05-01&end=2026-05-31"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["start"] == "2026-05-01"
        assert body["end"] == "2026-05-31"
        assert body["events"][0]["episode_id"] == "epi_1"
        api_server.memory_engine.list_external_events.assert_called_once_with(
            "2026-05-01", "2026-05-31"
        )

    def test_delete_returns_removed_flag(self, client):
        import backend.api_server as api_server
        api_server.memory_engine.delete_episode.return_value = True
        resp = client.delete("/api/memory/external-events/epi_1")
        assert resp.status_code == 200
        assert resp.json()["removed"] is True

    def test_delete_idempotent_when_missing(self, client):
        """re-click delete → second DELETE should still 200 with
        removed=false. UI doesn't need to special-case the second
        click."""
        import backend.api_server as api_server
        api_server.memory_engine.delete_episode.return_value = False
        resp = client.delete("/api/memory/external-events/epi_gone")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert resp.json()["removed"] is False


# --------------------------------------------------------------------------
# MCP tool
# --------------------------------------------------------------------------


class TestExternalEventsMCP:
    def test_get_external_events_forwards_range_and_passes_through(self):
        from backend import personal_coach_mcp as mcp
        backend_payload = {
            "start": "2026-05-01",
            "end": "2026-05-31",
            "events": [{"episode_id": "epi_1", "event_type": "travel"}],
        }
        with patch(
            "backend.personal_coach_mcp._get",
            new=AsyncMock(return_value=backend_payload),
        ) as get_rec:
            result = asyncio.run(
                mcp.get_external_events(start="2026-05-01", end="2026-05-31")
            )
        get_rec.assert_called_once_with(
            "/api/memory/external-events",
            start="2026-05-01",
            end="2026-05-31",
        )
        # Pass-through pin — agent sees exactly what the backend
        # returned, no wrapping.
        assert result == backend_payload


# --------------------------------------------------------------------------
# Route profile (compute_route_profile + endpoint + MCP)
# --------------------------------------------------------------------------


def _write_telemetry(tmp_path, activity_id, samples):
    """Write a minimal Garmin-format detail JSON for the processor
    to read. `samples` is a list of (elevation_m, sum_dist_m) tuples.
    DataProcessor reads details from `{data_dir}/get_activity_details/`."""
    details_dir = tmp_path / "data" / "get_activity_details"
    details_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "metricDescriptors": [
            {"key": "directElevation", "metricsIndex": 0},
            {"key": "sumDistance", "metricsIndex": 1},
        ],
        "activityDetailMetrics": [
            {"metrics": [elev, dist]} for elev, dist in samples
        ],
    }
    (details_dir / f"{activity_id}.json").write_text(json.dumps(payload))


class TestComputeRouteProfile:
    @pytest.fixture
    def dp(self, tmp_path):
        # Real DataProcessor reading from tmp paths — _write_telemetry
        # lays JSON files matching the processor's expected layout.
        from backend.data_processor import DataProcessor
        return DataProcessor(data_dir=str(tmp_path / "data"))

    def test_missing_file_returns_none(self, dp):
        assert dp.compute_route_profile(activity_id=99999) is None

    def test_pure_flat_run(self, dp, tmp_path):
        """No elevation change — should produce all distance in the
        flat band, 0 gain / 0 loss."""
        # 5 km, flat: samples at 0/500/1000/.../5000 m, elev 100 throughout
        samples = [(100.0, d) for d in range(0, 5001, 500)]
        _write_telemetry(tmp_path, 1, samples)
        prof = dp.compute_route_profile(1)
        assert prof is not None
        # Distance bands sum to total
        band_dist = sum(b["distance_mi"] for b in prof["grade_distribution"])
        assert band_dist == pytest.approx(prof["total_distance_mi"], abs=0.05)
        flat = next(
            b for b in prof["grade_distribution"] if b["band"] == "flat"
        )
        assert flat["pct"] == 100.0
        assert prof["elevation_gain_ft"] == 0
        assert prof["elevation_loss_ft"] == 0
        assert prof["net_climb_ft"] == 0

    def test_uphill_then_downhill(self, dp, tmp_path):
        """Symmetric out-and-back hill: 1 mile up at 4% grade
        (rolling_up), 1 mile down at -4% grade (rolling_down). Bands
        50/50, gain ≈ loss, net_climb ≈ 0."""
        # 1 mile ≈ 1609 m; 4% grade over 1609 m = 64.4 m climb.
        # Step in 100 m increments for resolution.
        up = [(0.04 * d, d) for d in range(0, 1610, 100)]
        # Descent: each step is +100 m in distance, -4 m in elev.
        last_elev = up[-1][0]
        down = [
            (last_elev - 0.04 * (d - 1600), d)
            for d in range(1700, 3210, 100)
        ]
        _write_telemetry(tmp_path, 2, up + down)
        prof = dp.compute_route_profile(2)
        assert prof is not None
        # Net climb close to zero
        assert abs(prof["net_climb_ft"]) < 10
        # Gain and loss roughly equal (each ~211 ft for 64.4 m)
        assert prof["elevation_gain_ft"] == pytest.approx(
            prof["elevation_loss_ft"], abs=15
        )
        # Most of the time in the rolling bands, not flat
        bands = {b["band"]: b["pct"] for b in prof["grade_distribution"]}
        assert bands["rolling_up"] + bands["rolling_down"] > 80

    def test_single_sample_returns_none(self, dp, tmp_path):
        """Need at least 2 samples to compute any delta. One sample
        → can't infer grade, return None rather than 0-pct everything."""
        _write_telemetry(tmp_path, 3, [(100.0, 0)])
        assert dp.compute_route_profile(3) is None

    def test_missing_elevation_descriptor_returns_none(self, dp, tmp_path):
        """Some treadmill / pool activities have no directElevation
        metric in the descriptor list. Bail out cleanly."""
        details_dir = tmp_path / "data" / "activity_details"
        details_dir.mkdir(parents=True, exist_ok=True)
        (details_dir / "4.json").write_text(json.dumps({
            "metricDescriptors": [
                {"key": "sumDistance", "metricsIndex": 0},
            ],
            "activityDetailMetrics": [
                {"metrics": [0]}, {"metrics": [500]},
            ],
        }))
        assert dp.compute_route_profile(4) is None

    def test_grade_max_min_realistic(self, dp, tmp_path):
        """Sharp climb spike → max_grade reflects the steepest
        segment, not the average. (Used in agent reasoning: "with a
        max of 12% in there, that surge effort makes sense.")"""
        samples = [
            (100, 0), (100, 100), (100, 200),     # flat
            (108, 300),                            # 8% spike
            (108, 400), (108, 500),                # flat
            (104, 600),                            # -4% descent
        ]
        _write_telemetry(tmp_path, 5, samples)
        prof = dp.compute_route_profile(5)
        assert prof is not None
        assert prof["max_grade_pct"] == pytest.approx(8.0, abs=0.5)
        assert prof["min_grade_pct"] == pytest.approx(-4.0, abs=0.5)


class TestRouteProfileEndpoint:
    def test_404_when_unavailable(self, client):
        import backend.api_server as api_server
        api_server.processor.compute_route_profile.return_value = None
        resp = client.get("/api/runs/1234/route-profile")
        assert resp.status_code == 404

    def test_200_passes_through_processor_shape(self, client):
        import backend.api_server as api_server
        payload = {
            "activity_id": 1234,
            "total_distance_mi": 5.0,
            "elevation_gain_ft": 200,
            "elevation_loss_ft": 195,
            "net_climb_ft": 5,
            "max_grade_pct": 6.2,
            "min_grade_pct": -5.8,
            "grade_distribution": [
                {"band": "flat", "range": "-2% to 2%",
                 "distance_mi": 4.0, "pct": 80.0},
            ],
        }
        api_server.processor.compute_route_profile.return_value = payload
        resp = client.get("/api/runs/1234/route-profile")
        assert resp.status_code == 200
        assert resp.json() == payload


class TestRouteProfileMCP:
    def test_forwards_path_and_returns_payload(self):
        from backend import personal_coach_mcp as mcp
        backend_payload = {
            "activity_id": 1234,
            "total_distance_mi": 5.0,
            "elevation_gain_ft": 200,
            "elevation_loss_ft": 195,
            "net_climb_ft": 5,
            "max_grade_pct": 6.2,
            "min_grade_pct": -5.8,
            "grade_distribution": [],
        }
        with patch(
            "backend.personal_coach_mcp._get",
            new=AsyncMock(return_value=backend_payload),
        ) as get_rec:
            result = asyncio.run(mcp.get_run_route_profile(activity_id=1234))
        get_rec.assert_called_once_with("/api/runs/1234/route-profile")
        assert result == backend_payload

    def test_returns_safe_empty_dict_on_404(self):
        """404 (no GPS / not synced) shouldn't make the agent crash —
        return a stable shape with zeros + a note. Callers branch on
        total_distance_mi > 0."""
        import httpx
        from backend import personal_coach_mcp as mcp

        async def boom(*args, **kwargs):
            raise httpx.HTTPStatusError(
                "404", request=None, response=None,  # type: ignore[arg-type]
            )

        with patch("backend.personal_coach_mcp._get", new=AsyncMock(side_effect=boom)):
            result = asyncio.run(mcp.get_run_route_profile(activity_id=9999))
        assert result["total_distance_mi"] == 0
        assert result["grade_distribution"] == []
        assert "note" in result
