"""Unit tests for backend/personal_coach_mcp.py.

Every tool is a thin async HTTP wrapper around api_server. The contract
is:
  • path: which endpoint the tool calls
  • params: how tool args map to query-string args
  • shape: how the tool projects / transforms the api response

We mock `_get` at the module boundary and capture (path, params) per
call, so each test asserts the contract without standing up FastAPI or
httpx. Pure-function helpers (`_pace_str_from_dec`, `_format_duration`,
`_split_pace_dec`, `_zones_time_min`) get direct sync coverage.

Tools are async; the rest of the suite is sync. We bridge with
`asyncio.run(tool(...))` so we don't need to introduce pytest-asyncio
as a dep just for this file.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from backend import personal_coach_mcp as mcp


# ---------------------------------------------------------------------------
# Pure-helper unit tests (no async)
# ---------------------------------------------------------------------------


class TestPaceStrFromDec:
    def test_none(self):
        assert mcp._pace_str_from_dec(None) is None

    def test_zero(self):
        assert mcp._pace_str_from_dec(0) is None

    def test_negative(self):
        assert mcp._pace_str_from_dec(-3.5) is None

    def test_integer_minute(self):
        # 9 min flat → "9:00"
        assert mcp._pace_str_from_dec(9.0) == "9:00"

    def test_half_minute(self):
        # 7.5 = 7:30
        assert mcp._pace_str_from_dec(7.5) == "7:30"

    def test_truncates_seconds(self):
        # 8.42 min = 8 min 25.2 s → "8:25" (int seconds, truncated)
        assert mcp._pace_str_from_dec(8.42) == "8:25"

    def test_zero_padding_when_seconds_under_ten(self):
        # 8.5 → 8 min 30 s → "8:30" (proves the :02d zero-pad runs for
        # all values, even though 30 needs no padding — picking a
        # cleanly-representable float to avoid IEEE-754 surprises like
        # 8.1 → 8.099999... where int() truncates to 5 instead of 6).
        assert mcp._pace_str_from_dec(8.5) == "8:30"
        # Sanity: small-second value still gets zero-padded.
        assert mcp._pace_str_from_dec(8.05) == "8:03"


class TestFormatDuration:
    def test_none(self):
        assert mcp._format_duration(None) is None

    def test_zero(self):
        assert mcp._format_duration(0) is None

    def test_negative(self):
        assert mcp._format_duration(-5) is None

    def test_under_one_hour(self):
        assert mcp._format_duration(125) == "2:05"  # 2 min 5 s

    def test_over_one_hour(self):
        assert mcp._format_duration(3725) == "1:02:05"  # 1h 2m 5s

    def test_rounds_subsecond(self):
        # 124.6 should round to 125 → "2:05"
        assert mcp._format_duration(124.6) == "2:05"


class TestSplitPaceDec:
    def test_zero_distance(self):
        assert mcp._split_pace_dec(0, 600) is None

    def test_zero_duration(self):
        assert mcp._split_pace_dec(1609, 0) is None

    def test_one_mile_six_minutes(self):
        # 1609.34 m in 360 s = 6.00 min/mi
        result = mcp._split_pace_dec(1609.34, 360)
        assert result == 6.0

    def test_rounding_to_two_decimals(self):
        # 1 km = 0.6214 mi; in 300s = 5 min total → 8.04 min/mi
        result = mcp._split_pace_dec(1000, 300)
        assert isinstance(result, float)
        # round(300/60 / (1000/1609.34), 2) = round(5/0.6214, 2) ≈ 8.05
        assert 8.0 < result < 8.1


class TestZonesTimeMin:
    def test_empty_telemetry(self):
        zones = [
            {"name": "Hold Back", "rpe_label": "Hold Back Easy", "low": 0, "high": 144},
        ]
        assert mcp._zones_time_min([], zones) == []

    def test_empty_zones(self):
        rows = [{"HeartRate": 150}, {"HeartRate": 160}]
        assert mcp._zones_time_min(rows, []) == []

    def test_distributes_seconds_by_zone(self):
        """Each row is 1 second of telemetry. 4 rows = 4 sec total."""
        zones = [
            {"name": "Easy",  "rpe_label": "Hold Back Easy",  "low": 0, "high": 144},
            {"name": "Steady", "rpe_label": "Steady Effort", "low": 145, "high": 162},
        ]
        rows = [
            {"HeartRate": 130},  # Easy
            {"HeartRate": 140},  # Easy
            {"HeartRate": 150},  # Steady
            {"HeartRate": 160},  # Steady
        ]
        result = mcp._zones_time_min(rows, zones)
        # 2 sec each → 2/60 = 0.033 → rounded to 0.0
        # pct: 2/4*100 = 50%
        assert len(result) == 2
        assert result[0]["rpe_label"] == "Hold Back Easy"
        assert result[0]["pct"] == 50.0
        assert result[1]["rpe_label"] == "Steady Effort"
        assert result[1]["pct"] == 50.0

    def test_drops_rows_with_no_hr(self):
        zones = [{"name": "Easy", "rpe_label": "Hold Back Easy", "low": 0, "high": 144}]
        rows = [
            {"HeartRate": 130},
            {"HeartRate": None},
            {"HeartRate": 0},
            {"HeartRate": -5},
            {"HeartRate": 140},
        ]
        result = mcp._zones_time_min(rows, zones)
        # Only 2 rows counted → 2 sec → 100% of total
        assert result[0]["pct"] == 100.0


# ---------------------------------------------------------------------------
# Async tool tests — mock _get and capture (path, params)
# ---------------------------------------------------------------------------


class _GetRecorder:
    """Replacement for `_get` that records every call + returns canned
    responses in order. Anything past the canned list returns {}."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, path, **params):
        self.calls.append((path, dict(params)))
        if self.responses:
            return self.responses.pop(0)
        return {}


