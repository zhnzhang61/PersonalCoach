"""Unit tests for data_processor.py — the project's data layer.

Per the Phase 3 plan in docs/IMPROVEMENTS.md: `data_processor.py` is
the layer everything else reads through (the agent's MCP tools all
wrap api_server endpoints, which all delegate to DataProcessor). This
file covers it in two passes.

Pass 1 (✅ 2026-05-12):
  • RunActivity dataclass — Garmin-dict parsing + derived props
  • ManualActivity dataclass — round-trip serialization
  • _bucket_run_surface module helper
  • DataProcessor bootstrap on tmp_path (no real data/ touched)
  • Semantic memory CRUD
  • Training blocks CRUD + validation
  • Manual activity CRUD
  • calculate_category_stats — perceived-stream derivation
  • compute_telemetry_summary — pandas pure function

Pass 2 (✅ 2026-05-27 — this PR):
  • compile_health_ledger — joins sleep/rhr/hrv/stress/activity files
  • get_hr_zones — user_zones.json parser + rpe_label projection
  • get_athlete_profile_full — semantic-memory composite + LT pace
  • get_readiness — green/yellow/red verdict + baseline deltas
  • get_training_load — ACWR bands, weekly miles trend
  • compute_cycle_and_week_stats — block/week aggregation

Out of scope (no current need):
  • telemetry/laps/route/weather IO — only the get_run_laps helper
    used by compute_cycle_and_week_stats is exercised here.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from backend.data_processor import (
    DataProcessor,
    ManualActivity,
    RunActivity,
    _bucket_run_surface,
)


# ---------------------------------------------------------------------------
# Shared fixture: a DataProcessor pointing at an isolated tmp_path so each
# test gets a fresh dir tree with no carryover from real `data/`.
# ---------------------------------------------------------------------------

@pytest.fixture
def proc(tmp_path):
    return DataProcessor(data_dir=str(tmp_path))


# ===========================================================================
# RunActivity
# ===========================================================================

class TestRunActivityFromGarmin:
    """`from_garmin(d)` is the hot path — every run-list endpoint
    funnels through it. Covers field mapping, manual_meta overlay,
    and the various `None`-fallback branches."""

    @staticmethod
    def _full_garmin_dict() -> dict:
        """A representative Garmin activity dict with every field we map."""
        return {
            "activityId": 22833575003,
            "startTimeLocal": "2026-05-10T07:14:00",
            "activityName": "Weehawken Running",
            "distance": 17703.0,             # meters
            "movingDuration": 1947.0,
            "duration": 1965.0,
            "averageHR": 162,
            "elevationGain": 75.0,           # meters
            "calories": 1234,
            "activityType": {
                "typeKey": "running",
                "subTypeKey": "road_running",
            },
            "manual_meta": {
                "name": "Weehawken LR",
                "notes": "felt steady",
                "category_stats": [
                    {"category": "Steady Effort", "distance_mi": 10.0,
                     "pace": "9:23", "avg_hr": 159},
                ],
                "lap_categories": ["Steady Effort"] * 10 + ["Marathon"],
            },
        }

    def test_maps_all_top_level_fields(self):
        ra = RunActivity.from_garmin(self._full_garmin_dict())
        assert ra.activity_id == 22833575003
        assert ra.date == "2026-05-10"
        assert ra.distance_m == 17703.0
        assert ra.moving_duration_s == 1947.0
        assert ra.duration_s == 1965.0
        assert ra.avg_hr == 162
        assert ra.elevation_gain_m == 75.0
        assert ra.calories == 1234
        assert ra.surface == "road"

    def test_manual_meta_overlays_name_and_notes(self):
        ra = RunActivity.from_garmin(self._full_garmin_dict())
        # manual_meta.name overrides activityName
        assert ra.name == "Weehawken LR"
        assert ra.notes == "felt steady"

    def test_manual_meta_overlays_categories(self):
        ra = RunActivity.from_garmin(self._full_garmin_dict())
        assert len(ra.lap_categories) == 11
        assert ra.lap_categories[-1] == "Marathon"
        assert ra.category_stats[0]["category"] == "Steady Effort"

    def test_falls_back_to_activity_name_without_manual_meta(self):
        d = self._full_garmin_dict()
        del d["manual_meta"]
        ra = RunActivity.from_garmin(d)
        assert ra.name == "Weehawken Running"
        assert ra.notes == ""
        assert ra.category_stats == []
        assert ra.lap_categories == []

    def test_falls_back_to_run_when_neither_name(self):
        d = self._full_garmin_dict()
        del d["manual_meta"]
        del d["activityName"]
        ra = RunActivity.from_garmin(d)
        assert ra.name == "Run"

    def test_missing_optional_fields_default_safely(self):
        """The watch occasionally drops fields (track / treadmill runs
        without HR / elevation). Make sure those become 0 / None, not
        KeyError."""
        d = {
            "activityId": 1,
            "startTimeLocal": "2026-01-01T08:00:00",
            "activityType": {"typeKey": "running"},
        }
        ra = RunActivity.from_garmin(d)
        assert ra.distance_m == 0
        assert ra.moving_duration_s == 0
        assert ra.duration_s == 0
        assert ra.avg_hr is None
        assert ra.elevation_gain_m == 0
        assert ra.calories == 0
        assert ra.surface == "road"  # no subTypeKey → default

    def test_preserves_raw_payload(self):
        d = self._full_garmin_dict()
        ra = RunActivity.from_garmin(d)
        assert ra.raw is d  # exact reference, not a copy

    def test_date_extraction_is_first_10_chars(self):
        d = self._full_garmin_dict()
        d["startTimeLocal"] = "2026-05-10T07:14:00.123Z"
        assert RunActivity.from_garmin(d).date == "2026-05-10"

    def test_empty_start_time_local_yields_empty_date(self):
        d = self._full_garmin_dict()
        d["startTimeLocal"] = None
        assert RunActivity.from_garmin(d).date == ""


class TestRunActivityIsRunDict:
    """Garmin uses typeKey strings like 'running', 'trail_running',
    'cycling' — we keep all the running-family ones, drop everything
    else. Used by /api/runs to filter the activity list."""

    @pytest.mark.parametrize("type_key,expected", [
        ("running", True),
        ("trail_running", True),
        ("treadmill_running", True),
        ("road_running", True),
        ("cycling", False),
        ("strength_training", False),
        ("", False),
    ])
    def test_typekey_match(self, type_key, expected):
        d = {"activityType": {"typeKey": type_key}}
        assert RunActivity.is_run_dict(d) is expected

    def test_missing_activity_type_returns_false(self):
        assert RunActivity.is_run_dict({}) is False

    def test_activity_type_explicit_none(self):
        assert RunActivity.is_run_dict({"activityType": None}) is False


class TestRunActivityDerivedProps:
    @staticmethod
    def _ra(**overrides):
        defaults = dict(
            activity_id=1, date="2026-01-01", name="Run",
            distance_m=16093.4,        # = 10 mi exactly
            moving_duration_s=3000,    # 50 min
            duration_s=3060,
            avg_hr=160, elevation_gain_m=100,
            calories=800, surface="road", notes="",
        )
        defaults.update(overrides)
        return RunActivity(**defaults)

    def test_distance_mi_conversion(self):
        assert self._ra(distance_m=16093.4).distance_mi == pytest.approx(10.0, rel=1e-4)

    def test_elevation_ft_conversion(self):
        # 100 m → 328 ft (int truncation)
        assert self._ra(elevation_gain_m=100).elevation_ft == 328

    def test_effective_duration_prefers_moving(self):
        ra = self._ra(moving_duration_s=3000, duration_s=3060)
        assert ra.effective_duration_s == 3000

    def test_effective_duration_falls_back_to_total_when_moving_zero(self):
        """Garmin reports movingDuration=0 on some track/treadmill runs.
        Without the fallback, pace math would divide by zero."""
        ra = self._ra(moving_duration_s=0, duration_s=1800)
        assert ra.effective_duration_s == 1800

    def test_pace_str_for_normal_run(self):
        # 10 mi in 50 min = 5:00/mi
        ra = self._ra(distance_m=16093.4, moving_duration_s=3000)
        assert ra.pace_str() == "5:00"

    def test_pace_str_with_seconds_remainder(self):
        # 1 mi in 7:42 = 462 seconds → "7:42"
        ra = self._ra(distance_m=1609.34, moving_duration_s=462)
        assert ra.pace_str() == "7:42"

    def test_pace_str_zero_distance_returns_na(self):
        assert self._ra(distance_m=0).pace_str() == "N/A"

    def test_pace_str_zero_duration_returns_na(self):
        assert self._ra(moving_duration_s=0, duration_s=0).pace_str() == "N/A"


# ===========================================================================
# ManualActivity
# ===========================================================================

class TestManualActivityRoundTrip:
    def test_minimal_roundtrip(self):
        original = {"id": "manual_1", "date": "2026-05-10",
                    "type": "swim", "desc": "1500m easy"}
        ma = ManualActivity.from_dict(original)
        assert ma.id == "manual_1"
        assert ma.type == "swim"
        assert ma.description == "1500m easy"
        # to_dict round-trips the minimal fields
        assert ma.to_dict() == original

    def test_full_roundtrip_with_optionals(self):
        original = {
            "id": "manual_2", "date": "2026-05-10", "type": "run",
            "desc": "easy shake-out", "duration_min": 30.0,
            "distance_mi": 3.5, "start_time": "07:30",
        }
        ma = ManualActivity.from_dict(original)
        assert ma.duration_min == 30.0
        assert ma.distance_mi == 3.5
        assert ma.start_time == "07:30"
        assert ma.to_dict() == original

    def test_unknown_type_normalized_to_other(self):
        ma = ManualActivity.from_dict({"id": "x", "date": "2026-01-01",
                                       "type": "yoga", "desc": ""})
        assert ma.type == "other"

    def test_to_dict_omits_none_optionals(self):
        ma = ManualActivity(id="x", date="2026-01-01", type="gym",
                            description="lift", duration_min=None,
                            distance_mi=None, start_time=None)
        d = ma.to_dict()
        assert "duration_min" not in d
        assert "distance_mi" not in d
        assert "start_time" not in d

    def test_missing_desc_becomes_empty_string(self):
        """Legacy entries had no `desc` field. from_dict tolerates that."""
        ma = ManualActivity.from_dict({"id": "x", "date": "2026-01-01",
                                       "type": "swim"})
        assert ma.description == ""


# ===========================================================================
# Module-level helpers
# ===========================================================================

class TestBucketRunSurface:
    @pytest.mark.parametrize("sub_type,expected", [
        ("track_running", "track"),
        ("treadmill_running", "treadmill"),
        ("indoor_running", "treadmill"),
        ("trail_running", "trail"),
        ("road_running", "road"),
        ("street_running", "road"),  # default
        ("", "road"),                # default on empty
        (None, "road"),              # default on None
    ])
    def test_buckets(self, sub_type, expected):
        assert _bucket_run_surface(sub_type) == expected


# ===========================================================================
# DataProcessor bootstrap
# ===========================================================================

class TestDataProcessorBootstrap:
    """The constructor calls `_ensure_infrastructure` which creates the
    whole data/ scaffold. Each test gets a fresh tmp_path so we can
    verify directory + seed-file creation without touching real data."""

    def test_constructs_in_tmp_dir_without_touching_real_data(self, tmp_path):
        proc = DataProcessor(data_dir=str(tmp_path))
        assert proc.data_dir == str(tmp_path)

    def test_creates_garmin_subdirs(self, tmp_path):
        DataProcessor(data_dir=str(tmp_path))
        for sub in ("get_activities", "get_sleep_data", "get_hrv_data",
                    "get_stress_data"):
            assert (tmp_path / sub).is_dir(), f"{sub} should exist"

    def test_seeds_empty_blocks_file(self, tmp_path):
        DataProcessor(data_dir=str(tmp_path))
        blocks_file = tmp_path / "blocks" / "training_blocks.json"
        assert blocks_file.is_file()
        assert json.loads(blocks_file.read_text()) == []

    def test_seeds_empty_aux_file(self, tmp_path):
        DataProcessor(data_dir=str(tmp_path))
        aux_file = tmp_path / "blocks" / "auxiliary_log.json"
        assert aux_file.is_file()
        assert json.loads(aux_file.read_text()) == []

    def test_seeds_semantic_memory_with_template(self, tmp_path):
        """semantic_memory.json gets a non-empty seed so callers
        reading it on first run don't see {} and skip injection."""
        DataProcessor(data_dir=str(tmp_path))
        sm_file = tmp_path / "memory" / "user_profile.json"
        assert sm_file.is_file()
        # Seeded with at least one key — exact shape may evolve.
        content = json.loads(sm_file.read_text())
        assert isinstance(content, dict)

    def test_seeds_empty_episodic_memory(self, tmp_path):
        DataProcessor(data_dir=str(tmp_path))
        ep_file = tmp_path / "memory" / "episodic_logs.json"
        assert ep_file.is_file()
        assert json.loads(ep_file.read_text()) == []


