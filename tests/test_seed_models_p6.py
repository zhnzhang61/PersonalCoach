"""PR P6 — stat-derived model refit functions (batch 1).

Covers:
  • _compute_run_decoupling_pct: math correctness on synthetic
    telemetry, None-on-missing, None-on-too-few-samples,
    None-on-bad-speed.
  • _compute_run_avg_cadence: average correctness, None on missing /
    too few / implausibly-low filtering.
  • _is_aerobic_run: duration + HR thresholds, missing-HR rejection.
  • _aerobic_hr_ceiling: reads LT from profile, falls back when
    missing.
  • refit_aerobic_decoupling_baseline: creates / updates model row,
    params shape, status flips, insufficient-data path.
  • refit_cadence_baseline: same.
  • /api/memory/models/refit/{key}: routes new model_keys.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# Synthetic telemetry helper
# --------------------------------------------------------------------------


def _make_telemetry(
    n_samples: int = 200,
    *,
    hr_start: float = 140,
    hr_end: float = 160,
    speed_mps: float = 3.0,
    cadence: float = 175,
    second_step: int = 10,
) -> pd.DataFrame:
    """Build a synthetic raw-telemetry DataFrame with the columns
    `get_activity_telemetry` returns. HR ramps linearly from
    `hr_start` → `hr_end` so decoupling has a deterministic
    direction; speed and cadence are constant by default but can be
    overridden per-test."""
    rows = []
    for i in range(n_samples):
        frac = i / max(n_samples - 1, 1)
        hr = hr_start + (hr_end - hr_start) * frac
        rows.append({
            "Second": i * second_step,
            "Lap": 1,
            "Distance": (i * second_step) * speed_mps / 1609.34,
            "HeartRate": hr,
            "Speed_mps": speed_mps,
            "Cadence": cadence,
            "Elevation": 100.0,
            "StrideLength": 120,
            "RespirationRate": 35,
            "VerticalOscillation": 8.5,
            "GroundContactTime": 240,
            "GroundContactBalanceLeft": 49.5,
            "Power": 250,
            "AirTemperature": 18,
        })
    return pd.DataFrame(rows)


def _mock_run(activity_id, *, duration_s=2000, avg_hr=140, date="2026-05-15"):
    """A minimal RunActivity-like stand-in for the fields refit
    functions actually touch.

    Uses `spec=RunActivity` so attribute access matches the real
    dataclass — if a future PR adds a field to RunActivity and
    `_is_aerobic_run` starts reading it, accessing it on a bare
    MagicMock would return a truthy mock (silent pass); spec-mode
    raises AttributeError until the test sets the new field, making
    the gap visible."""
    from backend.data_processor import RunActivity

    r = MagicMock(spec=RunActivity)
    r.activity_id = activity_id
    r.duration_s = duration_s
    r.avg_hr = avg_hr
    r.date = date
    return r


# --------------------------------------------------------------------------
# Per-run helpers
# --------------------------------------------------------------------------


class TestComputeRunDecouplingPct:
    def test_returns_none_when_telemetry_missing(self):
        from backend.seed_models import _compute_run_decoupling_pct

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (None, None)
        assert _compute_run_decoupling_pct(1, dp) is None

    def test_returns_none_when_telemetry_empty(self):
        from backend.seed_models import _compute_run_decoupling_pct

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (pd.DataFrame(), None)
        assert _compute_run_decoupling_pct(1, dp) is None

    def test_positive_decoupling_when_hr_drifts_up(self):
        """HR ramps 140→160 linearly across 200 samples at constant
        Speed_mps=3.0. Analytic values for a wall-clock midpoint
        split (samples 0..99 vs 100..199):
            h1_mean_hr = 140 + (20/199) * (sum 0..99 / 100) = 144.975
            h2_mean_hr = 140 + (20/199) * (sum 100..199 / 100) = 155.025
            decoupling = (155.025 - 144.975) / 144.975 * 100 = 6.93%
        Tight tolerance — anything outside 6.85-7.0 indicates a
        boundary / off-by-one bug in the split (row-position vs
        wall-clock, inclusive vs exclusive, etc.)."""
        from backend.seed_models import _compute_run_decoupling_pct

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (
            _make_telemetry(n_samples=200, hr_start=140, hr_end=160),
            None,
        )
        result = _compute_run_decoupling_pct(1, dp)
        assert result is not None
        assert result == pytest.approx(6.93, abs=0.05)

    def test_negative_decoupling_when_hr_drops(self):
        """Going downhill or warming up properly → HR drops over
        time at the same pace → negative decoupling."""
        from backend.seed_models import _compute_run_decoupling_pct

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (
            _make_telemetry(n_samples=200, hr_start=160, hr_end=140),
            None,
        )
        result = _compute_run_decoupling_pct(1, dp)
        assert result is not None
        assert result < 0

    def test_zero_decoupling_when_hr_steady(self):
        """Constant HR + constant speed → exactly 0 decoupling."""
        from backend.seed_models import _compute_run_decoupling_pct

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (
            _make_telemetry(n_samples=200, hr_start=150, hr_end=150),
            None,
        )
        result = _compute_run_decoupling_pct(1, dp)
        assert result == pytest.approx(0.0, abs=0.001)

    def test_returns_none_below_60_valid_samples(self):
        """50 samples isn't enough — emitting one would skew the
        baseline more than skipping it."""
        from backend.seed_models import _compute_run_decoupling_pct

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (
            _make_telemetry(n_samples=50),
            None,
        )
        assert _compute_run_decoupling_pct(1, dp) is None

    def test_filters_walk_pause_samples(self):
        """Speed below 0.5 m/s should be filtered out as
        walk/pause/stoplight. Otherwise the speed denominator goes
        to ~0 and decoupling explodes."""
        from backend.seed_models import _compute_run_decoupling_pct

        df = _make_telemetry(n_samples=200, hr_start=140, hr_end=160)
        # Inject 30 stop-and-go rows in the middle
        df.loc[80:110, "Speed_mps"] = 0.1
        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (df, None)
        result = _compute_run_decoupling_pct(1, dp)
        # Should still produce a finite ~6-7% number, not nonsense.
        assert result is not None
        assert 4 < result < 10


class TestComputeRunAvgCadence:
    def test_steady_state_average(self):
        from backend.seed_models import _compute_run_avg_cadence

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (
            _make_telemetry(n_samples=200, cadence=178), None,
        )
        result = _compute_run_avg_cadence(1, dp)
        assert result == pytest.approx(178.0, abs=0.01)

    def test_filters_below_100_spm(self):
        """Cadence below 100 spm is walking / paused — exclude. A
        run that paused at lights for several samples shouldn't drag
        its averaged cadence into "walking" territory."""
        from backend.seed_models import _compute_run_avg_cadence

        df = _make_telemetry(n_samples=200, cadence=178)
        df.loc[50:80, "Cadence"] = 60  # walking-cadence pause
        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (df, None)
        result = _compute_run_avg_cadence(1, dp)
        # Should be ~178, not the weighted mean with the 60s.
        assert result == pytest.approx(178.0, abs=0.01)

    def test_returns_none_when_telemetry_missing(self):
        from backend.seed_models import _compute_run_avg_cadence

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (None, None)
        assert _compute_run_avg_cadence(1, dp) is None

    def test_returns_none_below_60_valid_samples(self):
        from backend.seed_models import _compute_run_avg_cadence

        dp = MagicMock()
        dp.get_activity_telemetry.return_value = (
            _make_telemetry(n_samples=30), None,
        )
        assert _compute_run_avg_cadence(1, dp) is None


class TestIsAerobicRun:
    def test_includes_easy_run_with_avg_hr_below_ceiling(self):
        from backend.seed_models import _is_aerobic_run

        r = _mock_run(1, duration_s=2400, avg_hr=140)
        assert _is_aerobic_run(r, hr_ceiling=160) is True

    def test_excludes_tempo_run_above_ceiling(self):
        from backend.seed_models import _is_aerobic_run

        r = _mock_run(1, duration_s=2400, avg_hr=170)
        assert _is_aerobic_run(r, hr_ceiling=160) is False

    def test_excludes_short_run(self):
        from backend.seed_models import _is_aerobic_run

        r = _mock_run(1, duration_s=600, avg_hr=140)  # 10 min
        assert _is_aerobic_run(r, hr_ceiling=160) is False

    def test_excludes_run_with_no_hr_signal(self):
        """Forgot the strap → can't tell if it was aerobic. Skip
        rather than guess — including HR-less runs in a cadence
        baseline would conflate workout types."""
        from backend.seed_models import _is_aerobic_run

        r = _mock_run(1, duration_s=2400, avg_hr=None)
        assert _is_aerobic_run(r, hr_ceiling=160) is False


class TestAerobicHrCeiling:
    def test_uses_lt_when_available(self):
        from backend.seed_models import _aerobic_hr_ceiling

        dp = MagicMock()
        dp.get_athlete_profile_full.return_value = {
            "fitness": {"lactate_threshold_hr": 170},
        }
        # 170 * 0.92 = 156.4
        # 170 * 0.92 = 156.4 exactly under float (no rounding).
        assert _aerobic_hr_ceiling(dp) == pytest.approx(156.4, abs=1e-9)

    def test_falls_back_when_lt_missing(self):
        from backend.seed_models import _aerobic_hr_ceiling

        dp = MagicMock()
        dp.get_athlete_profile_full.return_value = {"fitness": {}}
        assert _aerobic_hr_ceiling(dp) == 155.0

    def test_falls_back_when_profile_file_missing(self):
        """Fresh install: semantic profile JSON not yet written →
        FileNotFoundError. Fall back to the generic ceiling. This
        is the genuine 'profile not bootstrapped' case the narrow
        catch is meant to cover."""
        from backend.seed_models import _aerobic_hr_ceiling

        dp = MagicMock()
        dp.get_athlete_profile_full.side_effect = FileNotFoundError("no file")
        assert _aerobic_hr_ceiling(dp) == 155.0

    def test_falls_back_when_profile_key_missing(self):
        """Partially-populated profile dict raises KeyError on
        access — also a bootstrap case."""
        from backend.seed_models import _aerobic_hr_ceiling

        dp = MagicMock()
        dp.get_athlete_profile_full.side_effect = KeyError("fitness")
        assert _aerobic_hr_ceiling(dp) == 155.0

    def test_unexpected_exception_propagates(self):
        """Real failures (DB lock, schema migration error, buggy
        refactor) must NOT silently demote the ceiling — that would
        skew the baseline sample set and make the model invisibly
        wrong. Original bare `except Exception` swallowed these;
        the narrowed catch lets them surface so the operator sees
        the actual problem."""
        from backend.seed_models import _aerobic_hr_ceiling

        dp = MagicMock()
        dp.get_athlete_profile_full.side_effect = RuntimeError(
            "DB locked"
        )
        with pytest.raises(RuntimeError, match="DB locked"):
            _aerobic_hr_ceiling(dp)

    def test_falls_back_on_zero_lt(self):
        """`lactate_threshold_hr=0` (Garmin reports 0 when uncertain)
        shouldn't produce a 0 ceiling that excludes every run."""
        from backend.seed_models import _aerobic_hr_ceiling

        dp = MagicMock()
        dp.get_athlete_profile_full.return_value = {
            "fitness": {"lactate_threshold_hr": 0},
        }
        assert _aerobic_hr_ceiling(dp) == 155.0


