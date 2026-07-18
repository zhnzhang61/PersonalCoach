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