# ===========================================================================
# Semantic memory CRUD
# ===========================================================================

class TestSemanticMemory:
    def test_get_returns_seeded_dict(self, proc):
        mem = proc.get_semantic_memory()
        assert isinstance(mem, dict)

    def test_update_creates_new_dict_category(self, proc):
        # `race_history` isn't in the default seed → category created as dict
        proc.update_semantic_memory("race_history", "2026_pr_5k", "18:34")
        mem = proc.get_semantic_memory()
        assert mem["race_history"]["2026_pr_5k"] == "18:34"

    def test_update_overwrites_existing_key_in_dict_category(self, proc):
        proc.update_semantic_memory("race_history", "2026_pr_5k", "18:34")
        proc.update_semantic_memory("race_history", "2026_pr_5k", "18:12")
        assert proc.get_semantic_memory()["race_history"]["2026_pr_5k"] == "18:12"

    def test_update_appends_to_list_category_without_duplicate(self, proc):
        # `medical_notes` is a list-typed category in the default seed,
        # so update_semantic_memory appends the value (ignoring `key`)
        # and dedupes.
        proc.update_semantic_memory("medical_notes", None, "right foot external rotation")
        proc.update_semantic_memory("medical_notes", None, "right foot external rotation")
        proc.update_semantic_memory("medical_notes", None, "left shoulder tight")
        notes = proc.get_semantic_memory()["medical_notes"]
        assert notes.count("right foot external rotation") == 1  # de-duped
        assert "left shoulder tight" in notes