# --------------------------------------------------------------------------
# Refit functions (E2E with real MemoryOS)
# --------------------------------------------------------------------------


@pytest.fixture
def mem(tmp_path):
    from backend.cognitive_memory_engine import MemoryOS
    return MemoryOS(
        db_path=str(tmp_path / "cme.db"),
        semantic_profile_path=str(tmp_path / "sem.json"),
    )


def _aerobic_dp_with_runs(n_runs, *, decoupling_targets=None):
    """Build a DataProcessor mock that returns n_runs aerobic runs,
    each with synthetic telemetry that produces a known decoupling
    %. `decoupling_targets` is a list of (hr_start, hr_end) tuples
    — one per run; falls back to a sensible default."""
    dp = MagicMock()
    dp.get_athlete_profile_full.return_value = {
        "fitness": {"lactate_threshold_hr": 170},
    }
    runs = [_mock_run(i + 1, duration_s=2400, avg_hr=140) for i in range(n_runs)]
    dp.list_runs.return_value = runs
    if decoupling_targets is None:
        decoupling_targets = [(140 + 5 * (i % 3), 155 + 5 * (i % 3))
                              for i in range(n_runs)]

    telemetry_by_id = {
        i + 1: (_make_telemetry(n_samples=200,
                                hr_start=hs, hr_end=he,
                                cadence=174 + (i % 5)),
                None)
        for i, (hs, he) in enumerate(decoupling_targets)
    }
    dp.get_activity_telemetry.side_effect = lambda aid: telemetry_by_id.get(
        aid, (None, None)
    )
    return dp