def _run(coro):
    """Sync-friendly runner for our async tool calls."""
    return asyncio.run(coro)


@pytest.fixture
def fake_get():
    """Returns a `(recorder, patcher_cm)` tuple. Caller seeds responses
    via `recorder.responses.append(...)` or via constructor."""
    rec = _GetRecorder()
    with patch("backend.personal_coach_mcp._get", new=rec):
        yield rec


# ---- Section 1: profile + recent state -------------------------------------


class TestGetAthleteProfile:
    def test_calls_athlete_profile_path(self, fake_get):
        fake_get.responses.append({"athlete": {"age": 30}})
        result = _run(mcp.get_athlete_profile())
        assert fake_get.calls == [("/api/athlete/profile", {})]
        # Non-fitness fields pass through untouched.
        assert result["athlete"] == {"age": 30}

    def test_renames_hr_zones_to_medium_term_hr_effort_map(self, fake_get):
        """Per §2 of the reorg: the agent-facing key for "the user's
        medium-term HR↔effort mapping" is explicitly named so the
        prompt doesn't have to keep clarifying that hr_zones is one
        of the perceived layers."""
        fake_get.responses.append({
            "athlete": {},
            "fitness": {
                "vo2max_running": 50,
                "hr_zones": [
                    {"name": "Steady", "rpe_label": "Steady Effort",
                     "low": 145, "high": 162},
                ],
            },
        })
        result = _run(mcp.get_athlete_profile())
        fit = result["fitness"]
        # New name present, old name gone.
        assert "medium_term_hr_effort_map" in fit
        assert "hr_zones" not in fit
        # Content unchanged — just a rename.
        assert fit["medium_term_hr_effort_map"] == [
            {"name": "Steady", "rpe_label": "Steady Effort",
             "low": 145, "high": 162},
        ]
        # Other fitness fields untouched.
        assert fit["vo2max_running"] == 50

    def test_missing_hr_zones_no_error(self, fake_get):
        """If the underlying profile has no hr_zones, the projection
        should still produce a valid response (no spurious key)."""
        fake_get.responses.append({"athlete": {}, "fitness": {}})
        result = _run(mcp.get_athlete_profile())
        assert "medium_term_hr_effort_map" not in result["fitness"]
        assert "hr_zones" not in result["fitness"]


class TestGetReadiness:
    def test_default_date_passed_as_none(self, fake_get):
        fake_get.responses.append({"status": "Green"})
        _run(mcp.get_readiness())
        # _get receives date=None; it will be stripped by _get itself,
        # but the tool's job is just to forward the kwarg.
        assert fake_get.calls == [("/api/health/readiness", {"date": None})]

    def test_explicit_date_forwarded(self, fake_get):
        fake_get.responses.append({"status": "Yellow"})
        _run(mcp.get_readiness(date="2026-05-12"))
        assert fake_get.calls == [
            ("/api/health/readiness", {"date": "2026-05-12"})
        ]