# ===========================================================================
# Training blocks CRUD
# ===========================================================================

class TestTrainingBlocks:
    def test_get_blocks_returns_empty_list_initially(self, proc):
        assert proc.get_blocks() == []

    def test_create_assigns_sequential_ids(self, proc):
        id1 = proc.create_block("Pre Fall 2026", "2026-05-01", "2026-06-15")
        id2 = proc.create_block("Fall Build", "2026-06-16", "2026-08-31")
        assert id1 == "block_001"
        assert id2 == "block_002"

    def test_create_rejects_empty_name(self, proc):
        with pytest.raises(ValueError, match="name"):
            proc.create_block("", "2026-05-01", "2026-06-15")

    def test_create_rejects_inverted_dates(self, proc):
        with pytest.raises(ValueError, match="end_date"):
            proc.create_block("Block X", "2026-06-15", "2026-05-01")

    def test_get_blocks_sorted_newest_first(self, proc):
        proc.create_block("Older", "2026-01-01", "2026-02-01")
        proc.create_block("Newer", "2026-05-01", "2026-06-01")
        blocks = proc.get_blocks()
        assert blocks[0]["name"] == "Newer"
        assert blocks[1]["name"] == "Older"

    def test_update_block_patches_fields(self, proc):
        bid = proc.create_block("Block X", "2026-05-01", "2026-06-01")
        ok = proc.update_block(bid, name="Renamed Block")
        assert ok is True
        assert proc.get_blocks()[0]["name"] == "Renamed Block"

    def test_update_block_rejects_inverted_dates_on_merge(self, proc):
        bid = proc.create_block("Block X", "2026-05-01", "2026-06-01")
        with pytest.raises(ValueError, match="end_date"):
            proc.update_block(bid, end_date="2026-04-01")

    def test_update_block_unknown_id_returns_false(self, proc):
        assert proc.update_block("block_999", name="Ghost") is False

    def test_delete_block_returns_true_when_removed(self, proc):
        bid = proc.create_block("Block X", "2026-05-01", "2026-06-01")
        assert proc.delete_block(bid) is True
        assert proc.get_blocks() == []

    def test_delete_block_returns_false_when_missing(self, proc):
        assert proc.delete_block("block_999") is False


# ===========================================================================
# Manual activity CRUD
# ===========================================================================

class TestManualActivityCRUD:
    def test_add_returns_entry_with_generated_id(self, proc):
        e = proc.add_manual_activity("2026-05-10", "swim", "1500m easy",
                                      duration_min=30)
        assert e["id"].startswith("manual_")
        assert e["type"] == "swim"
        assert e["duration_min"] == 30

    def test_add_normalizes_unknown_type_to_other(self, proc):
        e = proc.add_manual_activity("2026-05-10", "yoga", "")
        assert e["type"] == "other"

    def test_update_patches_in_place(self, proc):
        e = proc.add_manual_activity("2026-05-10", "swim", "1500m easy",
                                      duration_min=30)
        updated = proc.update_manual_activity(e["id"], duration_min=45)
        assert updated["duration_min"] == 45

    def test_update_description_field_renamed_to_desc_on_disk(self, proc):
        e = proc.add_manual_activity("2026-05-10", "gym", "leg day")
        updated = proc.update_manual_activity(e["id"], description="leg + core")
        assert updated["desc"] == "leg + core"
        assert "description" not in updated

    def test_update_clears_optional_field_with_none(self, proc):
        e = proc.add_manual_activity("2026-05-10", "swim", "1500m easy",
                                      duration_min=30)
        updated = proc.update_manual_activity(e["id"], duration_min=None)
        assert "duration_min" not in updated

    def test_update_rejects_clearing_required_field(self, proc):
        e = proc.add_manual_activity("2026-05-10", "gym", "leg day")
        with pytest.raises(ValueError, match="required"):
            proc.update_manual_activity(e["id"], date=None)

    def test_update_unknown_id_returns_none(self, proc):
        assert proc.update_manual_activity("manual_99999", duration_min=5) is None

    def test_delete_returns_true_when_removed(self, proc):
        e = proc.add_manual_activity("2026-05-10", "swim", "easy")
        assert proc.delete_manual_activity(e["id"]) is True

    def test_delete_returns_false_when_missing(self, proc):
        assert proc.delete_manual_activity("manual_99999") is False

    def test_get_in_range_filters_by_date(self, proc):
        proc.add_manual_activity("2026-05-10", "swim", "in")
        proc.add_manual_activity("2026-04-01", "swim", "before")
        proc.add_manual_activity("2026-06-01", "swim", "after")
        in_range = proc.get_manual_activities_in_range("2026-05-01", "2026-05-31")
        assert len(in_range) == 1
        assert in_range[0]["desc"] == "in"