class TestRefitAerobicDecouplingBaseline:
    def test_skips_when_insufficient_data(self, mem):
        from backend.seed_models import refit_aerobic_decoupling_baseline

        dp = _aerobic_dp_with_runs(2)  # < 3 minimum
        assert refit_aerobic_decoupling_baseline(mem, dp) is None
        assert mem.get_model("aerobic.decoupling_baseline") is None

    def test_creates_model_with_correct_shape(self, mem):
        from backend.seed_models import refit_aerobic_decoupling_baseline

        dp = _aerobic_dp_with_runs(5)
        key = refit_aerobic_decoupling_baseline(mem, dp)
        assert key == "aerobic.decoupling_baseline"

        got = mem.get_model(key)
        assert got["model_type"] == "mean_std"
        assert got["derivation_method"] == "stat"
        assert got["status"] == "Forming"  # n=5 < 8
        assert got["confidence"] == "low"  # 5 < 8
        assert got["n_samples"] == 5
        params = got["params_json"]
        # At n=5 < 7 the warning bands are intentionally suppressed
        # (see _compute_baseline_params docstring) — mean/sd/n_used
        # always present, low/high_warning only at n >= 7.
        assert {"mean", "sd", "n_used", "units",
                "aerobic_hr_ceiling_used", "lookback_days"} <= set(params.keys())
        assert "low_warning" not in params
        assert "high_warning" not in params
        assert params["units"] == "percent"
        assert params["n_used"] == 5
        assert params["aerobic_hr_ceiling_used"] == pytest.approx(156.4, abs=1e-9)
        # Decoupling values should be positive (HR ramps up in synthetic data)
        assert params["mean"] > 0

    def test_warning_bands_appear_at_n_7(self, mem):
        """Warning bands materialize as soon as n_used hits the
        suppression floor (7). Pin both directions: absent at 6,
        present at 7."""
        from backend.seed_models import refit_aerobic_decoupling_baseline

        dp = _aerobic_dp_with_runs(6)
        refit_aerobic_decoupling_baseline(mem, dp)
        got6 = mem.get_model("aerobic.decoupling_baseline")
        assert "low_warning" not in got6["params_json"]
        assert "high_warning" not in got6["params_json"]

        dp = _aerobic_dp_with_runs(7)
        refit_aerobic_decoupling_baseline(mem, dp)
        got7 = mem.get_model("aerobic.decoupling_baseline")
        assert "low_warning" in got7["params_json"]
        assert "high_warning" in got7["params_json"]
        assert got7["params_json"]["low_warning"] < got7["params_json"]["mean"]
        assert got7["params_json"]["high_warning"] > got7["params_json"]["mean"]

    def test_status_flips_stable_at_8_samples(self, mem):
        from backend.seed_models import refit_aerobic_decoupling_baseline

        dp = _aerobic_dp_with_runs(8)
        refit_aerobic_decoupling_baseline(mem, dp)
        got = mem.get_model("aerobic.decoupling_baseline")
        assert got["status"] == "Stable"
        assert got["confidence"] == "medium"

    def test_excludes_tempo_run_from_baseline(self, mem):
        """A tempo run (avg_hr above the ceiling) should NOT
        contribute even if its telemetry is good. Without this,
        tempo decoupling would skew the easy-baseline downward."""
        from backend.seed_models import refit_aerobic_decoupling_baseline

        dp = _aerobic_dp_with_runs(4)
        # Mark run 3 as tempo
        dp.list_runs.return_value[2].avg_hr = 175  # > 156.4 ceiling
        key = refit_aerobic_decoupling_baseline(mem, dp)
        assert key is not None
        got = mem.get_model(key)
        # n=3, not 4 — tempo was filtered out.
        assert got["n_samples"] == 3
        # And only the qualifying activity_ids land in evidence.
        assert 3 not in got["evidence_json"]["activity_ids"]

    def test_second_call_updates_in_place(self, mem):
        from backend.seed_models import refit_aerobic_decoupling_baseline

        dp = _aerobic_dp_with_runs(4)
        refit_aerobic_decoupling_baseline(mem, dp)
        first = mem.get_model("aerobic.decoupling_baseline")

        dp = _aerobic_dp_with_runs(10)
        refit_aerobic_decoupling_baseline(mem, dp)
        second = mem.get_model("aerobic.decoupling_baseline")

        # Same model_id — updated in place, not duplicated.
        assert first["model_id"] == second["model_id"]
        assert second["n_samples"] == 10
        assert second["status"] == "Stable"


