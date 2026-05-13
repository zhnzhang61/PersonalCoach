"""Unit tests for data_processor.py — the project's data layer.

Per the Phase 3 plan in docs/IMPROVEMENTS.md: `data_processor.py` had
zero direct tests despite being the layer everything else reads
through (the agent's MCP tools all wrap api_server endpoints, which
all delegate to DataProcessor). That's the project's biggest "running
blind" risk; this file is the first sweep at it.

Scope of this PR:
  • RunActivity dataclass — Garmin-dict parsing + derived props
  • ManualActivity dataclass — round-trip serialization
  • _bucket_run_surface module helper
  • DataProcessor bootstrap on tmp_path (no real data/ touched)
  • Semantic memory CRUD
  • Training blocks CRUD + validation
  • Manual activity CRUD
  • calculate_category_stats — perceived-stream derivation
  • compute_telemetry_summary — pandas pure function

Deliberately OUT of scope (later Phase 3 PRs):
  • Garmin-file-dependent paths: compile_health_ledger, get_hr_zones,
    get_athlete_profile_full, get_readiness, get_training_load,
    compute_cycle_and_week_stats, telemetry/laps/route/weather IO
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from data_processor import (
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
