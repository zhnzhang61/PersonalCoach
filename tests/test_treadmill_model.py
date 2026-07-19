"""treadmill_model: rolling-window fit, Rest/label hygiene, cache
staleness, curve prediction, and endpoint status mapping.

Synthetic world: outdoor runs whose laps follow a known linear stride
law `stride = 0.10 + 0.003*cad + 0.002*hr` (no temp/time dependence).
The fit should recover that mapping closely enough that predicting a
constant-cadence/HR treadmill curve integrates to the analytic
distance. Rest laps carry ABSURD stride values on purpose — if they
ever leak into training, the distance assertions blow up loudly.
"""

from __future__ import annotations

import datetime
import json
import os
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from backend import treadmill_model as tm
from backend.data_processor import DataProcessor


def true_stride(cad: float, hr: float) -> float:
    return 0.10 + 0.003 * cad + 0.002 * hr


def _write(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f)


@pytest.fixture()
def proc(tmp_path):
    p = DataProcessor(data_dir=str(tmp_path / "data"))
    today = datetime.date.today()
    summaries = []
    rng = np.random.default_rng(42)
    for k in range(30):
        rid = 9_000_000 + k
        date = today - datetime.timedelta(days=2 + k * 2)  # within 150d
        laps, cats = [], []
        for j in range(8):
            if j == 4:
                # Poisoned Rest lap: stride 3.0 m would wreck the fit if
                # it ever entered training.
                cad, hr, stride = 150.0, 150.0, 3.0
                cats.append("Rest")
            else:
                cad = float(rng.uniform(160, 182))
                hr = float(rng.uniform(138, 172))
                stride = true_stride(cad, hr)
                cats.append("Steady Effort")
            speed = cad * stride / 60  # m/s
            duration = 600.0
            laps.append(
                {
                    "lapIndex": j + 1,
                    "duration": duration,
                    "movingDuration": duration,
                    "distance": speed * duration,
                    "averageRunCadence": cad,
                    "averageHR": hr,
                    "averageSpeed": speed,
                    "avgGradeAdjustedSpeed": speed,
                }
            )
        summaries.append(
            {
                "activityId": rid,
                "activityName": f"Synth {k}",
                "activityType": {"typeKey": "running"},
                "startTimeLocal": f"{date.isoformat()} 07:00:00",
                "distance": sum(l["distance"] for l in laps),
            }
        )
        _write(os.path.join(p.paths["splits"], f"{rid}.json"), {"lapDTOs": laps})
        _write(
            os.path.join(p.paths["manual"], f"run_{rid}_meta.json"),
            {"name": f"Synth {k}", "week_num": 1, "category_stats": [],
             "lap_categories": cats},
        )
        # temps vary so the hinge column isn't degenerate
        _write(
            os.path.join(p.paths["weather"], f"{rid}.json"),
            {"temperature_c": 5 + (k % 4) * 5},
        )
    _write(os.path.join(p.paths["activities"], "acts.json"), summaries)
    return p


def _constant_telemetry(cad=170.0, hr=155.0, seconds=3600, step=5):
    n = seconds // step
    return pd.DataFrame(
        {
            "Second": np.arange(1, n + 1) * step,
            "HeartRate": np.full(n, hr),
            "Cadence": np.full(n, cad),
        }
    )


TREADMILL_SUMMARY = {
    "activityId": 1,
    "activityType": {"typeKey": "treadmill_running"},
}


class TestFit:
    def test_fit_recovers_stride_law(self, proc):
        model = tm.fit_model(proc)
        assert len(model["coef"]) == 8
        assert model["n_laps"] == 30 * 7  # Rest laps excluded
        assert model["n_runs"] == 30
        b = model["coef"]
        for cad, hr in ((165, 145), (175, 160), (180, 170)):
            got = (
                b[0] + b[1] * cad + b[2] * hr + b[3] * hr * hr + b[4] * cad * hr
            )
            assert abs(got - true_stride(cad, hr)) < 0.01

    def test_too_little_data_raises(self, proc):
        # Wipe most labels → below MIN_TRAIN_LAPS even in fallback window.
        for name in sorted(os.listdir(proc.paths["manual"]))[:25]:
            os.remove(os.path.join(proc.paths["manual"], name))
        with pytest.raises(tm.ModelUnavailable):
            tm.fit_model(proc)


class TestCache:
    def test_refit_only_when_labels_change(self, proc):
        m1 = tm.get_model(proc)
        m2 = tm.get_model(proc)
        assert m2["fitted_at"] == m1["fitted_at"]  # cache hit
        # Touch one meta → labels_mtime moves → refit.
        victim = os.path.join(proc.paths["manual"], "run_9000003_meta.json")
        future = datetime.datetime.now().timestamp() + 60
        os.utime(victim, (future, future))
        m3 = tm.get_model(proc)
        assert m3["labels_mtime"] != m1["labels_mtime"]