class TestRefitCadenceBaseline:
    def test_skips_when_insufficient_data(self, mem):
        from backend.seed_models import refit_cadence_baseline

        dp = _aerobic_dp_with_runs(2)
        assert refit_cadence_baseline(mem, dp) is None

    def test_creates_model_with_correct_shape(self, mem):
        from backend.seed_models import refit_cadence_baseline

        dp = _aerobic_dp_with_runs(5)
        key = refit_cadence_baseline(mem, dp)
        assert key == "cadence.baseline"
        got = mem.get_model(key)
        params = got["params_json"]
        assert params["units"] == "spm"
        # All 5 runs have cadence in [174, 178] → mean should fall there
        assert 174 <= params["mean"] <= 178

    def test_excludes_runs_without_hr_signal(self, mem):
        """No HR strap = can't classify as aerobic → skip. Sanity:
        cadence baseline shouldn't include workouts the user might
        have done at any effort."""
        from backend.seed_models import refit_cadence_baseline

        dp = _aerobic_dp_with_runs(5)
        # Strap run 2 — strip its avg_hr
        dp.list_runs.return_value[1].avg_hr = None
        refit_cadence_baseline(mem, dp)
        got = mem.get_model("cadence.baseline")
        assert got["n_samples"] == 4


# --------------------------------------------------------------------------
# Refit endpoint registry
# --------------------------------------------------------------------------