# ===========================================================================
# calculate_category_stats — the perceived-stream aggregation
# ===========================================================================

class TestCalculateCategoryStats:
    """User labels each lap with one of the RPE-named categories;
    we aggregate per category into the per-segment summary that
    feeds /api/runs/{id}.manual_meta.category_stats and the agent's
    perceived-short-term stream."""

    def test_groups_laps_by_category(self, proc):
        # 10 Steady laps + 1 Marathon lap, all 1 mile each (1609.34 m)
        laps = [
            {"category": "Steady Effort", "distance": 1609.34,
             "duration": 540, "averageHR": 159}
            for _ in range(10)
        ] + [
            {"category": "Marathon", "distance": 1609.34,
             "duration": 462, "averageHR": 174},
        ]
        out = proc.calculate_category_stats(laps)
        by_cat = {r["category"]: r for r in out}
        assert "Steady Effort" in by_cat
        assert "Marathon" in by_cat
        assert by_cat["Steady Effort"]["distance_mi"] == 10.0
        assert by_cat["Marathon"]["distance_mi"] == 1.0

    def test_pace_computed_from_weighted_distance_time(self, proc):
        # Single category, 1 mi @ 9:00 + 1 mi @ 10:00 → 9:30/mi avg
        laps = [
            {"category": "Steady Effort", "distance": 1609.34,
             "duration": 540, "averageHR": 150},
            {"category": "Steady Effort", "distance": 1609.34,
             "duration": 600, "averageHR": 160},
        ]
        out = proc.calculate_category_stats(laps)
        assert out[0]["pace"] == "9:30"

    def test_avg_hr_distance_weighted(self, proc):
        # 2 mi @ HR 150 + 1 mi @ HR 180 → weighted avg = 160
        laps = [
            {"category": "Steady Effort", "distance": 3218.68,
             "duration": 1080, "averageHR": 150},
            {"category": "Steady Effort", "distance": 1609.34,
             "duration": 540, "averageHR": 180},
        ]
        out = proc.calculate_category_stats(laps)
        assert out[0]["avg_hr"] == 160

    def test_invalid_category_demoted_to_rest(self, proc):
        laps = [
            {"category": "Sleepwalking", "distance": 1000,
             "duration": 360, "averageHR": 110},
        ]
        out = proc.calculate_category_stats(laps)
        assert out[0]["category"] == "Rest"

    def test_zero_distance_lap_yields_na_pace_and_zero_hr(self, proc):
        laps = [
            {"category": "Rest", "distance": 0, "duration": 60,
             "averageHR": 100},
        ]
        out = proc.calculate_category_stats(laps)
        assert out[0]["pace"] == "N/A"
        assert out[0]["avg_hr"] == 0

    def test_empty_input_returns_empty_list(self, proc):
        assert proc.calculate_category_stats([]) == []


# ===========================================================================
# compute_telemetry_summary — pandas pure function
# ===========================================================================

class TestComputeTelemetrySummary:
    @staticmethod
    def _df(**cols):
        return pd.DataFrame(cols)

    def test_empty_df_returns_empty_dict(self, proc):
        assert proc.compute_telemetry_summary(pd.DataFrame()) == {}

    def test_none_returns_empty_dict(self, proc):
        assert proc.compute_telemetry_summary(None) == {}

    def test_heart_rate_avg_min_max(self, proc):
        df = self._df(HeartRate=[120, 140, 160, 180], Pace=[8.0]*4)
        out = proc.compute_telemetry_summary(df)
        hr = out["HeartRate"]
        assert hr["avg"] == 150.0
        assert hr["min"] == 120
        assert hr["max"] == 180

    def test_pace_clipped_to_valid_range(self, proc):
        """Idle seconds show up as huge `Pace` values (min/mi → ∞).
        PACE_CLIP_MIN_PER_MI filters those out before averaging so the
        result reflects actual running pace."""
        pace_lo, pace_hi = proc.PACE_CLIP_MIN_PER_MI
        df = self._df(
            HeartRate=[150]*4,
            Pace=[8.0, 9.0, pace_hi + 5, pace_lo - 1],  # last two are clipped
        )
        out = proc.compute_telemetry_summary(df)
        # avg of just 8.0 and 9.0 → 8.5
        assert out["Pace"]["avg"] == pytest.approx(8.5)

    def test_missing_metric_yields_none(self, proc):
        """Watch without a chest strap → no GroundContactBalance column.
        That key should be present in the output but valued None."""
        df = self._df(HeartRate=[150]*3, Pace=[8.0]*3)
        out = proc.compute_telemetry_summary(df)
        assert out["GroundContactBalanceLeft"] is None
        assert out["RespirationRate"] is None

    def test_nans_are_dropped_not_averaged(self, proc):
        import numpy as np
        df = self._df(HeartRate=[150, np.nan, 160, np.nan], Pace=[8.0]*4)
        out = proc.compute_telemetry_summary(df)
        assert out["HeartRate"]["avg"] == 155.0


# ===========================================================================
# Pass 2 — Garmin-file-dependent paths
# ===========================================================================
#
# All of these read JSON / CSV off disk under `proc.paths[*]`. The fixture
# helpers below write fixtures into the right locations so each test starts
# with a clean slate; tests opt-in to the data they need.


def _write_json(path, payload):
    """Write a JSON payload to `path`, creating parent dirs."""
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


def _write_sleep(proc, date_str: str, sleep_seconds, sleep_score):
    """Drop a minimal sleep file under proc.paths['sleep']/<date>.json."""
    path = f"{proc.paths['sleep']}/{date_str}.json"
    _write_json(path, {
        "dailySleepDTO": {
            "calendarDate": date_str,
            "sleepTimeSeconds": sleep_seconds,
            "sleepScores": {"overall": {"value": sleep_score}},
        },
    })


def _write_rhr(proc, date_str: str, value):
    path = f"{proc.paths['rhr']}/{date_str}.json"
    _write_json(path, {
        "allMetrics": {"metricsMap": {
            "WELLNESS_RESTING_HEART_RATE": [
                {"value": value, "calendarDate": date_str}
            ]
        }},
    })