class TestGetTrainingLoad:
    def test_default_window(self, fake_get):
        fake_get.responses.append({"acwr": 1.07})
        _run(mcp.get_training_load())
        assert fake_get.calls == [("/api/training/load", {"window": 28})]

    def test_custom_window(self, fake_get):
        fake_get.responses.append({})
        _run(mcp.get_training_load(window_days=7))
        assert fake_get.calls == [("/api/training/load", {"window": 7})]


# ---- Section 3: runs --------------------------------------------------------


class TestListRuns:
    def test_passes_window_and_trims_summary(self, fake_get):
        fake_get.responses.append(
            {
                "start": "2026-05-01",
                "end": "2026-05-08",
                "runs": [
                    # Realistic-ish raw run; _trim_run_summary projects it.
                    {
                        "activityId": 12345,
                        "activityName": "Weehawken Run",
                        "startTimeLocal": "2026-05-05T08:00:00",
                        "distance": 5000,
                        "duration": 1800,
                        "averageHR": 150,
                        "manual_meta": {"category_stats": []},
                    }
                ],
            }
        )
        result = _run(mcp.list_runs("2026-05-01", "2026-05-08"))
        assert fake_get.calls == [
            ("/api/runs", {"start": "2026-05-01", "end": "2026-05-08"})
        ]
        assert result["start"] == "2026-05-01"
        assert result["end"] == "2026-05-08"
        assert len(result["runs"]) == 1
        # _trim_run_summary returns the curated three-stream shape.
        trimmed = result["runs"][0]
        assert "objective" in trimmed
        assert "perceived" in trimmed
        assert "planned" in trimmed

    def test_empty_runs_list(self, fake_get):
        fake_get.responses.append({"start": "x", "end": "y", "runs": []})
        result = _run(mcp.list_runs("x", "y"))
        assert result["runs"] == []

    def test_objective_block_excludes_garmin_interpretive_labels(self, fake_get):
        """§2 contract: even if the api dumps aerobicTrainingEffect /
        anaerobicTrainingEffect / activityTrainingLoad /
        trainingEffectLabel in the raw run, the MCP projection drops
        them so the agent never sees Garmin's derived guesses."""
        fake_get.responses.append({
            "start": "x", "end": "y",
            "runs": [{
                "activityId": 1, "activityName": "x",
                "startTimeLocal": "2026-05-05T08:00:00",
                "distance": 5000, "duration": 1800,
                "averageHR": 150, "maxHR": 170,
                # Garmin interpretive fields — should be filtered out.
                "aerobicTrainingEffect": 4.5,
                "anaerobicTrainingEffect": 1.2,
                "activityTrainingLoad": 220,
                "trainingEffectLabel": "TEMPO",
                "manual_meta": {"category_stats": []},
            }],
        })
        result = _run(mcp.list_runs("x", "y"))
        obj = result["runs"][0]["objective"]
        # What stays: raw HR.
        assert obj["avg_hr"] == 150
        assert obj["max_hr"] == 170
        # What's filtered:
        for forbidden in (
            "training_effect_aerobic", "training_effect_anaerobic",
            "training_load", "garmin_label",
        ):
            assert forbidden not in obj, (
                f"{forbidden} should be filtered at the MCP boundary"
            )


