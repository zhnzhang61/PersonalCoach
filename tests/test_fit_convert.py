"""fit_convert: FIT messages → the legacy lap / per-second / HR-zone shapes.

The mapping tests feed hand-built message dicts (what read_fit returns),
so each legacy field's source and unit conversion is pinned without a
binary fixture. One test at the end runs the real tracked FIT file
(data/21757314766.fit) through the whole chain.
"""

import datetime
import io
import os
import zipfile

import pytest

from backend import fit_convert as fc

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 11, 1, 14, 0, 0, tzinfo=UTC)
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "data", "21757314766.fit")


def _rec(sec, **kw):
    return {"timestamp": T0 + datetime.timedelta(seconds=sec), **kw}


def _lap(**kw):
    base = {
        "start_time": T0, "message_index": 0, "total_distance": 1609.34,
        "total_elapsed_time": 500.0, "total_timer_time": 480.0,
        "avg_heart_rate": 170, "max_heart_rate": 180, "intensity": "active",
    }
    return {**base, **kw}


class TestLaps:
    def test_core_fields_and_units(self):
        fit = {"record": [], "lap": [_lap(
            avg_running_cadence=88, avg_fractional_cadence=0.5,
            avg_step_length=1100.0, avg_vertical_oscillation=92.0,
            avg_stance_time=240.0, avg_stance_time_balance=50.4,
            enhanced_avg_respiration_rate=38.5, total_ascent=12, total_descent=5,
            start_position_lat=2**30, start_position_long=-(2**30),
            **{"#161": 3301, "#155": 9},
        )]}
        lap = fc.laps_to_dtos(fit)[0]
        assert lap["distance"] == 1609.34
        assert lap["duration"] == 480.0          # timer time, pauses excluded
        assert lap["elapsedDuration"] == 500.0
        assert lap["averageSpeed"] == pytest.approx(1609.34 / 480.0)
        assert lap["averageRunCadence"] == 177.0  # strides/min (+fraction) → steps/min
        assert lap["strideLength"] == 110.0       # mm → cm
        assert lap["verticalOscillation"] == 9.2  # mm → cm
        assert lap["groundContactTime"] == 240.0
        assert lap["groundContactBalanceLeft"] == 50.4
        assert lap["avgRespirationRate"] == 38.5
        assert lap["avgGradeAdjustedSpeed"] == 3.301  # field #161, mm/s → m/s
        assert lap["bmrCalories"] == 9
        assert lap["startLatitude"] == 90.0 and lap["startLongitude"] == -90.0
        assert lap["elevationGain"] == 12 and lap["elevationLoss"] == 5
        assert lap["startTimeGMT"] == "2026-11-01T14:00:00.0"
        assert lap["lapIndex"] == 1 and lap["messageIndex"] == 0
        assert lap["intensityType"] == "ACTIVE"

    @pytest.mark.parametrize("fit_value,legacy", [
        ("interval", "INTERVAL"), ("rest", "REST"), ("warmup", "WARMUP"),
        ("cooldown", "COOLDOWN"), ("recovery", "RECOVERY"),
    ])
    def test_intensity_names(self, fit_value, legacy):
        lap = fc.laps_to_dtos({"record": [], "lap": [_lap(intensity=fit_value)]})[0]
        assert lap["intensityType"] == legacy

    def test_moving_time_max_speed_and_elevation_come_from_the_laps_records(self):
        # 100 s lap: 60 s running, 40 s standing. A second lap's records
        # must not leak into the first.
        recs = [_rec(s, enhanced_speed=3.0, enhanced_altitude=10.0 + s * 0.1) for s in range(1, 61)]
        recs += [_rec(s, enhanced_speed=0.2, enhanced_altitude=16.0) for s in range(61, 101)]
        recs += [_rec(s, enhanced_speed=9.0, enhanced_altitude=99.0) for s in range(101, 111)]
        fit = {"record": recs, "lap": [_lap(total_elapsed_time=100.0, total_timer_time=100.0, total_distance=180.0)]}
        lap = fc.laps_to_dtos(fit)[0]
        assert lap["movingDuration"] == 60.0
        assert lap["averageMovingSpeed"] == 3.0
        assert lap["maxSpeed"] == 3.0
        assert lap["maxElevation"] == pytest.approx(16.0)
        assert lap["minElevation"] == pytest.approx(10.1)

    def test_lap_without_records_falls_back_to_timer_time(self):
        lap = fc.laps_to_dtos({"record": [], "lap": [_lap()]})[0]
        assert lap["movingDuration"] == 480.0
        assert lap["maxSpeed"] is None

    def test_walking_counts_as_moving(self):
        # Walk breaks (~1.3 m/s) are "moving" in Garmin Connect; a higher
        # threshold would shorten movingDuration on run-walk laps.
        recs = [_rec(s, enhanced_speed=1.3) for s in range(1, 31)]
        fit = {"record": recs, "lap": [_lap(total_elapsed_time=30.0, total_timer_time=30.0)]}
        assert fc.laps_to_dtos(fit)[0]["movingDuration"] == 30.0