def _write_hrv(proc, date_str: str, weekly_avg):
    path = f"{proc.paths['hrv']}/{date_str}.json"
    _write_json(path, {
        "hrvSummary": {"calendarDate": date_str, "weeklyAvg": weekly_avg},
    })


def _write_stress(proc, date_str: str, avg_stress):
    path = f"{proc.paths['stress']}/{date_str}.json"
    _write_json(path, {"avgStressLevel": avg_stress})


def _write_activity_summary(proc, activity_id, date_str, **extra):
    """Drop an activity-summary fixture under proc.paths['activities'].
    Defaults are running-shaped; pass `activityType` to override (e.g.
    swim/bike) for negative tests."""
    payload = {
        "activityId": activity_id,
        "activityName": "Run",
        "startTimeLocal": f"{date_str}T07:00:00",
        "distance": 8000.0,         # ~5 mi in metres
        "movingDuration": 2400.0,   # 40 min
        "duration": 2410.0,
        "averageHR": 150,
        "elevationGain": 30.0,
        "calories": 500,
        "activityType": {"typeKey": "running", "subTypeKey": "road_running"},
        "activityTrainingLoad": 90.0,
        **extra,
    }
    _write_json(
        f"{proc.paths['activities']}/{activity_id}_summary.json",
        payload,
    )


# ---------------------------------------------------------------------------
# compile_health_ledger
# ---------------------------------------------------------------------------


class TestCompileHealthLedger:
    """Iterates `days_back` days backwards from today, joining per-day
    sleep/rhr/hrv/stress files with same-day activity totals. Output is
    both returned to the caller and persisted to `paths['ledger']`."""

    def test_empty_data_dir_returns_skeleton(self, proc):
        records = proc.compile_health_ledger(days_back=3)
        assert len(records) == 3
        # All fields None / zero since no fixtures exist.
        assert all(r["sleep_score"] is None for r in records)
        assert all(r["rhr"] is None for r in records)
        assert all(r["hrv"] is None for r in records)
        # Ascending by date.
        assert records[0]["date"] < records[1]["date"] < records[2]["date"]

    def test_joins_sleep_rhr_hrv(self, proc):
        import datetime
        today = datetime.date.today().isoformat()
        _write_sleep(proc, today, sleep_seconds=28800, sleep_score=82)
        _write_rhr(proc, today, 51.0)
        _write_hrv(proc, today, 68)
        _write_stress(proc, today, 22)

        records = proc.compile_health_ledger(days_back=1)
        assert len(records) == 1
        r = records[0]
        assert r["date"] == today
        assert r["sleep_score"] == 82
        assert r["sleep_hours"] == 8.0  # 28800/3600
        assert r["rhr"] == 51.0
        assert r["hrv"] == 68
        assert r["stress"] == 22

    def test_aggregates_activity_miles_and_minutes(self, proc):
        import datetime
        today = datetime.date.today().isoformat()
        # Two activities same day: 8000 m + 5000 m = 13000 m ≈ 8.08 mi.
        _write_activity_summary(proc, 100, today, distance=8000.0, duration=2400.0)
        _write_activity_summary(proc, 101, today, distance=5000.0, duration=1800.0)
        records = proc.compile_health_ledger(days_back=1)
        r = records[0]
        assert r["run_miles"] == round(13000 / 1609.34, 2)
        assert r["run_mins"] == round((2400 + 1800) / 60, 1)

    def test_persists_csv_to_ledger_path(self, proc):
        """The ledger CSV is the source-of-truth for `get_health_stats`
        and downstream `get_readiness`. Existence + readable schema are
        load-bearing."""
        import os
        import datetime
        today = datetime.date.today().isoformat()
        _write_sleep(proc, today, 25200, 70)
        proc.compile_health_ledger(days_back=2)
        assert os.path.exists(proc.paths["ledger"])
        # Re-read via the typed accessor.
        stats = proc.get_health_stats()
        assert len(stats) == 2
        # Floats come back as floats (not strings) after the cast in
        # get_health_stats.
        today_row = next(r for r in stats if r["date"] == today)
        assert today_row["sleep_hours"] == 7.0

    def test_hrv_data_lastNight_overrides_summary_weeklyAvg(self, proc):
        """The code path that prefers `hrvData.lastNightAvg` over the
        weekly avg when both are present — pin it so a refactor can't
        invert the precedence."""
        import datetime
        today = datetime.date.today().isoformat()
        path = f"{proc.paths['hrv']}/{today}.json"
        _write_json(path, {
            "hrvSummary": {"weeklyAvg": 65},
            "hrvData": {"lastNightAvg": 71},
        })
        records = proc.compile_health_ledger(days_back=1)
        assert records[0]["hrv"] == 71


# ---------------------------------------------------------------------------
# get_hr_zones
# ---------------------------------------------------------------------------


class TestGetHrZones:
    """Parses `<data>/manual_inputs/user_zones.json` — a dict of zone
    name → bpm range — into a sorted list of {name, low, high,
    rpe_label} dicts. The rpe_label projection unifies vocab with
    `manual_meta.lap_categories`; the chart UI and the AI both consume
    this shape."""

    def test_returns_empty_when_file_missing(self, proc):
        assert proc.get_hr_zones() == []

    def test_parses_and_sorts_standard_zones(self, proc):
        _write_json(proc.paths["user_zones"], {
            "VO2 Max":              "175-185 bpm",
            "Lactate Threshold":    "165-175 bpm",
            "Marathon Pace":        "155-165 bpm",
            "Steady / Constant":    "140-155 bpm",
            "Hold Back / Recovery": "120-140 bpm",
        })
        zones = proc.get_hr_zones()
        # Sorted ascending by `low`.
        assert [z["low"] for z in zones] == [120, 140, 155, 165, 175]
        # rpe_label maps to the EFFORT_CATEGORIES vocabulary.
        first = zones[0]
        assert first["name"] == "Hold Back / Recovery"
        assert first["rpe_label"] == "Hold Back Easy"
        # VO2 Max maps to "VO2Max" (no space).
        last = zones[-1]
        assert last["rpe_label"] == "VO2Max"

    def test_open_ended_ranges(self, proc):
        """`<NNN bpm` and `>NNN bpm` are sentinel-clamped to 0 and 220."""
        _write_json(proc.paths["user_zones"], {
            "Recovery": "<120 bpm",
            "Topped Out": ">185 bpm",
        })
        zones = proc.get_hr_zones()
        recovery = next(z for z in zones if z["name"] == "Recovery")
        assert recovery["low"] == 0
        assert recovery["high"] == 119
        topped = next(z for z in zones if z["name"] == "Topped Out")
        assert topped["low"] == 186
        assert topped["high"] == 220

    def test_unparseable_zone_silently_skipped(self, proc):
        """A bad value (typo, missing unit) shouldn't poison the rest of
        the list — the UI should still see the valid zones."""
        _write_json(proc.paths["user_zones"], {
            "VO2 Max": "175-185 bpm",
            "Junk":    "not a range",
        })
        zones = proc.get_hr_zones()
        names = [z["name"] for z in zones]
        assert "VO2 Max" in names
        assert "Junk" not in names

    def test_unknown_zone_name_keeps_literal_rpe_label(self, proc):
        """If a user adds a custom zone name we don't have in
        `_ZONE_TO_RPE_LABEL`, fall back to the zone name itself rather
        than dropping the entry."""
        _write_json(proc.paths["user_zones"], {
            "Tempo": "150-160 bpm",
        })
        zones = proc.get_hr_zones()
        assert zones[0]["rpe_label"] == "Tempo"