class TestGetRunDetail:
    def test_makes_expected_api_calls_in_order(self, fake_get):
        """get_run_detail composes 5 reads to inline weather + zones +
        telemetry-derived drift. Locks in the call ordering so future
        refactors don't accidentally drop one."""
        fake_get.responses.extend([
            # 1. /api/runs/{id}
            {
                "run": {
                    "activityId": 999,
                    "activityName": "Test",
                    "startTimeLocal": "2026-05-05T08:00:00",
                    "distance": 5000,
                    "duration": 1800,
                    "averageHR": 150,
                    "manual_meta": {"category_stats": []},
                },
                "chat_history": [],
            },
            # 2. /api/runs/{id}/laps
            {"laps": [], "meta": {"category_stats": []}},
            # 3. /api/runs/{id}/weather
            {
                "temperature_f": 60, "apparent_temperature_f": 55,
                "humidity_pct": 70, "dew_point_f": 45, "wind_mph": 8,
                "source": "openweather", "fetched_at": "2026-05-05T08:00:00Z",
            },
            # 4. /api/athlete/profile (for hr_zones)
            {"fitness": {"hr_zones": []}},
            # 5. /api/runs/{id}/telemetry (for zone-time + drift)
            {"raw": [], "summary": {}},
        ])
        result = _run(mcp.get_run_detail(activity_id=999))
        paths = [c[0] for c in fake_get.calls]
        assert paths == [
            "/api/runs/999",
            "/api/runs/999/laps",
            "/api/runs/999/weather",
            "/api/athlete/profile",
            "/api/runs/999/telemetry",
        ]
        # Telemetry call passes downsample_sec=10 (high resolution for
        # drift; the standalone get_run_telemetry tool defaults to 30).
        tel_call = fake_get.calls[4]
        assert tel_call[1] == {"downsample_sec": 10}
        # Result includes the activity id (curated key name: `id`) and
        # the three-stream shape the agent prompt instructions rely on.
        assert result.get("id") == 999
        assert "objective" in result
        assert "perceived" in result
        assert "planned" in result

    def test_objective_drops_training_effect_block(self, fake_get):
        """§2 contract: get_run_detail's `objective` block does NOT
        ship Garmin's training_effect sub-dict, even though the raw
        api response carries it."""
        fake_get.responses.extend([
            {  # /api/runs/{id}
                "run": {
                    "activityId": 1, "activityName": "x",
                    "startTimeLocal": "2026-05-05T08:00:00",
                    "distance": 5000, "duration": 1800,
                    "averageHR": 150, "maxHR": 170,
                    # Interpretive fields the projection should drop:
                    "aerobicTrainingEffect": 4.5,
                    "anaerobicTrainingEffect": 1.2,
                    "activityTrainingLoad": 220,
                    "trainingEffectLabel": "TEMPO",
                    "aerobicTrainingEffectMessage": "Highly impacting tempo run",
                    "manual_meta": {"category_stats": []},
                },
                "chat_history": [],
            },
            {"laps": [], "meta": {"category_stats": []}},
            {  # weather
                "temperature_f": 60, "apparent_temperature_f": 55,
                "humidity_pct": 70, "dew_point_f": 45, "wind_mph": 8,
            },
            {"fitness": {"hr_zones": []}},
            {"raw": [], "summary": {}},
        ])
        result = _run(mcp.get_run_detail(activity_id=1))
        obj = result["objective"]
        # Heart rate + drift + power + form + splits still present.
        assert "heart_rate" in obj
        assert "drift" in obj
        # Whole interpretive block is gone.
        assert "training_effect" not in obj


class TestGetRunWeather:
    def test_projects_weather_shape(self, fake_get):
        fake_get.responses.append({
            "temperature_f": 60,
            "apparent_temperature_f": 55,
            "humidity_pct": 70,
            "dew_point_f": 45,
            "wind_mph": 8,
            "source": "openweather",
            "fetched_at": "2026-05-05T08:00:00Z",
        })
        result = _run(mcp.get_run_weather(activity_id=42))
        assert fake_get.calls == [("/api/runs/42/weather", {})]
        # The tool renames keys: temperature_f → temp_f, etc.
        assert result["activity_id"] == 42
        assert result["temp_f"] == 60
        assert result["feels_like_f"] == 55
        assert result["humidity_pct"] == 70
        assert result["dew_point_f"] == 45
        assert result["wind_mph"] == 8


class TestGetRunTelemetry:
    def test_default_downsample_30(self, fake_get):
        fake_get.responses.append({"raw": [], "summary": {}, "ai": []})
        _run(mcp.get_run_telemetry(activity_id=7))
        assert fake_get.calls == [
            ("/api/runs/7/telemetry", {"downsample_sec": 30})
        ]

    def test_empty_telemetry_returns_empty_buckets(self, fake_get):
        fake_get.responses.append({"raw": [], "summary": {}, "ai": []})
        result = _run(mcp.get_run_telemetry(activity_id=7))
        assert result["activity_id"] == 7
        assert result["downsample_sec"] == 30
        assert result["total_buckets"] == 0
        assert result["buckets"] == []
        assert result["summary"]["heart_rate"] is None
        # pace dict is always present, with all dec/str nulls
        assert result["summary"]["pace"]["avg_dec"] is None
        assert result["summary"]["pace"]["avg_str"] is None


# ---- Section 4: blocks / cycle / monthly -----------------------------------


class TestListBlocks:
    def test_path(self, fake_get):
        fake_get.responses.append({"blocks": [], "active_block_id": None})
        _run(mcp.list_blocks())
        assert fake_get.calls == [("/api/training/blocks", {})]