class TestDetails:
    def _fit(self):
        recs = [
            _rec(0, distance=0.0, enhanced_speed=0.0, heart_rate=100, enhanced_altitude=5.0,
                 position_lat=2**29, position_long=-(2**29)),
            _rec(1, distance=3.0, enhanced_speed=3.0, heart_rate=120, enhanced_altitude=5.5,
                 cadence=88, fractional_cadence=0.5, step_length=1000.0, stance_time=250.0,
                 stance_time_balance=50.1, vertical_oscillation=90.0, vertical_ratio=9.0,
                 power=380, accumulated_power=380, temperature=12,
                 enhanced_respiration_rate=30.5, position_lat=2**29, position_long=-(2**29),
                 **{"#140": 3100, "#90": 2, "#137": 99, "#138": 98, "#143": 60}),
        ]
        return {"record": recs, "lap": [], "session": [{"start_time": T0}], "event": []}

    def test_row_values_and_units(self):
        d = fc.records_to_details(42, self._fit())
        assert d["activityId"] == 42 and d["metricsCount"] == 2 and d["detailsAvailable"] is True
        idx = {m["key"]: m["metricsIndex"] for m in d["metricDescriptors"]}
        row = d["activityDetailMetrics"][1]["metrics"]
        get = lambda k: row[idx[k]]  # noqa: E731
        assert get("directTimestamp") == int((T0.timestamp() + 1) * 1000)
        assert get("sumElapsedDuration") == 1.0 and get("sumDuration") == 1.0
        assert get("sumDistance") == 3.0 and get("directSpeed") == 3.0
        assert get("directGradeAdjustedSpeed") == 3.1      # field #140, mm/s
        assert get("directHeartRate") == 120
        assert get("directRunCadence") == 88 and get("directDoubleCadence") == 177.0
        assert get("directStrideLength") == 100.0          # mm → cm
        assert get("directVerticalOscillation") == 9.0     # mm → cm
        assert get("directRespirationRate") == 30.5
        assert get("directVerticalSpeed") == 0.5
        assert get("directPerformanceCondition") == 2
        assert get("directPotentialStamina") == 99 and get("directAvailableStamina") == 98
        assert get("directBodyBattery") == 60
        assert get("directLatitude") == 45.0 and get("directLongitude") == -45.0

    def test_every_row_has_one_value_per_descriptor(self):
        d = fc.records_to_details(1, self._fit())
        assert all(len(r["metrics"]) == len(d["metricDescriptors"]) for r in d["activityDetailMetrics"])

    def test_timer_pause_is_excluded_from_duration_but_not_elapsed(self):
        fit = self._fit()
        fit["record"].append(_rec(61, distance=6.0, enhanced_speed=3.0))
        fit["event"] = [
            {"event": "timer", "event_type": "stop_all", "timestamp": T0 + datetime.timedelta(seconds=10)},
            {"event": "timer", "event_type": "start", "timestamp": T0 + datetime.timedelta(seconds=50)},
        ]
        d = fc.records_to_details(1, fit)
        idx = {m["key"]: m["metricsIndex"] for m in d["metricDescriptors"]}
        last = d["activityDetailMetrics"][-1]["metrics"]
        assert last[idx["sumElapsedDuration"]] == 61.0
        assert last[idx["sumDuration"]] == 21.0

    def test_polyline_start_end_and_bounds(self):
        geo = fc.records_to_details(1, self._fit())["geoPolylineDTO"]
        assert geo["startPoint"]["lat"] == 45.0 and geo["startPoint"]["lon"] == -45.0
        assert geo["minLat"] == geo["maxLat"] == 45.0
        assert len(geo["polyline"]) == 2

    def test_polyline_is_thinned_but_keeps_the_last_point(self):
        recs = [_rec(s, position_lat=2**29 + s, position_long=-(2**29)) for s in range(4000)]
        geo = fc.records_to_details(1, {"record": recs, "session": [{"start_time": T0}]})["geoPolylineDTO"]
        assert len(geo["polyline"]) <= 1501
        assert geo["polyline"][-1]["time"] == geo["endPoint"]["time"]

    def test_long_activity_is_thinned_like_the_legacy_endpoint(self):
        recs = [_rec(s, distance=float(s), enhanced_speed=3.0) for s in range(13000)]
        fit = {"record": recs, "session": [{"start_time": T0}]}
        d = fc.records_to_details(1, fit)
        assert d["totalMetricsCount"] == 13000
        assert d["metricsCount"] == len(d["activityDetailMetrics"]) <= 2001
        idx = {m["key"]: m["metricsIndex"] for m in d["metricDescriptors"]}
        last = d["activityDetailMetrics"][-1]["metrics"]
        # The final sample survives, and cumulative columns are still the
        # true totals (computed before thinning).
        assert last[idx["sumDistance"]] == 12999.0
        assert last[idx["sumMovingDuration"]] == 12999.0
        assert fc.records_to_details(1, fit, max_rows=None)["metricsCount"] == 13000

    def test_indoor_run_has_empty_polyline(self):
        fit = {"record": [_rec(1, distance=3.0, enhanced_speed=3.0)], "session": [{"start_time": T0}]}
        geo = fc.records_to_details(1, fit)["geoPolylineDTO"]
        assert geo["polyline"] == [] and geo["startPoint"] is None