# ---------------------------------------------------------------------------
# get_athlete_profile_full
# ---------------------------------------------------------------------------


class TestGetAthleteProfileFull:
    """Composite profile for the AI coach. Pulls from semantic memory
    (`user_profile.json` `garmin_profile.userData`), training blocks
    store, and user_zones. Surfaces age, weight_kg, vo2max, LT pace,
    current block + phase."""

    def _seed_profile(self, proc, user_data: dict, preferences=None, medical=None):
        with open(proc.paths["semantic_memory"], "w") as f:
            json.dump({
                "garmin_profile": {"userData": user_data},
                "preferences": preferences or [],
                "medical_notes": medical or [],
            }, f)

    def test_empty_userData(self, proc):
        self._seed_profile(proc, {})
        out = proc.get_athlete_profile_full()
        assert out["athlete"]["age"] is None
        assert out["athlete"]["weight_kg"] is None
        assert out["fitness"]["vo2max_running"] is None
        assert out["fitness"]["lactate_threshold_pace"] is None
        assert out["current_block"] is None
        assert out["preferences"] == []

    def test_weight_grams_to_kg(self, proc):
        self._seed_profile(proc, {"weight": 72400})  # grams
        out = proc.get_athlete_profile_full()
        assert out["athlete"]["weight_kg"] == 72.4

    def test_age_birthday_not_yet_this_year(self, proc):
        """Age is "completed years" — birthday-not-yet-this-year shaves
        a year off (year_diff - 1).

        codex P2 on PR #81 — the original `today + 60 days` shortcut
        rolled into the next calendar year for late-Nov/Dec runs,
        flipping the assertion. Pick a date a few days later this year
        (always in same calendar year unless today is in the last few
        days of December, in which case skip — the inverse-branch test
        below covers that case)."""
        import datetime
        today = datetime.date.today()
        future = today + datetime.timedelta(days=5)
        if future.year != today.year:
            pytest.skip(
                "Year-end edge: no future date stays in the current "
                "calendar year. The "
                "test_age_birthday_already_passed_this_year companion "
                "covers the inverse branch."
            )
        # Clamp day to 28 to avoid invalid (Feb 29 etc.) constructed
        # dates; the chosen day is always > today.day after the +5,
        # before the clamp, so the (month, day) < (month, clamped_day)
        # comparison still resolves True (since today.day must be
        # smaller than 28 if the constructed day was >28).
        birth = f"1990-{future.month:02d}-{min(future.day, 28):02d}"
        self._seed_profile(proc, {"birthDate": birth})
        out = proc.get_athlete_profile_full()
        assert out["athlete"]["age"] == today.year - 1990 - 1

    def test_age_birthday_already_passed_this_year(self, proc):
        """Inverse branch: birthday earlier this year → no subtraction
        (age == year_diff). Adds robustness to late-Dec runs where the
        not-yet-this-year branch skips."""
        import datetime
        today = datetime.date.today()
        past = today - datetime.timedelta(days=5)
        if past.year != today.year:
            pytest.skip(
                "Year-start edge: 5 days ago is the prior year. The "
                "test_age_birthday_not_yet_this_year companion covers "
                "the inverse branch."
            )
        birth = f"1990-{past.month:02d}-{min(past.day, 28):02d}"
        self._seed_profile(proc, {"birthDate": birth})
        out = proc.get_athlete_profile_full()
        assert out["athlete"]["age"] == today.year - 1990

    def test_lt_pace_from_decimetre_per_sec(self, proc):
        """Garmin's `lactateThresholdSpeed` < 1.0 is empirically dm/s.
        0.369 dm/s × 10 → 3.69 m/s → ~7:16/mi. Verify the conversion
        comes out within the sanity range."""
        self._seed_profile(proc, {"lactateThresholdSpeed": 0.369})
        out = proc.get_athlete_profile_full()
        pace = out["fitness"]["lactate_threshold_pace"]
        assert pace is not None
        assert pace.endswith("/mi")
        # 1609.34 / 3.69 / 60 ≈ 7.27 → "7:16/mi"
        assert pace.startswith("7:")

    def test_lt_pace_from_mps(self, proc):
        """When the value is >= 1.0 it's treated as plain m/s, not
        decimetre/s — that path lets a user with a different storage
        convention still get a sane pace."""
        self._seed_profile(proc, {"lactateThresholdSpeed": 3.69})
        out = proc.get_athlete_profile_full()
        pace = out["fitness"]["lactate_threshold_pace"]
        assert pace is not None
        assert pace.startswith("7:")

    def test_lt_pace_outside_sanity_range_returns_none(self, proc):
        """If the conversion yields something outside 4-14 min/mi the
        sanity clamp drops it rather than emitting nonsense like
        72 min/mi."""
        self._seed_profile(proc, {"lactateThresholdSpeed": 50.0})  # absurd
        out = proc.get_athlete_profile_full()
        assert out["fitness"]["lactate_threshold_pace"] is None

    def test_current_block_phase(self, proc):
        import datetime
        self._seed_profile(proc, {})
        today = datetime.date.today()
        # 8-week block, today = week 4 of 8 → 50% → "build".
        start = (today - datetime.timedelta(weeks=4)).isoformat()
        end = (today + datetime.timedelta(weeks=4)).isoformat()
        proc.create_block("Test Block", start, end)

        out = proc.get_athlete_profile_full()
        cb = out["current_block"]
        assert cb is not None
        assert cb["name"] == "Test Block"
        assert cb["phase"] == "build"
        assert cb["weeks_elapsed"] == 4

    def test_hr_zones_threaded_through(self, proc):
        """`get_athlete_profile_full` calls `get_hr_zones()` directly so
        the AI prompt + chart UI share one source. Pin the wiring."""
        self._seed_profile(proc, {})
        _write_json(proc.paths["user_zones"], {
            "VO2 Max": "175-185 bpm",
        })
        out = proc.get_athlete_profile_full()
        zones = out["fitness"]["hr_zones"]
        assert len(zones) == 1
        assert zones[0]["name"] == "VO2 Max"