class TestRefitEndpoint:
    def test_routes_new_decoupling_key_returns_422_on_no_data(self, client):
        """POST /api/memory/models/refit/aerobic.decoupling_baseline
        must reach refit_aerobic_decoupling_baseline (not 404). With
        no runs in scope, the registered fn returns None →
        endpoint emits 422 (distinct from 200, so cron / curl -f
        can alert). Body retains the ok/reason shape for human
        parsing."""
        import backend.api_server as api_server

        api_server.processor.list_runs.return_value = []
        resp = client.post("/api/memory/models/refit/aerobic.decoupling_baseline")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["ok"] is False
        assert detail["reason"] == "insufficient_data"

    def test_routes_new_cadence_key_returns_422_on_no_data(self, client):
        import backend.api_server as api_server

        api_server.processor.list_runs.return_value = []
        resp = client.post("/api/memory/models/refit/cadence.baseline")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["ok"] is False

    def test_hrv_baseline_also_returns_422_on_no_data(self, client):
        """Regression-pin the original HRV key — it's in the same
        registry now, must obey the same insufficient_data contract."""
        import backend.api_server as api_server

        api_server.processor.get_health_stats.return_value = []
        resp = client.post("/api/memory/models/refit/recovery.hrv_14d_baseline")
        assert resp.status_code == 422

    def test_unknown_key_still_404s(self, client):
        """Adding new keys shouldn't accidentally widen the route to
        accept arbitrary model_keys."""
        resp = client.post("/api/memory/models/refit/nonsense.key")
        assert resp.status_code == 404
        # 404 detail should now list all known keys including the
        # two new ones.
        detail = resp.json()["detail"]
        assert "aerobic.decoupling_baseline" in detail
        assert "cadence.baseline" in detail
        assert "recovery.hrv_14d_baseline" in detail

    def test_registry_is_module_level_importable(self):
        """A future nightly cron will `from backend.api_server import
        REFIT_REGISTRY` and iterate it. Without module-scope, the
        dict is trapped in the handler's local frame and the cron
        can't see it. Pin the import path."""
        from backend.api_server import REFIT_REGISTRY

        assert "recovery.hrv_14d_baseline" in REFIT_REGISTRY
        assert "aerobic.decoupling_baseline" in REFIT_REGISTRY
        assert "cadence.baseline" in REFIT_REGISTRY
        # Values are callables (the refit functions themselves).
        for fn in REFIT_REGISTRY.values():
            assert callable(fn)