class TestGetCycleStats:
    def test_forwards_all_three_params(self, fake_get):
        fake_get.responses.append({})
        _run(mcp.get_cycle_stats(
            block_id="block_001",
            week_start="2026-05-01",
            week_end="2026-05-08",
        ))
        assert fake_get.calls == [(
            "/api/training/cycle-stats",
            {
                "block_id": "block_001",
                "week_start": "2026-05-01",
                "week_end": "2026-05-08",
            },
        )]


class TestGetMonthlyStats:
    def test_default_activity_type_running(self, fake_get):
        fake_get.responses.append({"months": []})
        _run(mcp.get_monthly_stats())
        assert fake_get.calls == [
            ("/api/training/monthly-stats", {"activity_type": "running"})
        ]

    def test_custom_activity_type(self, fake_get):
        fake_get.responses.append({})
        _run(mcp.get_monthly_stats(activity_type="all"))
        assert fake_get.calls == [
            ("/api/training/monthly-stats", {"activity_type": "all"})
        ]


# ---- Section 5: manual activities ------------------------------------------


class TestListManualActivities:
    def test_path_with_window(self, fake_get):
        fake_get.responses.append({"activities": []})
        _run(mcp.list_manual_activities("2026-05-01", "2026-05-08"))
        assert fake_get.calls == [(
            "/api/manual-activities",
            {"start": "2026-05-01", "end": "2026-05-08"},
        )]


class TestGetManualActivity:
    def test_path_includes_id(self, fake_get):
        fake_get.responses.append({"id": "abc"})
        _run(mcp.get_manual_activity(activity_id="abc"))
        assert fake_get.calls == [("/api/manual-activities/abc", {})]


# ---- Section 6: calendar / workout-plan ------------------------------------


class TestGetCalendarEvents:
    def test_passes_window(self, fake_get):
        fake_get.responses.append({"events": []})
        _run(mcp.get_calendar_events(
            start="2026-05-01T00:00:00",
            end="2026-05-08T00:00:00",
        ))
        assert fake_get.calls == [(
            "/api/calendar/events",
            {"start": "2026-05-01T00:00:00", "end": "2026-05-08T00:00:00"},
        )]


class TestGetWorkoutPlan:
    """Phase 1 stub — always returns {date, planned: None}. The test
    locks in that contract so the agent's downstream prompt can rely
    on the shape, and makes the Phase 2 implementation cost more
    obvious (this test must change when we wire real planning)."""

    def test_returns_stable_shape_with_null_planned(self):
        # No mock needed — pure local function, no _get call.
        result = _run(mcp.get_workout_plan(date="2026-05-12"))
        assert result == {"date": "2026-05-12", "planned": None}

    def test_does_not_hit_api(self, fake_get):
        _run(mcp.get_workout_plan(date="2026-05-12"))
        assert fake_get.calls == []


# ---- Section 7: CME (recall / search / pending) ----------------------------


class TestRecallTopics:
    """4 branches: active / all / resolved / conflicting. `active` and
    `all` go to the unfiltered endpoint and filter client-side; the
    others pass `status=<Capitalized>` as a query param."""

    def test_active_filters_to_open_and_testing(self, fake_get):
        fake_get.responses.append({
            "topics": [
                {"topic_id": "tpc_a", "status": "Open"},
                {"topic_id": "tpc_b", "status": "Testing"},
                {"topic_id": "tpc_c", "status": "Resolved"},
                {"topic_id": "tpc_d", "status": "Conflicting"},
            ]
        })
        result = _run(mcp.recall_topics(status="active"))
        # Endpoint hit: no status filter (we filter client-side)
        assert fake_get.calls == [("/api/memory/topics", {})]
        # Kept only Open + Testing
        kept_ids = [t["topic_id"] for t in result["topics"]]
        assert kept_ids == ["tpc_a", "tpc_b"]
        assert result["filter"] == "active"

    def test_all_returns_unfiltered(self, fake_get):
        fake_get.responses.append({"topics": [{"id": 1}, {"id": 2}]})
        result = _run(mcp.recall_topics(status="all"))
        assert fake_get.calls == [("/api/memory/topics", {})]
        assert len(result["topics"]) == 2
        assert result["filter"] == "all"

    def test_resolved_passes_capitalized_status(self, fake_get):
        fake_get.responses.append({"topics": []})
        _run(mcp.recall_topics(status="resolved"))
        assert fake_get.calls == [
            ("/api/memory/topics", {"status": "Resolved"})
        ]

    def test_conflicting_passes_capitalized_status(self, fake_get):
        fake_get.responses.append({"topics": []})
        _run(mcp.recall_topics(status="conflicting"))
        assert fake_get.calls == [
            ("/api/memory/topics", {"status": "Conflicting"})
        ]


