"""PR P6 batch 2 — sleep debt + weekly volume trend models.

Covers:
  • refit_sleep_debt_14d: insufficient-data threshold (n<7),
    create shape, debt accounting math, null-filter, in-place update.
  • _bucket_mileage_by_iso_week: ISO weeks aggregate correctly,
    cross-year boundary, missing-data robustness.
  • refit_cycle_weekly_volume_diff: insufficient-data (n<3), trend
    slope/r2 math, zero-volume degenerate, in-place update.
  • /api/memory/models/refit/{key}: both new keys routed; 422 on
    insufficient_data; module-level REFIT_REGISTRY contains them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def mem(tmp_path):
    from backend.cognitive_memory_engine import MemoryOS
    return MemoryOS(
        db_path=str(tmp_path / "cme.db"),
        semantic_profile_path=str(tmp_path / "sem.json"),
    )


# --------------------------------------------------------------------------
# sleep.debt_14d
# --------------------------------------------------------------------------


def _ledger_with_sleep(sleep_hours_list: list[float | None]) -> list[dict]:
    """Build a health-stats list of (date, sleep_hours) rows from
    a list. Dates count up from 2026-05-01."""
    rows = []
    for i, h in enumerate(sleep_hours_list):
        rows.append({
            "date": f"2026-05-{i + 1:02d}",
            "sleep_hours": h,
            "rhr": 50, "hrv": 70, "stress": 20,
            "run_miles": 0, "run_mins": 0,
            "sleep_score": 80,
        })
    return rows


class TestRefitSleepDebt14d:
    def test_skips_below_7_qualifying_nights(self, mem):
        from backend.seed_models import refit_sleep_debt_14d

        dp = MagicMock()
        # 14 rows but 8 are null → only 6 qualify
        sleep = [7.5, None, 7.0, None, 8.0, None, 7.5,
                 None, 6.5, None, 7.0, None, None, None]
        dp.get_health_stats.return_value = _ledger_with_sleep(sleep)
        assert refit_sleep_debt_14d(mem, dp) is None
        assert mem.get_model("sleep.debt_14d") is None

    def test_creates_model_with_debt_fields(self, mem):
        from backend.seed_models import refit_sleep_debt_14d

        dp = MagicMock()
        # 14 nights, 12 valid, mix of below/above target (target=8).
        # Below: 6.5×3, 7.0×2, 7.5×2 → debt = 1.5+1.5+1.5+1.0+1.0+0.5+0.5 = 7.5
        # Above: 8.0×3, 8.5×2 → debt 0
        sleep = [6.5, 6.5, 6.5, 7.0, 7.0, 7.5, 7.5,
                 8.0, 8.0, 8.0, 8.5, 8.5, None, None]
        dp.get_health_stats.return_value = _ledger_with_sleep(sleep)
        key = refit_sleep_debt_14d(mem, dp)
        assert key == "sleep.debt_14d"
        got = mem.get_model(key)
        assert got["model_type"] == "mean_std"
        assert got["derivation_method"] == "stat"
        assert got["n_samples"] == 12

        params = got["params_json"]
        assert params["units"] == "hours"
        assert params["target_hours"] == 8.0
        assert params["window_days"] == 14
        # Debt: 3×1.5 + 2×1.0 + 2×0.5 = 4.5 + 2.0 + 1.0 = 7.5.
        # Field name is "_in_window" (not "_14d") because the sum
        # is over `n_samples` measured nights, not over the full
        # 14-day window. With n=12 here both happen to nearly
        # coincide, but the contract is the n_samples sum.
        assert params["total_debt_hours_in_window"] == pytest.approx(
            7.5, abs=0.01
        )
        # Nights below target = 7 (6.5×3 + 7.0×2 + 7.5×2)
        assert params["nights_below_target_in_window"] == 7
        # Invariant — total_debt equals exactly the
        # sum(max(0, target - s) for s in samples). Pin this so a
        # future refactor that re-derives in a different basis
        # (e.g. extrapolates to a 14-night equivalent) regresses
        # visibly.
        sleep = [6.5, 6.5, 6.5, 7.0, 7.0, 7.5, 7.5,
                 8.0, 8.0, 8.0, 8.5, 8.5]
        assert params["total_debt_hours_in_window"] == pytest.approx(
            sum(max(0.0, 8.0 - s) for s in sleep), abs=0.01
        )
        # n>=7 → warning bands present
        assert "low_warning" in params
        assert "high_warning" in params

    def test_filters_zero_hours_as_no_watch_worn(self, mem):
        """Garmin reports 0 when the watch wasn't worn — distinct
        from "actually slept 0 hours". Skip rather than poison the
        mean with a 0."""
        from backend.seed_models import refit_sleep_debt_14d

        dp = MagicMock()
        # 10 nights, 8 valid + 2 zeros
        sleep = [7.5, 7.0, 7.5, 0.0, 8.0, 7.5, 7.0, 0.0, 7.5, 8.0,
                 None, None, None, None]
        dp.get_health_stats.return_value = _ledger_with_sleep(sleep)
        refit_sleep_debt_14d(mem, dp)
        got = mem.get_model("sleep.debt_14d")
        # Zeros filtered → 8 valid nights, mean is over the 8
        assert got["n_samples"] == 8
        # Mean of [7.5, 7.0, 7.5, 8.0, 7.5, 7.0, 7.5, 8.0] = 7.5
        assert got["params_json"]["mean"] == pytest.approx(7.5, abs=0.01)

    def test_no_debt_when_all_above_target(self, mem):
        from backend.seed_models import refit_sleep_debt_14d

        dp = MagicMock()
        sleep = [8.0, 8.5, 9.0, 8.0, 8.5, 9.0, 8.5, 8.0] + [None] * 6
        dp.get_health_stats.return_value = _ledger_with_sleep(sleep)
        refit_sleep_debt_14d(mem, dp)
        got = mem.get_model("sleep.debt_14d")
        assert got["params_json"]["total_debt_hours_in_window"] == 0.0
        assert got["params_json"]["nights_below_target_in_window"] == 0

    def test_second_call_updates_in_place(self, mem):
        from backend.seed_models import refit_sleep_debt_14d

        dp = MagicMock()
        dp.get_health_stats.return_value = _ledger_with_sleep(
            [7.0] * 8 + [None] * 6
        )
        refit_sleep_debt_14d(mem, dp)
        first = mem.get_model("sleep.debt_14d")

        # Different data → updates in place, same model_id.
        dp.get_health_stats.return_value = _ledger_with_sleep(
            [8.0] * 12 + [None] * 2
        )
        refit_sleep_debt_14d(mem, dp)
        second = mem.get_model("sleep.debt_14d")
        assert first["model_id"] == second["model_id"]
        assert second["n_samples"] == 12
        assert second["params_json"]["nights_below_target_in_window"] == 0


# --------------------------------------------------------------------------
# cycle.weekly_volume_diff
# --------------------------------------------------------------------------


def _ledger_with_miles(daily_miles: list[float], start_date="2026-04-06"):
    """Build a ledger spanning daily_miles, starting at `start_date`
    (Monday by default → clean ISO week boundaries)."""
    import datetime
    rows = []
    d = datetime.date.fromisoformat(start_date)
    for miles in daily_miles:
        rows.append({
            "date": d.isoformat(),
            "sleep_hours": 7.5, "rhr": 50, "hrv": 70, "stress": 20,
            "run_miles": miles, "run_mins": 0,
            "sleep_score": 80,
        })
        d += datetime.timedelta(days=1)
    return rows


class TestBucketMileageByIsoWeek:
    def test_aggregates_within_week(self):
        from backend.seed_models import _bucket_mileage_by_iso_week

        # 2026-04-06 is Monday of ISO week 15. 5 days of 3mi each = 15mi.
        rows = _ledger_with_miles([3, 3, 3, 3, 3])
        weeks = _bucket_mileage_by_iso_week(rows)
        assert len(weeks) == 1
        assert weeks[0][1] == "2026-W15"
        assert weeks[0][2] == 15.0

    def test_splits_across_weeks(self):
        from backend.seed_models import _bucket_mileage_by_iso_week

        # 14 days from Monday → 2 full ISO weeks
        rows = _ledger_with_miles([3.0] * 14)
        weeks = _bucket_mileage_by_iso_week(rows)
        assert len(weeks) == 2
        assert weeks[0][2] == 21.0
        assert weeks[1][2] == 21.0
        # Sequential index 0, 1
        assert weeks[0][0] == 0
        assert weeks[1][0] == 1

    def test_skips_rows_missing_miles(self):
        """A health-ledger row with `run_miles=None` (very old days
        before the field existed) shouldn't crash the bucketer."""
        from backend.seed_models import _bucket_mileage_by_iso_week

        rows = _ledger_with_miles([3, 3, 3])
        rows[1]["run_miles"] = None
        weeks = _bucket_mileage_by_iso_week(rows)
        # Only 2 valid days; 1 week
        assert weeks[0][2] == 6.0

    def test_skips_invalid_date(self):
        """Defensive — a hand-edited row with non-ISO date shouldn't
        explode the whole bucket."""
        from backend.seed_models import _bucket_mileage_by_iso_week

        rows = _ledger_with_miles([3, 3])
        rows.append({
            "date": "not-a-date", "run_miles": 5,
            "sleep_hours": 7, "rhr": 50, "hrv": 70, "stress": 20,
            "run_mins": 0, "sleep_score": 80,
        })
        weeks = _bucket_mileage_by_iso_week(rows)
        assert weeks[0][2] == 6.0  # bad row dropped

    def test_aggregates_across_iso_year_boundary(self):
        """2025-12-29 is Monday of ISO 2026-W01 (NOT 2025-W53). The
        bucketer must group it with Jan 5 2026 sequentially and
        assign a continuous week_index across the year change. A
        refactor that swapped `isocalendar()` for `strftime('%U')`
        would correctly aggregate within each week but mis-order
        the cross-year sequence — regression test for that."""
        import datetime
        from backend.seed_models import _bucket_mileage_by_iso_week

        rows = []
        d = datetime.date.fromisoformat("2025-12-29")
        for _ in range(14):
            rows.append({
                "date": d.isoformat(),
                "sleep_hours": 7, "rhr": 50, "hrv": 70,
                "stress": 20, "run_miles": 3.0, "run_mins": 0,
                "sleep_score": 80,
            })
            d += datetime.timedelta(days=1)
        weeks = _bucket_mileage_by_iso_week(rows)
        assert len(weeks) == 2
        assert weeks[0][1] == "2026-W01"
        assert weeks[1][1] == "2026-W02"
        # Sequential index continuous across the year change.
        assert weeks[0][0] == 0
        assert weeks[1][0] == 1

    def test_zero_mile_rest_week_keeps_bucket(self):
        """A rest week (intentional, miles=0) should produce a
        0-bucket, NOT be skipped. The bucketer filters miles=None
        (data gap) but keeps miles=0 (real observation).

        Pin: without this, a refactor that swapped `miles is None`
        for `not miles` (truthy check) would silently drop rest
        weeks, collapsing a `30 → 0 → 0 → 25` taper-then-comeback
        into a `30 → 25` two-point regression with wildly different
        slope."""
        from backend.seed_models import _bucket_mileage_by_iso_week

        # Three full weeks starting on a Monday.
        rows = _ledger_with_miles(
            [3.0] * 7 + [0.0] * 7 + [3.0] * 7,
            start_date="2026-01-05",
        )
        weeks = _bucket_mileage_by_iso_week(rows)
        assert len(weeks) == 3
        assert weeks[0][2] == 21.0
        assert weeks[1][2] == 0.0  # rest week present, not dropped
        assert weeks[2][2] == 21.0