# ---------------------------------------------------------------------------
# get_readiness
# ---------------------------------------------------------------------------


def _seed_ledger(proc, rows):
    """Write a ledger CSV directly so get_readiness can read it without
    going through compile_health_ledger's per-day JSON crawl."""
    import csv
    import os
    os.makedirs(os.path.dirname(proc.paths["ledger"]), exist_ok=True)
    with open(proc.paths["ledger"], "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "date", "sleep_score", "sleep_hours", "rhr", "hrv",
                "stress", "run_miles", "run_mins",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


class TestGetReadiness:
    @staticmethod
    def _row(date, *, rhr=None, hrv=None, sleep_hours=None, stress=None):
        return {
            "date": date, "sleep_score": "",
            "sleep_hours": sleep_hours if sleep_hours is not None else "",
            "rhr": rhr if rhr is not None else "",
            "hrv": hrv if hrv is not None else "",
            "stress": stress if stress is not None else "",
            "run_miles": "", "run_mins": "",
        }

    def test_empty_ledger_returns_unknown(self, proc):
        """target_date far in the past = no rows at-or-before it →
        early-out branch returning {"score": "unknown"}.

        (Using today's date here would land in the "no signals" branch
        which scores `red` because sleep_hours=0 trips the <5 floor.
        That's the right rule for the actual signal-bearing case but
        it's not what this test is pinning.)"""
        out = proc.get_readiness(target_date="2020-01-01")
        assert out["readiness"]["score"] == "unknown"
        assert out["today"] is None
        assert out["history_7d"] == []

    def test_green_when_deltas_in_band_and_sleep_ok(self, proc):
        rows = []
        # 7 baseline days w/ stable signals.
        for i in range(7, 0, -1):
            rows.append(self._row(
                f"2026-05-{20 + (7 - i):02d}",
                rhr=50, hrv=70, sleep_hours=7.5,
            ))
        # Today: same numbers, sleep 7.5 → all deltas 0%.
        rows.append(self._row("2026-05-27", rhr=50, hrv=70, sleep_hours=7.5))
        _seed_ledger(proc, rows)

        out = proc.get_readiness(target_date="2026-05-27")
        assert out["readiness"]["score"] == "green"
        assert out["baseline_7d"]["rhr"] == 50.0
        assert out["baseline_7d"]["hrv"] == 70.0

    def test_red_when_hrv_drops_more_than_ten_percent(self, proc):
        rows = [
            self._row(f"2026-05-{20 + i:02d}", rhr=50, hrv=70, sleep_hours=8)
            for i in range(7)
        ]
        # HRV 55 vs baseline 70 → -21.4% → red.
        rows.append(self._row("2026-05-27", rhr=50, hrv=55, sleep_hours=8))
        _seed_ledger(proc, rows)
        out = proc.get_readiness(target_date="2026-05-27")
        assert out["readiness"]["score"] == "red"

    def test_red_when_rhr_spikes_more_than_ten_percent(self, proc):
        rows = [
            self._row(f"2026-05-{20 + i:02d}", rhr=50, hrv=70, sleep_hours=8)
            for i in range(7)
        ]
        # RHR 60 vs baseline 50 → +20% → red.
        rows.append(self._row("2026-05-27", rhr=60, hrv=70, sleep_hours=8))
        _seed_ledger(proc, rows)
        out = proc.get_readiness(target_date="2026-05-27")
        assert out["readiness"]["score"] == "red"

    def test_red_when_sleep_under_five_hours(self, proc):
        rows = [
            self._row(f"2026-05-{20 + i:02d}", rhr=50, hrv=70, sleep_hours=8)
            for i in range(7)
        ]
        rows.append(self._row("2026-05-27", rhr=50, hrv=70, sleep_hours=4.5))
        _seed_ledger(proc, rows)
        out = proc.get_readiness(target_date="2026-05-27")
        assert out["readiness"]["score"] == "red"

    def test_yellow_when_in_between(self, proc):
        rows = [
            self._row(f"2026-05-{20 + i:02d}", rhr=50, hrv=70, sleep_hours=8)
            for i in range(7)
        ]
        # HRV -7% (not in band, not >10%), RHR 0%, sleep 6.5 (not <5, not ≥7).
        rows.append(self._row("2026-05-27", rhr=50, hrv=65, sleep_hours=6.5))
        _seed_ledger(proc, rows)
        out = proc.get_readiness(target_date="2026-05-27")
        assert out["readiness"]["score"] == "yellow"


# ---------------------------------------------------------------------------
# get_training_load
# ---------------------------------------------------------------------------


class TestGetTrainingLoad:
    def test_no_runs_returns_empty_state(self, proc):
        out = proc.get_training_load(window_days=28)
        assert out["acute_7d"]["miles"] == 0
        assert out["chronic_28d"]["miles"] == 0
        assert out["acwr"] is None
        assert out["acwr_band"] == "unknown"
        assert out["weekly_miles_trend"] == []

    def test_filters_non_running_activities(self, proc):
        """A swim summary in the activities dir must NOT contribute to
        run miles. The filter is `RunActivity.is_run_dict`."""
        import datetime
        today = datetime.date.today().isoformat()
        _write_activity_summary(proc, 1, today, distance=8000.0)
        _write_activity_summary(
            proc, 2, today,
            distance=2000.0,
            activityType={"typeKey": "lap_swimming", "subTypeKey": "pool_swimming"},
        )
        out = proc.get_training_load(window_days=7)
        # Only the run (8000 m → ~5.0 mi) should count.
        assert out["acute_7d"]["miles"] == round(8000 / 1609.34, 1)
        assert out["acute_7d"]["session_count"] == 1

    def test_acwr_sweet_band(self, proc):
        """ACWR ≈ 1.0 → "sweet" band. The acute window is
        `today − 7 days` (inclusive), so to keep acute_avg ≈ chronic_avg
        we put one run in acute and three more strictly outside it.
        Total load 400 over 28 days, single 100-load run inside the
        last 7 → 14.29 ≈ 14.29 → ACWR 1.0."""
        import datetime
        today = datetime.date.today()
        # Offsets: 0 in acute (today), 8/14/21 in chronic-only (all
        # strictly older than `today - 7`).
        for i, days_ago in enumerate([0, 8, 14, 21]):
            d = (today - datetime.timedelta(days=days_ago)).isoformat()
            _write_activity_summary(
                proc, 1000 + i, d,
                distance=8000.0,
                activityTrainingLoad=100.0,
            )
        out = proc.get_training_load(window_days=28)
        assert out["acwr"] == 1.0
        assert out["acwr_band"] == "sweet"

    def test_weekly_miles_trend_bucketed_by_monday(self, proc):
        """Each run's date snaps to that week's Monday for trend
        bucketing. Two runs in the same Mon-Sun should land in one
        bucket.

        Dates are generated relative to `today` so the test stays
        valid year-round (codex P2 on PR #81 — fixed-2026-05 dates
        rolled out of the 14-day window once the calendar advanced).
        """
        import datetime
        today = datetime.date.today()
        # Last week (Mon + Wed) — same bucket.
        last_monday = today - datetime.timedelta(days=today.weekday() + 7)
        last_wednesday = last_monday + datetime.timedelta(days=2)
        # This week's Monday (today snapped back) — different bucket.
        this_monday = today - datetime.timedelta(days=today.weekday())

        _write_activity_summary(
            proc, 1, last_monday.isoformat(), distance=8000.0,
        )
        _write_activity_summary(
            proc, 2, last_wednesday.isoformat(), distance=5000.0,
        )
        _write_activity_summary(
            proc, 3, today.isoformat(), distance=3000.0,
        )
        # window_days=14 covers both `last_monday` (≤13 days ago, even
        # when today is Sunday) and today.
        out = proc.get_training_load(window_days=14)
        buckets = {
            row["week_start"]: row["miles"] for row in out["weekly_miles_trend"]
        }
        assert last_monday.isoformat() in buckets
        assert this_monday.isoformat() in buckets
        # Two same-week runs collapse into one bucket.
        assert buckets[last_monday.isoformat()] == round(
            (8000 + 5000) / 1609.34, 1
        )


# ---------------------------------------------------------------------------
# compute_cycle_and_week_stats
# ---------------------------------------------------------------------------


class TestComputeCycleAndWeekStats:
    def test_unknown_block_id_returns_none(self, proc):
        assert proc.compute_cycle_and_week_stats(
            "block_999", "2026-05-25", "2026-05-31"
        ) is None

    def test_empty_block_zero_stats(self, proc):
        """A block with no runs in range still returns the dict shape
        (the UI relies on the keys being present, not on the values)."""
        proc.create_block("Empty Block", "2026-05-01", "2026-05-31")
        blocks = proc.get_blocks()
        block_id = blocks[0]["id"]
        out = proc.compute_cycle_and_week_stats(
            block_id, "2026-05-25", "2026-05-31"
        )
        assert out is not None
        assert out["cycle"]["total_runs"] == 0
        assert out["cycle"]["total_miles"] == 0
        assert out["week"]["runs"] == 0
        assert isinstance(out["weekly_miles"], list)
        # weeks generated by get_weeks_for_block — non-empty for a
        # one-month block.
        assert len(out["weekly_miles"]) > 0

    def test_aggregates_runs_in_block(self, proc):
        """One run inside the block, inside the selected week, surfaces
        in BOTH the cycle totals and the week totals."""
        proc.create_block("Test Block", "2026-05-01", "2026-05-31")
        block_id = proc.get_blocks()[0]["id"]
        # 2026-05-27 is a Wed; week 2026-05-25 (Mon) → 2026-05-31 (Sun).
        _write_activity_summary(
            proc, 9001, "2026-05-27",
            distance=16093.4,    # 10 mi
            movingDuration=3600, # 60 min
            averageHR=150,
            elevationGain=50.0,
            calories=900,
        )
        out = proc.compute_cycle_and_week_stats(
            block_id, "2026-05-25", "2026-05-31"
        )
        assert out["cycle"]["total_runs"] == 1
        assert out["cycle"]["total_miles"] == 10.0
        assert out["cycle"]["longest_run"] == 10.0
        assert out["week"]["runs"] == 1
        assert out["week"]["miles"] == 10.0
        assert out["week"]["avg_hr"] == 150


class TestSuggestLapCategories:
    """HR-zone prefill for the paint editor. Simple by design: Rest for
    micro/walk laps, otherwise the user's own zone band for avg HR."""

    def _setup(self, proc):
        _write_json(proc.paths["user_zones"], {
            "Steady / Constant":    "145-162 bpm",
            "Hold Back / Recovery": "<145 bpm",
            "Increasing Effort":    "163-173 bpm",
        })
        laps = [
            # normal easy lap
            {"duration": 600, "movingDuration": 600, "distance": 1609.34,
             "averageRunCadence": 168, "averageHR": 140},
            # steady HR
            {"duration": 600, "movingDuration": 600, "distance": 1609.34,
             "averageRunCadence": 172, "averageHR": 155},
            # boundary micro lap (autolap blip)
            {"duration": 2, "movingDuration": 2, "distance": 5,
             "averageRunCadence": 180, "averageHR": 165},
            # walking recovery
            {"duration": 120, "movingDuration": 120, "distance": 160,
             "averageRunCadence": 110, "averageHR": 150},
            # no HR (strap dropout)
            {"duration": 600, "movingDuration": 600, "distance": 1609.34,
             "averageRunCadence": 168, "averageHR": None},
        ]
        import os as _os
        _write_json(
            _os.path.join(proc.paths["splits"], "777.json"), {"lapDTOs": laps}
        )

    def test_maps_hr_and_rest_rules(self, proc):
        self._setup(proc)
        assert proc.suggest_lap_categories(777) == [
            "Hold Back Easy",
            "Steady Effort",
            "Rest",
            "Rest",
            "Rest",
        ]
