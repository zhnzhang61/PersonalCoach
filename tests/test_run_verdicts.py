"""PR #114 — post-run verdict pool (run_verdicts.py).

Covers:
  • segments_from_laps: consecutive same-label merging, clock math.
  • label_vs_objective: sustained overshoot fires attention with a
    correct anchor; transient spikes stay ok; settle-in seconds are
    amnestied; Easy/Rest blocks excluded; gate (no matching zones).
  • easy_purity: pure run ok, impure run attention, minimum-time gate.
  • rest_recovery_drops / rest_recovery: HRR60 math, duration + entry
    HR gates, baseline band → attention.
  • lr_asymmetry: thirds math, fatigue-growth attention, coverage gate,
    missing-metric gate.
  • compute_run_verdicts loader: fired/not_fired split, unlabeled runs
    still fire L/R, attention-first ordering.
  • refit_rest_recovery_baseline: creates the model row (real
    MemoryOS), insufficient-data path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.run_verdicts import (  # noqa: E402
    EASY_LABEL,
    REST_LABEL,
    compute_run_verdicts,
    easy_purity,
    label_vs_objective,
    lr_asymmetry,
    rest_recovery,
    rest_recovery_drops,
    segments_from_laps,
)

ZONES = [
    {"name": "Hold Back / Recovery", "low": 120, "high": 140, "rpe_label": EASY_LABEL},
    {"name": "Steady / Constant", "low": 141, "high": 155, "rpe_label": "Steady Effort"},
    {"name": "Increasing Effort", "low": 156, "high": 165, "rpe_label": "Increasing Effort"},
    {"name": "Lactate Threshold", "low": 166, "high": 172, "rpe_label": "LT Effort"},
]


def _df(hr_by_window: list[tuple[int, int, float]], *, step: int = 5,
        balance=None) -> pd.DataFrame:
    """Telemetry frame from (start_sec, end_sec, hr) windows. `balance`
    is an optional same-shape list for GroundContactBalanceLeft."""
    rows = []
    for w_i, (t0, t1, hr) in enumerate(hr_by_window):
        for t in range(t0, t1, step):
            rows.append({
                "Second": t,
                "HeartRate": hr,
                "GroundContactBalanceLeft": (
                    balance[w_i] if balance is not None else np.nan
                ),
            })
    return pd.DataFrame(rows)


def _laps(durations: list[int]) -> list[dict]:
    return [{"duration": d} for d in durations]


class TestSegmentsFromLaps:
    def test_merges_consecutive_same_label(self):
        blocks = segments_from_laps(
            _laps([300, 300, 600, 120]),
            [EASY_LABEL, EASY_LABEL, "Steady Effort", REST_LABEL],
        )
        assert [b["label"] for b in blocks] == [EASY_LABEL, "Steady Effort", REST_LABEL]
        assert blocks[0] == {
            "label": EASY_LABEL, "start_sec": 0.0, "end_sec": 600.0,
            "duration_sec": 600.0,
        }
        assert blocks[1]["start_sec"] == 600.0 and blocks[1]["end_sec"] == 1200.0

    def test_missing_categories_become_none(self):
        blocks = segments_from_laps(_laps([300, 300]), ["Steady Effort"])
        assert blocks[1]["label"] is None


class TestLabelVsObjective:
    def test_sustained_overshoot_fires_attention_with_anchor(self):
        # Steady 0..1800: in-band 150 until 1300, then 170 for 500s.
        blocks = segments_from_laps(_laps([1800]), ["Steady Effort"])
        df = _df([(0, 1300, 150), (1300, 1800, 170)])
        v = label_vs_objective(blocks, ZONES, df)
        assert v["status"] == "attention"
        seg = v["data"]["segments"][0]
        # ~500s above minus smoothing lag; generous bounds.
        assert 6.5 <= seg["minutes_above"] <= 8.5
        assert v["anchor"]["start_sec"] >= 1300
        assert v["anchor"]["end_sec"] <= 1805

    def test_transient_spike_stays_ok(self):
        # 90s spike < MISMATCH_ATTENTION_MIN of sustained time.
        blocks = segments_from_laps(_laps([1800]), ["Steady Effort"])
        df = _df([(0, 900, 150), (900, 990, 172), (990, 1800, 150)])
        v = label_vs_objective(blocks, ZONES, df)
        assert v["status"] == "ok"
        assert v["anchor"] is None

    def test_settle_in_is_amnestied(self):
        # HR climbing through the first 60s would read "below band"
        # without the settle-in exclusion.
        blocks = segments_from_laps(_laps([1200]), ["Steady Effort"])
        df = _df([(0, 55, 125), (55, 1200, 150)])
        v = label_vs_objective(blocks, ZONES, df)
        assert v["data"]["segments"][0]["minutes_below"] == 0.0
        assert v["status"] == "ok"

    def test_easy_and_rest_blocks_excluded(self):
        blocks = segments_from_laps(
            _laps([600, 600]), [EASY_LABEL, REST_LABEL]
        )
        df = _df([(0, 1200, 150)])
        assert label_vs_objective(blocks, ZONES, df) is None

    def test_no_matching_zone_gates_out(self):
        blocks = segments_from_laps(_laps([600]), ["Legacy Free Text"])
        df = _df([(0, 600, 150)])
        assert label_vs_objective(blocks, ZONES, df) is None


class TestEasyPurity:
    def test_pure_easy_ok(self):
        blocks = segments_from_laps(_laps([1200]), [EASY_LABEL])
        df = _df([(0, 1200, 132)])
        v = easy_purity(blocks, ZONES, df)
        assert v["status"] == "ok"
        assert v["data"]["purity_pct"] == 100.0

    def test_impure_easy_fires_attention(self):
        # 40% of post-settle time above the ceiling.
        blocks = segments_from_laps(_laps([1200]), [EASY_LABEL])
        df = _df([(0, 700, 132), (700, 1200, 155)])
        v = easy_purity(blocks, ZONES, df)
        assert v["status"] == "attention"
        assert v["data"]["purity_pct"] < 80
        assert v["anchor"]["start_sec"] >= 690

    def test_short_easy_gates_out(self):
        blocks = segments_from_laps(_laps([300]), [EASY_LABEL])
        df = _df([(0, 300, 132)])
        assert easy_purity(blocks, ZONES, df) is None


class TestRestRecovery:
    def _interval_df(self, drop_to: float = 130) -> pd.DataFrame:
        return _df([(0, 600, 170), (600, 660, drop_to), (660, 900, 120)])

    def test_drop_math(self):
        laps = _laps([600, 300])
        cats = ["LT Effort", REST_LABEL]
        drops = rest_recovery_drops(laps, cats, self._interval_df())
        assert len(drops) == 1
        d = drops[0]
        assert d["from_label"] == "LT Effort"
        # smoothed(15s) start ≈170, at +60s ≈130 → drop ≈40.
        assert 30 <= d["drop_bpm"] <= 45

    def test_short_rest_gates_out(self):
        laps = _laps([600, 60])
        drops = rest_recovery_drops(
            laps, ["LT Effort", REST_LABEL], self._interval_df()
        )
        assert drops == []

    def test_low_entry_hr_gates_out(self):
        laps = _laps([600, 300])
        df = _df([(0, 600, 120), (600, 900, 110)])
        drops = rest_recovery_drops(laps, ["Steady Effort", REST_LABEL], df)
        assert drops == []

    def test_baseline_band_flips_attention(self):
        laps = _laps([600, 300])
        cats = ["LT Effort", REST_LABEL]
        baseline = {"mean": 45.0, "sd": 4.0, "low_warning": 37.0,
                    "high_warning": 53.0, "n_used": 8}
        v = rest_recovery(laps, cats, self._interval_df(drop_to=145), baseline=baseline)
        assert v["status"] == "attention"
        v2 = rest_recovery(laps, cats, self._interval_df(drop_to=125), baseline=baseline)
        assert v2["status"] == "ok"

    def test_no_baseline_is_ok_with_note(self):
        laps = _laps([600, 300])
        v = rest_recovery(laps, ["LT Effort", REST_LABEL], self._interval_df())
        assert v["status"] == "ok"
        assert "基线" in v["summary"]


class TestLrAsymmetry:
    def test_growing_imbalance_fires_attention(self):
        df = _df(
            [(0, 600, 150), (600, 1200, 150), (1200, 1800, 150)],
            balance=[50.4, 50.9, 51.8],
        )
        v = lr_asymmetry(df)
        assert v["status"] == "attention"
        assert v["data"]["thirds"][0]["dev"] == pytest.approx(0.4, abs=0.2)
        assert v["data"]["thirds"][2]["dev"] == pytest.approx(1.8, abs=0.2)
        assert v["data"]["side_late"] == "左"

    def test_stable_balance_ok(self):
        df = _df(
            [(0, 600, 150), (600, 1200, 150), (1200, 1800, 150)],
            balance=[50.2, 50.3, 50.2],
        )
        assert lr_asymmetry(df)["status"] == "ok"

    def test_coverage_gate(self):
        df = _df([(0, 300, 150)], balance=[50.2])
        assert lr_asymmetry(df) is None

    def test_missing_metric_gates_out(self):
        df = _df([(0, 1800, 150)])
        assert lr_asymmetry(df) is None


class TestComputeRunVerdicts:
    def _processor(self, tmp_path, *, categories, df, laps):
        dp = MagicMock()
        dp.get_run_laps.return_value = laps
        dp.get_activity_telemetry.return_value = (df, None)
        dp.get_hr_zones.return_value = ZONES
        dp.paths = {"manual": str(tmp_path)}
        if categories is not None:
            (tmp_path / "run_42_meta.json").write_text(
                json.dumps({"lap_categories": categories})
            )
        return dp

    def test_labeled_run_fires_pool_attention_first(self, tmp_path):
        laps = _laps([1200, 1800, 300])
        cats = [EASY_LABEL, "Steady Effort", REST_LABEL]
        df = _df(
            [(0, 1200, 132), (1200, 2400, 150), (2400, 3000, 170), (3000, 3300, 130)],
            balance=[50.3, 50.5, 51.9, 52.0],
        )
        dp = self._processor(tmp_path, categories=cats, df=df, laps=laps)
        out = compute_run_verdicts(dp, 42)
        keys = [v["key"] for v in out["verdicts"]]
        assert set(keys) >= {"label_vs_objective", "easy_purity", "lr_asymmetry"}
        statuses = [v["status"] for v in out["verdicts"]]
        assert statuses == sorted(statuses, key=lambda s: s != "attention")

    def test_unlabeled_run_only_lr_fires(self, tmp_path):
        laps = _laps([1800])
        df = _df(
            [(0, 600, 150), (600, 1200, 150), (1200, 1800, 150)],
            balance=[50.2, 50.3, 50.2],
        )
        dp = self._processor(tmp_path, categories=None, df=df, laps=laps)
        out = compute_run_verdicts(dp, 42)
        assert [v["key"] for v in out["verdicts"]] == ["lr_asymmetry"]
        skipped = {n["key"]: n["reason"] for n in out["not_fired"]}
        assert "本次未标注强度" in skipped["label_vs_objective"]

    def test_no_telemetry_nothing_fires(self, tmp_path):
        dp = MagicMock()
        dp.get_run_laps.return_value = _laps([600])
        dp.get_activity_telemetry.return_value = (None, None)
        out = compute_run_verdicts(dp, 42)
        assert out["verdicts"] == []
        assert len(out["not_fired"]) == 4

    def test_baseline_flows_from_memory_engine(self, tmp_path):
        laps = _laps([600, 300])
        cats = ["LT Effort", REST_LABEL]
        df = _df([(0, 600, 170), (600, 660, 140), (660, 900, 130)])
        dp = self._processor(tmp_path, categories=cats, df=df, laps=laps)
        engine = MagicMock()
        engine.get_model.return_value = {
            "params_json": {"mean": 45.0, "sd": 4.0, "low_warning": 37.0,
                            "high_warning": 53.0, "n_used": 8}
        }
        out = compute_run_verdicts(dp, 42, memory_engine=engine)
        rr = next(v for v in out["verdicts"] if v["key"] == "rest_recovery")
        assert rr["status"] == "attention"
        engine.get_model.assert_called_once_with("hrr.rest_recovery_baseline")


@pytest.fixture
def mem(tmp_path):
    from backend.cognitive_memory_engine import MemoryOS
    return MemoryOS(
        db_path=str(tmp_path / "cme.db"),
        semantic_profile_path=str(tmp_path / "sem.json"),
    )


class TestRefitRestRecoveryBaseline:
    def _dp(self, tmp_path, n_runs: int):
        from backend.data_processor import RunActivity

        dp = MagicMock()
        runs = []
        for i in range(n_runs):
            rid = i + 1
            run = MagicMock(spec=RunActivity)
            run.activity_id = rid
            runs.append(run)
            (tmp_path / f"run_{rid}_meta.json").write_text(json.dumps({
                "lap_categories": ["LT Effort", "Rest"],
            }))
        dp.list_runs.return_value = runs
        dp.get_run_laps.return_value = _laps([600, 300])
        df = _df([(0, 600, 170), (600, 660, 130), (660, 900, 120)])
        dp.get_activity_telemetry.return_value = (df, None)
        dp.paths = {"manual": str(tmp_path)}
        return dp

    def test_creates_model_row(self, mem, tmp_path):
        from backend.seed_models import refit_rest_recovery_baseline

        dp = self._dp(tmp_path, 3)
        key = refit_rest_recovery_baseline(mem, dp)
        assert key == "hrr.rest_recovery_baseline"
        model = mem.get_model(key)
        assert model["model_type"] == "mean_std"
        params = model["params_json"]
        assert params["units"] == "bpm_per_60s"
        assert params["n_used"] == 3
        assert 30 <= params["mean"] <= 45

    def test_insufficient_runs_returns_none(self, mem, tmp_path):
        from backend.seed_models import refit_rest_recovery_baseline

        dp = self._dp(tmp_path, 2)
        assert refit_rest_recovery_baseline(mem, dp) is None