class TestRefitCycleWeeklyVolumeDiff:
    def test_skips_below_3_weeks(self, mem):
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        # 1 week of data
        dp.get_health_stats.return_value = _ledger_with_miles(
            [3.0] * 7, start_date="2026-01-05",
        )
        assert refit_cycle_weekly_volume_diff(mem, dp) is None
        assert mem.get_model("cycle.weekly_volume_diff") is None

    def test_skips_at_exactly_2_weeks(self, mem):
        """Pin both sides of the n >= 3 boundary so a regression that
        tightens to `len < 4` or loosens to `len < 2` regresses
        visibly — same shape as PR #88's `test_warning_bands_appear_
        at_n_7` boundary pin."""
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        dp.get_health_stats.return_value = _ledger_with_miles(
            [3.0] * 14, start_date="2026-01-05",
        )
        assert refit_cycle_weekly_volume_diff(mem, dp) is None

    def test_creates_model_with_positive_slope(self, mem):
        """Mileage ramps 20 → 25 → 30 → 35 over 4 weeks → slope=5/wk,
        weekly_change_pct = (5 / 27.5) * 100 = ~18%, r² = 1.0 (perfect
        linear)."""
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        miles_per_day_by_week = [20 / 7, 25 / 7, 30 / 7, 35 / 7]
        all_miles = []
        for w in miles_per_day_by_week:
            all_miles.extend([w] * 7)
        dp.get_health_stats.return_value = _ledger_with_miles(all_miles)
        key = refit_cycle_weekly_volume_diff(mem, dp)
        assert key == "cycle.weekly_volume_diff"
        got = mem.get_model(key)
        assert got["model_type"] == "linear_trend"
        assert got["n_samples"] == 4
        params = got["params_json"]
        assert params["units"] == "miles_per_week"
        # Tolerances tightened to match storage precision (slope /
        # r²: round(_, 2); weekly_change_pct: round(_, 1)) — see
        # PR #88 lesson on loose-abs hiding boundary regressions.
        assert params["slope"] == pytest.approx(5.0, abs=0.01)
        # Perfect linear ramp → r² = 1.0
        assert params["r2"] == pytest.approx(1.0, abs=0.01)
        # Mean = 27.5, slope = 5 → 18.18%
        assert params["weekly_change_pct"] == pytest.approx(18.2, abs=0.1)
        assert len(params["weeks_used"]) == 4
        # Weekly mileage stored at round(_, 2) — match.
        assert params["weeks_used"][0]["miles"] == pytest.approx(20.0, abs=0.01)
        assert params["weeks_used"][-1]["miles"] == pytest.approx(35.0, abs=0.01)

    def test_negative_slope_when_tapering(self, mem):
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        # 30 → 24 → 18 → 12 → 6 (5 weeks)
        targets = [30, 24, 18, 12, 6]
        all_miles = []
        for total in targets:
            all_miles.extend([total / 7] * 7)
        dp.get_health_stats.return_value = _ledger_with_miles(all_miles)
        refit_cycle_weekly_volume_diff(mem, dp)
        got = mem.get_model("cycle.weekly_volume_diff")
        # Storage rounds slope to 2 decimals, pct to 1 decimal — match.
        assert got["params_json"]["slope"] == pytest.approx(-6.0, abs=0.01)
        # Mean = 18, slope = -6 → -33.3%
        assert got["params_json"]["weekly_change_pct"] == pytest.approx(
            -33.3, abs=0.1
        )

    def test_zero_volume_degenerate(self, mem):
        """No running at all across the window. Should still emit
        a model (slope=0, r2=1.0 conventionally) rather than fail —
        a coach reading 'no running for 3 weeks' is valid signal."""
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        dp.get_health_stats.return_value = _ledger_with_miles([0.0] * 21)
        key = refit_cycle_weekly_volume_diff(mem, dp)
        assert key == "cycle.weekly_volume_diff"
        got = mem.get_model(key)
        assert got["params_json"]["slope"] == 0.0
        assert got["params_json"]["r2"] == 1.0
        assert got["params_json"]["weekly_change_pct"] == 0.0

    def test_caps_at_volume_weeks_window(self, mem):
        """Only the most recent _VOLUME_WEEKS=6 weeks contribute.

        Uses ISO 2026-W02 as the start so the entire 10-week window
        sits well before today (2026-W22+) — keeps the test
        deterministic across calendar dates without mocking
        date.today() and unaffected by the current-week filter
        (no overlap with today's ISO week)."""
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        # 10 weeks of data → only last 6 should be used
        miles_each_day = []
        for week_miles in range(10, 20):  # 10, 11, ..., 19
            miles_each_day.extend([week_miles / 7] * 7)
        dp.get_health_stats.return_value = _ledger_with_miles(
            miles_each_day, start_date="2026-01-05"
        )
        refit_cycle_weekly_volume_diff(mem, dp)
        got = mem.get_model("cycle.weekly_volume_diff")
        assert got["n_samples"] == 6
        # Most recent 6 weeks = 14, 15, 16, 17, 18, 19 → slope = 1.0.
        # Tightened tolerance to abs=0.01 to match the production
        # code's round(slope, 2) storage precision (per PR #88
        # lesson — loose abs hides off-by-one regressions).
        assert got["params_json"]["slope"] == pytest.approx(1.0, abs=0.01)

    def test_constant_non_zero_volume_r2_fallback(self, mem):
        """Steady 25 mi/wk for 6 weeks. linear_regression returns
        slope=0 + ss_tot=0 → r2 falls through the `else 1.0` branch.
        Pin both that the model still publishes AND that r²=1.0 (a
        perfectly steady runner has perfect "no trend" fit).

        Regression target: a refactor that drops the `if ss_tot > 0`
        guard would ZeroDivisionError on every steady-state runner.
        Without this test, the failure mode only surfaces in
        production."""
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        # 6 weeks of 25 mi/wk = 150mi total, well clear of the
        # current-iso-week filter (start in 2026-W02).
        dp.get_health_stats.return_value = _ledger_with_miles(
            [25 / 7] * 42, start_date="2026-01-05",
        )
        refit_cycle_weekly_volume_diff(mem, dp)
        got = mem.get_model("cycle.weekly_volume_diff")
        assert got is not None
        assert got["params_json"]["slope"] == pytest.approx(0.0, abs=0.01)
        assert got["params_json"]["r2"] == pytest.approx(1.0, abs=0.01)
        assert got["params_json"]["weekly_change_pct"] == 0.0

    def test_low_volume_pct_floored_at_5mi(self, mem):
        """Return-from-injury regime: weeks [0.5, 1.0, 1.0, 1.5,
        1.5, 2.0] mi. Mean ≈ 1.25, slope ≈ 0.27. Without the
        denominator floor at 5 mi/wk, weekly_change_pct would
        explode to ~21.6% — agent reads it as "ramping fast, deload"
        when the absolute change is clinically irrelevant.

        With the _LOW_VOLUME_FLOOR_MI=5 floor, the denominator
        clamps to 5 → pct = 0.27 / 5 * 100 = ~5.4%. The slope
        itself is unchanged (raw absolute is the honest signal at
        low volume; pct is the safety net)."""
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        # 6 weeks, totals [0.5, 1.0, 1.0, 1.5, 1.5, 2.0] mi.
        # Spread each total evenly over 7 days.
        targets = [0.5, 1.0, 1.0, 1.5, 1.5, 2.0]
        all_miles = []
        for total in targets:
            all_miles.extend([total / 7] * 7)
        dp.get_health_stats.return_value = _ledger_with_miles(
            all_miles, start_date="2026-01-05",
        )
        refit_cycle_weekly_volume_diff(mem, dp)
        got = mem.get_model("cycle.weekly_volume_diff")
        # Slope still ~0.27 mi/wk (un-floored — absolute is honest).
        assert got["params_json"]["slope"] == pytest.approx(0.27, abs=0.05)
        # pct now floored: 0.27 / 5.0 * 100 ≈ 5.4% (not 21.6%).
        assert got["params_json"]["weekly_change_pct"] == pytest.approx(
            5.4, abs=0.5
        )

    def test_drops_current_iso_week_from_regression(self, mem, monkeypatch):
        """The current (in-progress) ISO week must NOT contribute
        to the regression — otherwise a Monday-morning refit on a
        steady 25mi/wk runner produces a misleading downward slope
        because the current week is `0` (no run logged yet today).

        We mock date.today() to a known Monday so the test data
        deterministically straddles "completed" vs "current" weeks."""
        import datetime as _dt
        from backend import seed_models

        # Pin "today" to Monday 2026-02-09 (ISO 2026-W07). Build
        # 6 full weeks of 25 mi (W01-W06) plus 1 row at today's
        # date carrying 0 miles (mimics the cron firing on a fresh
        # Monday). Without the drop-current-week filter, slope
        # would swing negative; with the filter, slope stays 0.
        class FakeDate(_dt.date):
            @classmethod
            def today(cls):
                return _dt.date(2026, 2, 9)
        monkeypatch.setattr(seed_models, "date", FakeDate)

        from backend.seed_models import refit_cycle_weekly_volume_diff

        # 6 completed weeks (W01-W06: 2025-12-29 .. 2026-02-08)
        # + 1 day at today (2026-02-09, W07, 0 miles).
        rows = _ledger_with_miles(
            [25 / 7] * 42 + [0.0], start_date="2025-12-29",
        )
        dp = MagicMock()
        dp.get_health_stats.return_value = rows
        refit_cycle_weekly_volume_diff(mem, dp)
        got = mem.get_model("cycle.weekly_volume_diff")
        assert got is not None
        # Slope should reflect 6 completed weeks of steady 25 → 0
        # (NOT slope downward toward the partial 0-mile current week).
        assert got["params_json"]["slope"] == pytest.approx(0.0, abs=0.01)
        # Evidence shouldn't include the current week's label.
        assert "2026-W07" not in got["evidence_json"]["weeks"]

    def test_second_call_updates_in_place(self, mem):
        from backend.seed_models import refit_cycle_weekly_volume_diff

        dp = MagicMock()
        dp.get_health_stats.return_value = _ledger_with_miles(
            [3.0] * 28, start_date="2026-01-05",
        )
        refit_cycle_weekly_volume_diff(mem, dp)
        first = mem.get_model("cycle.weekly_volume_diff")

        dp.get_health_stats.return_value = _ledger_with_miles(
            [5.0] * 28, start_date="2026-01-05",
        )
        refit_cycle_weekly_volume_diff(mem, dp)
        second = mem.get_model("cycle.weekly_volume_diff")
        assert first["model_id"] == second["model_id"]


