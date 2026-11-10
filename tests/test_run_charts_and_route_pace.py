"""Extra telemetry channels (GAP, power, dynamics, stamina) and pace
along the route for the coloured map line."""

import json
import os

import pytest

from backend.data_processor import DataProcessor

AID = 888
KEYS = ["directTimestamp", "sumElapsedDuration", "sumDistance", "directHeartRate",
        "directGradeAdjustedSpeed", "directPower", "directGroundContactTime",
        "directVerticalOscillation", "directVerticalRatio", "directAvailableStamina",
        "directPotentialStamina", "directPerformanceCondition", "directRunCadence"]


def _details(rows, polyline=None):
    return {
        "metricDescriptors": [{"metricsIndex": i, "key": k} for i, k in enumerate(KEYS)],
        "activityDetailMetrics": [{"metrics": r} for r in rows],
        "geoPolylineDTO": {"polyline": polyline or [], "minLat": 40.0, "maxLat": 40.1,
                           "minLon": -74.0, "maxLon": -74.0},
    }


def _dp(tmp_path, details):
    p = DataProcessor(data_dir=str(tmp_path))
    d = os.path.join(str(tmp_path), "get_activity_details")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{AID}.json"), "w") as fh:
        json.dump(details, fh)
    return p


def _row(sec, gap=3.3528, power=380, gct=233, vo=9.2, vr=8.1, stam=80, pc=2):
    #        ts          elapsed  dist(m)      hr   gap  power gct  vo  vr  avail pot  pc  cad
    return [sec * 1000, sec, sec * 3.3528, 170, gap, power, gct, vo, vr, stam, 99, pc, 88]


class TestTelemetryChannels:
    def test_new_columns_and_gap_pace(self, tmp_path):
        p = _dp(tmp_path, _details([_row(s) for s in range(0, 60)]))
        raw, _ai = p.get_activity_telemetry(AID)
        last = raw.iloc[-1]
        assert last["GapPace"] == pytest.approx(8.0, abs=0.01)      # 3.3528 m/s = 8:00 /mi
        assert last["Power"] == 380 and last["GroundContactTime"] == 233
        assert last["VerticalRatio"] == 8.1 and last["AvailableStamina"] == 80
        assert last["PotentialStamina"] == 99 and last["PerformanceCondition"] == 2

    def test_summary_covers_the_new_tabs(self, tmp_path):
        p = _dp(tmp_path, _details([_row(s, power=300 + s) for s in range(0, 60)]))
        raw, _ = p.get_activity_telemetry(AID)
        sm = p.compute_telemetry_summary(raw)
        assert sm["GapPace"]["avg"] == pytest.approx(8.0, abs=0.01)
        assert sm["Power"]["min"] == 300 and sm["Power"]["max"] == 359
        for key in ("GroundContactTime", "VerticalOscillation", "VerticalRatio",
                    "AvailableStamina", "PerformanceCondition"):
            assert sm[key] is not None

    def test_indoor_run_has_no_gap_so_the_tab_stays_hidden(self, tmp_path):
        p = _dp(tmp_path, _details([_row(s, gap=None, pc=None) for s in range(0, 60)]))
        raw, _ = p.get_activity_telemetry(AID)
        sm = p.compute_telemetry_summary(raw)
        assert sm["GapPace"] is None and sm["PerformanceCondition"] is None
        assert sm["Power"] is not None

    def test_gap_standstill_and_outliers_do_not_enter_the_average(self, tmp_path):
        rows = [_row(s) for s in range(0, 50)] + [_row(s, gap=0.1) for s in range(50, 60)]
        p = _dp(tmp_path, _details(rows))
        raw, _ = p.get_activity_telemetry(AID)
        assert p.compute_telemetry_summary(raw)["GapPace"]["avg"] == pytest.approx(8.0, abs=0.01)


def _line(n, step_deg=0.0001, dt_ms=4000, t0=1_000_000):
    # ~11.1 m of latitude per step
    return [{"lat": 40.0 + i * step_deg, "lon": -74.0, "time": t0 + i * dt_ms} for i in range(n)]


class TestRoutePace:
    def test_progress_runs_zero_to_one_by_distance(self, tmp_path):
        p = _dp(tmp_path, _details([], _line(50)))
        r = p.get_run_route(AID)
        assert r["progress"][0] == 0.0 and r["progress"][-1] == 1.0
        assert r["progress"] == sorted(r["progress"])
        assert len(r["progress"]) == len(r["polyline"]) == len(r["pace_min_mi"])

    def test_pace_from_spacing_and_timestamps(self, tmp_path):
        # 11.12 m every 4 s = 2.78 m/s = 9:39 /mi
        p = _dp(tmp_path, _details([], _line(50)))
        r = p.get_run_route(AID)
        assert r["pace_min_mi"][10] == pytest.approx(9.65, abs=0.05)
        assert r["pace_scale"]["fast_min_mi"] == pytest.approx(9.65, abs=0.05)

    def test_scale_spans_the_bulk_not_the_extremes(self, tmp_path):
        slow = _line(40, dt_ms=6000)                       # ~14:28 /mi
        fast = [{"lat": 40.0 + (40 + i) * 0.0001, "lon": -74.0,
                 "time": slow[-1]["time"] + (i + 1) * 3000} for i in range(40)]   # ~7:14 /mi
        p = _dp(tmp_path, _details([], slow + fast))
        r = p.get_run_route(AID)
        s = r["pace_scale"]
        assert s["fast_min_mi"] < 8.0 < 13.0 < s["slow_min_mi"]

    def test_standing_still_has_no_pace(self, tmp_path):
        pts = _line(30)
        pts += [{"lat": pts[-1]["lat"], "lon": -74.0, "time": pts[-1]["time"] + (i + 1) * 4000} for i in range(10)]
        pts += [{"lat": pts[-1]["lat"] + (i + 1) * 0.0001, "lon": -74.0, "time": pts[-1]["time"] + (i + 1) * 4000} for i in range(30)]
        p = _dp(tmp_path, _details([], pts))
        r = p.get_run_route(AID)
        assert r["pace_min_mi"][35] is None
        assert r["pace_min_mi"][10] is not None and r["pace_min_mi"][-5] is not None

    def test_track_without_timestamps_degrades_to_plain_line(self, tmp_path):
        pts = [{"lat": 40.0 + i * 0.0001, "lon": -74.0} for i in range(50)]
        p = _dp(tmp_path, _details([], pts))
        r = p.get_run_route(AID)
        assert len(r["polyline"]) == 50
        assert r["pace_min_mi"] is None and r["pace_scale"] is None

    def test_downsampling_keeps_the_arrays_aligned_and_the_true_end(self, tmp_path):
        pts = _line(1200)
        p = _dp(tmp_path, _details([], pts))
        r = p.get_run_route(AID, max_points=200)
        assert len(r["polyline"]) == len(r["progress"]) == len(r["pace_min_mi"]) == 200
        assert r["end"] == [pts[-1]["lat"], pts[-1]["lon"]] and r["progress"][-1] == 1.0
        assert r["pace_min_mi"][100] == pytest.approx(9.65, abs=0.1)

    def test_no_gps_is_still_none(self, tmp_path):
        p = _dp(tmp_path, _details([], []))
        assert p.get_run_route(AID) is None