class TestSearchEpisodes:
    def test_joins_keywords_into_query_string(self, fake_get):
        fake_get.responses.append({"episodes": []})
        _run(mcp.search_episodes(keywords=["hot", "long", "run"], limit=5))
        assert fake_get.calls == [(
            "/api/memory/episodes/search",
            {"q": "hot long run", "limit": 5},
        )]

    def test_default_limit_10(self, fake_get):
        fake_get.responses.append({"episodes": []})
        _run(mcp.search_episodes(keywords=["rainy"]))
        assert fake_get.calls == [(
            "/api/memory/episodes/search",
            {"q": "rainy", "limit": 10},
        )]


class TestGetPendingClarifications:
    def test_path(self, fake_get):
        fake_get.responses.append({"pending": []})
        _run(mcp.get_pending_clarifications())
        assert fake_get.calls == [("/api/memory/pending", {})]


class TestGetModel:
    """PR P1 — pattern store MCP tool. Discovery path: agent asks
    "does model X exist?" and expects None if not, not an exception."""

    def test_returns_payload_when_found(self, fake_get):
        fake_get.responses.append({
            "model_id": "mdl_abc",
            "model_key": "recovery.hrv_14d_baseline",
            "name": "14天 HRV 基线",
            "params_json": {"mean": 70.5},
        })
        result = _run(mcp.get_model(model_key="recovery.hrv_14d_baseline"))
        assert result["model_id"] == "mdl_abc"
        assert result["params_json"] == {"mean": 70.5}
        assert fake_get.calls == [
            ("/api/memory/models/recovery.hrv_14d_baseline", {}),
        ]

    def test_returns_none_on_404(self):
        """Codex P2 catch on PR P1 (#76). The API returns 404 for
        not-yet-seeded models (semantically correct), but the
        MCP tool's docstring promises `None` so agents can branch on
        `is None` rather than try/except every discovery call.
        Verify the 404 is swallowed → None."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        # Mock _get to raise the same HTTPStatusError httpx would on 404
        response = MagicMock(spec=httpx.Response, status_code=404)
        err = httpx.HTTPStatusError("404", request=MagicMock(), response=response)
        mock_get = AsyncMock(side_effect=err)

        with patch("backend.personal_coach_mcp._get", new=mock_get):
            result = _run(mcp.get_model(model_key="recovery.not_yet_built"))
        assert result is None

    def test_non_404_errors_still_propagate(self):
        """A 500 / 503 / network failure is NOT 'model missing' — those
        must surface to the caller, not get silently None'd."""
        import httpx
        from unittest.mock import AsyncMock, MagicMock

        response = MagicMock(spec=httpx.Response, status_code=500)
        err = httpx.HTTPStatusError("500", request=MagicMock(), response=response)
        mock_get = AsyncMock(side_effect=err)

        with patch("backend.personal_coach_mcp._get", new=mock_get):
            with pytest.raises(httpx.HTTPStatusError):
                _run(mcp.get_model(model_key="any"))


class TestListModels:
    def test_no_filter_path(self, fake_get):
        fake_get.responses.append({"models": []})
        _run(mcp.list_models())
        assert fake_get.calls == [("/api/memory/models", {})]

    def test_category_filter_passed_through(self, fake_get):
        fake_get.responses.append({"models": []})
        _run(mcp.list_models(category="Health/Recovery"))
        assert fake_get.calls == [
            ("/api/memory/models", {"category": "Health/Recovery"}),
        ]

    def test_status_filter_passed_through(self, fake_get):
        fake_get.responses.append({"models": []})
        _run(mcp.list_models(status="Stable"))
        assert fake_get.calls == [
            ("/api/memory/models", {"status": "Stable"}),
        ]

    def test_both_filters(self, fake_get):
        fake_get.responses.append({"models": []})
        _run(mcp.list_models(category="Running", status="Forming"))
        assert fake_get.calls == [
            ("/api/memory/models", {"category": "Running", "status": "Forming"}),
        ]

    def test_none_args_dropped_from_query(self, fake_get):
        """category=None / status=None must NOT appear as `?category=&status=`
        in the URL — that'd give us empty-string filters server-side
        instead of unfiltered."""
        fake_get.responses.append({"models": []})
        _run(mcp.list_models(category=None, status=None))
        assert fake_get.calls == [("/api/memory/models", {})]