class TestPredict:
    def test_constant_curve_integrates_to_analytic_distance(self, proc):
        cad, hr, seconds = 170.0, 155.0, 3600
        with patch.object(
            DataProcessor,
            "get_activity_telemetry",
            return_value=(_constant_telemetry(cad, hr, seconds), None),
        ):
            out = tm.predict_run(proc, 1, TREADMILL_SUMMARY)
        est = out["estimate"]
        expected_mi = cad * true_stride(cad, hr) / 60 * seconds / 1609.34
        # Poisoned Rest laps would push this off by >2x; warm-up/drift
        # terms fitted on time-independent data stay near zero.
        assert est["total_distance_mi"] == pytest.approx(expected_mi, rel=0.03)
        assert est["duration_s"] == pytest.approx(seconds, abs=10)
        # Constant speed → all full-mile splits within a couple seconds.
        full = [s for s in est["splits"] if "partial_mi" not in s]
        assert len(full) == int(expected_mi)
        assert max(s["pace_s"] for s in full) - min(s["pace_s"] for s in full) <= 3
        # Each split carries its duration-weighted avg HR (constant curve
        # → exactly the input HR).
        assert all(s["avg_hr"] == 155 for s in est["splits"])

    def test_walking_buckets_add_time_not_distance(self, proc):
        df = _constant_telemetry(170.0, 155.0, 1800)
        idle = _constant_telemetry(60.0, 120.0, 600)  # cadence < 140
        idle["Second"] = idle["Second"] + 1800
        both = pd.concat([df, idle], ignore_index=True)
        with patch.object(
            DataProcessor, "get_activity_telemetry", return_value=(both, None)
        ):
            out = tm.predict_run(proc, 1, TREADMILL_SUMMARY)
        run_only_mi = 170.0 * true_stride(170.0, 155.0) / 60 * 1800 / 1609.34
        assert out["estimate"]["total_distance_mi"] == pytest.approx(
            run_only_mi, rel=0.03
        )
        assert out["estimate"]["duration_s"] == pytest.approx(2400, abs=10)

    def test_not_treadmill_raises(self, proc):
        with pytest.raises(tm.NotTreadmill):
            tm.predict_run(
                proc, 1, {"activityType": {"typeKey": "running"}}
            )

    def test_subtypekey_treadmill_recognized(self, proc):
        """codex P1: Garmin may flag indoor via subTypeKey with a plain
        `running` typeKey. Such a run must get an estimate…"""
        summary = {
            "activityId": 1,
            "activityType": {
                "typeKey": "running",
                "subTypeKey": "treadmill_running",
            },
        }
        with patch.object(
            DataProcessor,
            "get_activity_telemetry",
            return_value=(_constant_telemetry(), None),
        ):
            out = tm.predict_run(proc, 1, summary)
        assert out["estimate"]["total_distance_mi"] > 0

    def test_subtypekey_treadmill_excluded_from_training(self, proc):
        """…and must NEVER train the outdoor fit (watch-guessed distance).
        Poison one such run with absurd strides: n_laps must not move."""
        baseline = tm.fit_model(proc)["n_laps"]
        rid = 9_999_999
        today = datetime.date.today()
        laps = [
            {
                "lapIndex": 1,
                "duration": 600.0,
                "movingDuration": 600.0,
                "distance": 170 * 3.0 / 60 * 600,  # stride 3.0 m — poison
                "averageRunCadence": 170.0,
                "averageHR": 150.0,
                "averageSpeed": 170 * 3.0 / 60,
                "avgGradeAdjustedSpeed": 170 * 3.0 / 60,
            }
        ] * 4
        _write(os.path.join(proc.paths["splits"], f"{rid}.json"), {"lapDTOs": laps})
        _write(
            os.path.join(proc.paths["manual"], f"run_{rid}_meta.json"),
            {"lap_categories": ["Steady Effort"] * 4},
        )
        _write(
            os.path.join(proc.paths["weather"], f"{rid}.json"),
            {"temperature_c": 10},
        )
        acts_path = os.path.join(proc.paths["activities"], "acts.json")
        acts = json.load(open(acts_path))
        acts.append(
            {
                "activityId": rid,
                "activityType": {
                    "typeKey": "running",
                    "subTypeKey": "treadmill_running",
                },
                "startTimeLocal": f"{(today - datetime.timedelta(days=3)).isoformat()} 07:00:00",
            }
        )
        _write(acts_path, acts)
        assert tm.fit_model(proc)["n_laps"] == baseline

    def test_pause_gap_does_not_age_the_runner(self, proc):
        """codex P2: a >30s recording gap must not advance the
        warm-up/drift clock. Same running signal with and without a
        15-min mid-run gap → same distance and duration."""
        first = _constant_telemetry(170.0, 155.0, 1800)
        second = _constant_telemetry(170.0, 155.0, 1800)
        second["Second"] = second["Second"] + 1800
        contiguous = pd.concat([first, second], ignore_index=True)
        gapped = contiguous.copy()
        gapped.loc[gapped["Second"] > 1800, "Second"] += 900  # 15-min hole
        outs = []
        for frame in (contiguous, gapped):
            with patch.object(
                DataProcessor,
                "get_activity_telemetry",
                return_value=(frame, None),
            ):
                outs.append(tm.predict_run(proc, 1, TREADMILL_SUMMARY))
        a, g = (o["estimate"] for o in outs)
        assert g["duration_s"] == pytest.approx(a["duration_s"], abs=6)
        assert g["total_distance_mi"] == pytest.approx(
            a["total_distance_mi"], rel=0.005
        )

    def test_garmin_lap_view_repriced_in_model_coordinates(self, proc):
        """Block-2 unification: rows = Garmin laps, numbers = model.
        Two fake 900s watch-laps over a constant 1800s curve must each
        get half the model distance, the meta's categories, and a
        category_stats_model aggregate that ignores the Rest lap."""
        rid = 1
        _write(
            os.path.join(proc.paths["splits"], f"{rid}.json"),
            {
                "lapDTOs": [
                    {"lapIndex": 1, "duration": 900.0, "distance": 1609.34},
                    {"lapIndex": 2, "duration": 900.0, "distance": 1609.34},
                    {"lapIndex": 3, "duration": 60.0, "distance": 10.0},
                ]
            },
        )
        _write(
            os.path.join(proc.paths["manual"], f"run_{rid}_meta.json"),
            {"lap_categories": ["Hold Back Easy", "Steady Effort", "Rest"]},
        )
        cad, hr = 170.0, 155.0
        with patch.object(
            DataProcessor,
            "get_activity_telemetry",
            return_value=(_constant_telemetry(cad, hr, 1860), None),
        ):
            out = tm.predict_run(proc, rid, TREADMILL_SUMMARY)
        laps = out["estimate"]["laps"]
        assert [l["category"] for l in laps] == [
            "Hold Back Easy",
            "Steady Effort",
            "Rest",
        ]
        half_mi = cad * true_stride(cad, hr) / 60 * 900 / 1609.34
        assert laps[0]["model_distance_mi"] == pytest.approx(half_mi, rel=0.04)
        assert laps[1]["model_distance_mi"] == pytest.approx(half_mi, rel=0.04)
        # constant curve → both full laps show the same model pace ±2s
        assert abs(laps[0]["pace_s"] - laps[1]["pace_s"]) <= 2
        assert laps[0]["avg_hr"] == 155
        cats = {c["category"] for c in out["estimate"]["category_stats_model"]}
        assert cats == {"Hold Back Easy", "Steady Effort"}  # Rest excluded
        hbe = next(
            c
            for c in out["estimate"]["category_stats_model"]
            if c["category"] == "Hold Back Easy"
        )
        assert hbe["distance_mi"] == pytest.approx(half_mi, rel=0.04)

    def test_no_telemetry_raises(self, proc):
        with patch.object(
            DataProcessor, "get_activity_telemetry", return_value=(None, None)
        ):
            with pytest.raises(tm.NoTelemetry):
                tm.predict_run(proc, 1, TREADMILL_SUMMARY)


class TestEndpoint:
    """Status mapping only — the math is covered above."""

    def test_400_when_not_treadmill(self, client):
        import backend.api_server as api_server

        summary = {"activityId": 5, "activityType": {"typeKey": "running"}}
        with patch.object(api_server, "_find_run_summary", return_value=summary):
            r = client.get("/api/runs/5/treadmill-estimate")
        assert r.status_code == 400

    def test_503_when_model_unavailable(self, client):
        import backend.api_server as api_server

        summary = {
            "activityId": 5,
            "activityType": {"typeKey": "treadmill_running"},
        }
        with patch.object(api_server, "_find_run_summary", return_value=summary):
            with patch.object(
                tm, "predict_run", side_effect=tm.ModelUnavailable("thin data")
            ):
                r = client.get("/api/runs/5/treadmill-estimate")
        assert r.status_code == 503

    def test_404_when_run_missing(self, client):
        import backend.api_server as api_server

        with patch.object(api_server, "_find_run_summary", return_value=None):
            r = client.get("/api/runs/5/treadmill-estimate")
        assert r.status_code == 404