# --------------------------------------------------------------------------
# Endpoint registry
# --------------------------------------------------------------------------


class TestRefitEndpointBatch2:
    def test_sleep_debt_routes_and_422_on_no_data(self, client):
        import backend.api_server as api_server

        api_server.processor.get_health_stats.return_value = []
        resp = client.post("/api/memory/models/refit/sleep.debt_14d")
        assert resp.status_code == 422
        # Pin BOTH halves of the body contract — a refactor that
        # dropped `ok` or flipped it to True would break any client
        # branching on `detail.ok`.
        detail = resp.json()["detail"]
        assert detail["ok"] is False
        assert detail["reason"] == "insufficient_data"

    def test_cycle_volume_routes_and_422_on_no_data(self, client):
        import backend.api_server as api_server

        api_server.processor.get_health_stats.return_value = []
        resp = client.post("/api/memory/models/refit/cycle.weekly_volume_diff")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["ok"] is False
        assert detail["reason"] == "insufficient_data"

    def test_registry_includes_new_keys(self):
        """Module-level REFIT_REGISTRY pinned to include both new
        keys (so a future cron iterating it picks them up)."""
        from backend.api_server import REFIT_REGISTRY

        assert "sleep.debt_14d" in REFIT_REGISTRY
        assert "cycle.weekly_volume_diff" in REFIT_REGISTRY
        # And the previous 3 still there — no regression.
        assert "recovery.hrv_14d_baseline" in REFIT_REGISTRY
        assert "aerobic.decoupling_baseline" in REFIT_REGISTRY
        assert "cadence.baseline" in REFIT_REGISTRY

    def test_unknown_404_lists_all_5_keys(self, client):
        resp = client.post("/api/memory/models/refit/nonsense.key")
        assert resp.status_code == 404
        detail = resp.json()["detail"]
        for key in (
            "recovery.hrv_14d_baseline",
            "aerobic.decoupling_baseline",
            "cadence.baseline",
            "sleep.debt_14d",
            "cycle.weekly_volume_diff",
        ):
            assert key in detail