class TestHrZones:
    def test_zone_n_is_index_n_and_starts_at_previous_boundary(self):
        fit = {"time_in_zone": [
            {"reference_mesg": "lap", "time_in_hr_zone": [9, 9, 9, 9, 9, 9, 9],
             "hr_zone_high_boundary": [130, 162, 173, 179, 184, 190]},
            {"reference_mesg": "session", "time_in_hr_zone": [25.0, 248.0, 2063.0, 5968.0, 4119.0, 731.0, 91.0],
             "hr_zone_high_boundary": [130, 162, 173, 179, 184, 190]},
        ]}
        zones = fc.hr_time_in_zones(fit)
        assert [z["zoneNumber"] for z in zones] == [1, 2, 3, 4, 5]
        assert [z["zoneLowBoundary"] for z in zones] == [130, 162, 173, 179, 184]
        assert zones[0]["secsInZone"] == 248.0
        # Zone 5 absorbs the over-max bucket, as Garmin Connect reports it.
        assert zones[4]["secsInZone"] == 822.0

    def test_no_session_zone_message(self):
        assert fc.hr_time_in_zones({"time_in_zone": []}) == []


class TestReadFit:
    def test_real_file_end_to_end(self):
        with open(FIXTURE, "rb") as fh:
            raw = fh.read()
        fit = fc.read_fit(raw)
        assert len(fit["record"]) > 1000 and len(fit["lap"]) >= 1
        laps = fc.laps_to_dtos(fit)
        assert laps[0]["distance"] == pytest.approx(1609.34)
        assert laps[0]["avgGradeAdjustedSpeed"] and 2.0 < laps[0]["avgGradeAdjustedSpeed"] < 6.0
        assert laps[0]["intensityType"] in {"ACTIVE", "INTERVAL", "WARMUP", "REST", "COOLDOWN", "RECOVERY"}
        details = fc.records_to_details(21757314766, fit, max_rows=None)
        assert details["metricsCount"] == len(fit["record"])
        assert details["geoPolylineDTO"]["startPoint"]["lat"] == pytest.approx(40.77, abs=0.05)
        assert sum(z["secsInZone"] for z in fc.hr_time_in_zones(fit)) > 0

    def test_zip_from_the_download_endpoint_is_accepted(self):
        # The tracked fixture is itself the zip Garmin serves; start from
        # the bare .fit inside it.
        with open(FIXTURE, "rb") as fh:
            raw = fc.unzip_fit(fh.read())
        assert raw[8:12] == b".FIT"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("21757314766_ACTIVITY.fit", raw)
        assert fc.unzip_fit(buf.getvalue()) == raw
        assert len(fc.read_fit(buf.getvalue())["record"]) == len(fc.read_fit(raw)["record"])
