"""garmin_official_sync: Personal API responses → legacy on-disk layout.

A fake client stands in for Garmin; the FIT download serves the tracked
fixture file so the whole activity chain (download → convert → write) runs
for real.
"""

import datetime
import json
import os

import pytest

from backend import garmin_official_sync as gos
from backend.data_processor import DataProcessor

TODAY = datetime.date(2026, 11, 9)
FIXTURE = os.path.join(os.path.dirname(__file__), "..", "data", "21757314766.fit")


class HttpError(Exception):
    def __init__(self, status_code):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class FakeClient:
    def __init__(self, activities=None, fail=None):
        self.activities = activities if activities is not None else [
            {"activityId": 111, "startTimeLocal": "2026-11-08 07:00:00",
             "activityType": {"typeKey": "running"}, "distance": 5000.0, "duration": 1500.0},
        ]
        self.fail = fail or {}
        self.calls = []

    def _maybe_fail(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def search_activities(self, *, start=0, limit=20, start_date=None, end_date=None, activity_type=None):
        self._maybe_fail("search")
        return self.activities[start:start + limit]

    def get_activity(self, aid):
        self._maybe_fail("activity")
        return {"activityId": aid, "summaryDTO": {"distance": 5000.0}}

    def download_activity(self, aid):
        self._maybe_fail("download")
        with open(FIXTURE, "rb") as fh:
            return fh.read()

    def get(self, path, *, params=None):
        self._maybe_fail("daily")
        d = params["calendarDate"]
        return {"calendarDate": d, "restingHeartRate": 51, "averageStressLevel": 30,
                "maxStressLevel": 90, "moderateIntensityMinutes": 10,
                "vigorousIntensityMinutes": 20, "intensityMinutesGoal": 400,
                "avgWakingRespirationValue": 14.0, "bodyBatteryAtWakeTime": 80}

    def get_daily_hrv(self, d):
        self._maybe_fail("hrv")
        return {"hrvSummary": {"calendarDate": d, "lastNightAvg": 71, "weeklyAvg": 70, "status": "BALANCED"}}

    def get_daily_sleep(self, d):
        self._maybe_fail("sleep")
        return {"calendarDate": d, "sleepTimeSeconds": 27000,
                "sleepScores": {"overall": {"value": 86}}, "averageRespirationValue": 13.0}

    def get_fitness_age(self, d):
        self._maybe_fail("fitness_age")
        return {"fitnessAge": 27.2, "chronologicalAge": 33}


def _sync(tmp_path, client=None, **kw):
    s = gos.GarminOfficialSync(client or FakeClient(), data_dir=str(tmp_path), today=TODAY,
                               sleep=lambda _s: None)
    s.run_sync(**kw)
    return s


def _load(tmp_path, folder, name):
    with open(tmp_path / folder / name) as fh:
        return json.load(fh)


class TestActivityFiles:
    def test_writes_summary_detail_and_fit_derivatives(self, tmp_path):
        _sync(tmp_path, days_back=3)
        assert _load(tmp_path, "get_activities", "111_summary.json")["activityId"] == 111
        assert _load(tmp_path, "get_activity", "111.json")["summaryDTO"]["distance"] == 5000.0
        assert (tmp_path / "fit" / "111.fit").stat().st_size > 1000
        laps = _load(tmp_path, "get_activity_splits", "111.json")
        assert laps["activityId"] == 111 and len(laps["lapDTOs"]) >= 1
        details = _load(tmp_path, "get_activity_details", "111.json")
        assert details["metricsCount"] > 1000
        zones = _load(tmp_path, "get_activity_hr_in_timezones", "111.json")
        assert [z["zoneNumber"] for z in zones] == [1, 2, 3, 4, 5]

    def test_files_are_tagged_with_their_source(self, tmp_path):
        _sync(tmp_path, days_back=1)
        for folder, name in [("get_activities", "111_summary.json"), ("get_activity_splits", "111.json"),
                             ("get_sleep_data", "2026-11-09.json"), ("get_rhr_day", "2026-11-09.json")]:
            assert _load(tmp_path, folder, name)["_source"] == "garmin_personal_api"

    def test_fit_is_downloaded_once(self, tmp_path):
        first = FakeClient()
        _sync(tmp_path, client=first, days_back=1)
        assert first.calls.count("download") == 1
        second = FakeClient()
        _sync(tmp_path, client=second, days_back=1)
        assert second.calls.count("download") == 0

    def test_force_fit_redownloads(self, tmp_path):
        _sync(tmp_path, days_back=1)
        again = FakeClient()
        _sync(tmp_path, client=again, days_back=1, force_fit=True)
        assert again.calls.count("download") == 1

    def test_unreadable_fit_leaves_no_marker_so_the_next_sync_retries(self, tmp_path):
        class Broken(FakeClient):
            def download_activity(self, aid):
                self.calls.append("download")
                return b"not a fit file"
        _sync(tmp_path, client=Broken(), days_back=1)
        assert not (tmp_path / "fit" / "111.fit").exists()
        retry = FakeClient()
        _sync(tmp_path, client=retry, days_back=1)
        assert retry.calls.count("download") == 1
        assert (tmp_path / "get_activity_splits" / "111.json").exists()

    def test_activity_search_pages_until_a_short_page(self, tmp_path):
        many = [{"activityId": 1000 + i, "startTimeLocal": "2026-11-08 07:00:00"} for i in range(120)]

        class NoFit(FakeClient):
            def download_activity(self, aid):
                self.calls.append("download")
                return b""
        client = NoFit(activities=many)
        s = gos.GarminOfficialSync(client, data_dir=str(tmp_path), today=TODAY, sleep=lambda _s: None)
        ids = s.sync_activities(TODAY - datetime.timedelta(days=3), TODAY)
        assert len(ids) == 120 and client.calls.count("search") == 3


class TestDailyFiles:
    def test_legacy_shapes_the_ledger_reads(self, tmp_path):
        _sync(tmp_path, days_back=1)
        d = "2026-11-09.json"
        assert _load(tmp_path, "get_sleep_data", d)["dailySleepDTO"]["sleepTimeSeconds"] == 27000
        rhr = _load(tmp_path, "get_rhr_day", d)
        assert rhr["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"][0]["value"] == 51.0
        assert _load(tmp_path, "get_hrv_data", d)["hrvSummary"]["weeklyAvg"] == 70
        assert _load(tmp_path, "get_stress_data", d)["avgStressLevel"] == 30
        assert _load(tmp_path, "get_stats_and_body", d)["bodyBatteryAtWakeTime"] == 80
        assert _load(tmp_path, "get_fitnessage_data", d)["fitnessAge"] == 27.2

    def test_sleep_file_carries_overnight_hrv_and_resting_hr(self, tmp_path):
        _sync(tmp_path, days_back=1)
        sleep = _load(tmp_path, "get_sleep_data", "2026-11-09.json")
        assert sleep["avgOvernightHrv"] == 71 and sleep["hrvStatus"] == "BALANCED"
        assert sleep["restingHeartRate"] == 51
        assert sleep["bodyBatteryChange"] is None  # not exposed by the Personal API

    def test_data_processor_builds_the_same_ledger_row(self, tmp_path):
        _sync(tmp_path, days_back=2)
        rows = DataProcessor(data_dir=str(tmp_path)).compile_health_ledger(days_back=400)
        row = next(r for r in rows if r["date"] == "2026-11-08")
        assert row["sleep_score"] == 86 and row["sleep_hours"] == 7.5
        assert row["rhr"] == 51.0 and row["hrv"] == 70 and row["stress"] == 30
        assert row["run_miles"] == pytest.approx(3.11, abs=0.01)

    def test_intensity_minutes_accumulate_over_the_iso_week(self, tmp_path):
        # 2026-11-09 is a Monday, so use Wednesday the 11th as "today".
        s = gos.GarminOfficialSync(FakeClient(activities=[]), data_dir=str(tmp_path),
                                   today=datetime.date(2026, 11, 11), sleep=lambda _s: None)
        s.run_sync(days_back=4)
        wed = _load(tmp_path, "get_intensity_minutes_data", "2026-11-11.json")
        assert wed["moderateMinutes"] == 10 and wed["vigorousMinutes"] == 20
        assert wed["weeklyModerate"] == 30 and wed["weeklyVigorous"] == 60   # Mon+Tue+Wed
        assert wed["weeklyTotal"] == 150 and wed["weekGoal"] == 400
        mon = _load(tmp_path, "get_intensity_minutes_data", "2026-11-09.json")
        assert mon["weeklyModerate"] == 10

    def test_complete_old_days_are_not_refetched(self, tmp_path):
        _sync(tmp_path, days_back=6)
        again = FakeClient()
        _sync(tmp_path, client=again, days_back=6)
        # today, yesterday and the day before are always refreshed; the
        # four older days are complete on disk.
        assert again.calls.count("daily") == 3

    def test_old_day_with_stub_sleep_is_refetched(self, tmp_path):
        _sync(tmp_path, days_back=6)
        stub = tmp_path / "get_sleep_data" / "2026-11-04.json"
        stub.write_text(json.dumps({"dailySleepDTO": {"sleepTimeSeconds": None}}))
        again = FakeClient()
        _sync(tmp_path, client=again, days_back=6)
        assert again.calls.count("daily") == 4
        assert _load(tmp_path, "get_sleep_data", "2026-11-04.json")["dailySleepDTO"]["sleepTimeSeconds"] == 27000


class TestFailures:
    def test_one_failing_endpoint_does_not_stop_the_rest(self, tmp_path):
        client = FakeClient(fail={"hrv": HttpError(500)})
        _sync(tmp_path, client=client, days_back=1)
        assert (tmp_path / "get_sleep_data" / "2026-11-09.json").exists()
        assert not (tmp_path / "get_hrv_data" / "2026-11-09.json").exists()

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_key_aborts_the_sync(self, tmp_path, status):
        client = FakeClient(fail={"search": HttpError(status)})
        with pytest.raises(gos.KeyRejected):
            _sync(tmp_path, client=client, days_back=1)

    def test_revoked_cached_token_is_refreshed_once_and_the_call_retried(self, tmp_path):
        # The library reuses a cached bearer token; if Garmin revoked it
        # early the first data call 401s although the key is fine.
        class StaleToken(FakeClient):
            token_ok = False

            def search_activities(self, **kw):
                self.calls.append("search")
                if not self.token_ok:
                    raise HttpError(401)
                return self.activities[kw.get("start", 0):kw.get("start", 0) + kw.get("limit", 20)]
        client = StaleToken()
        refreshed = []

        def refresh():
            refreshed.append(1)
            client.token_ok = True
        s = gos.GarminOfficialSync(client, data_dir=str(tmp_path), today=TODAY,
                                   sleep=lambda _s: None, refresh_token=refresh)
        s.run_sync(days_back=1)
        assert refreshed == [1]
        assert (tmp_path / "get_activities" / "111_summary.json").exists()

    def test_still_rejected_after_refresh_means_the_key_is_bad(self, tmp_path):
        client = FakeClient(fail={"search": HttpError(401)})
        refreshed = []
        s = gos.GarminOfficialSync(client, data_dir=str(tmp_path), today=TODAY,
                                   sleep=lambda _s: None, refresh_token=lambda: refreshed.append(1))
        with pytest.raises(gos.KeyRejected):
            s.run_sync(days_back=1)
        assert refreshed == [1]   # tried exactly once, not per call

    def test_failed_activity_search_is_an_error_not_an_empty_window(self, tmp_path):
        client = FakeClient(fail={"search": HttpError(500)})
        with pytest.raises(gos.SyncError):
            _sync(tmp_path, client=client, days_back=1)

    def test_main_reports_failed_search_as_exit_1(self, monkeypatch, capsys, tmp_path):
        monkeypatch.setenv(gos.ENV_KEY, "k")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gos, "connect",
                            lambda _k: (FakeClient(fail={"search": HttpError(503)}), lambda: None))
        monkeypatch.setattr(gos.time, "sleep", lambda _s: None)
        assert gos.main(["prog"]) == 1
        assert "SYNC_FAILED" in capsys.readouterr().err

    def test_main_without_key_exits_3(self, monkeypatch, capsys):
        monkeypatch.delenv(gos.ENV_KEY, raising=False)
        assert gos.main(["prog"]) == gos.EXIT_NO_KEY
        assert "NO_API_KEY" in capsys.readouterr().err

    def test_main_with_rejected_key_exits_4(self, monkeypatch, capsys):
        monkeypatch.setenv(gos.ENV_KEY, "bad")

        def refuse(_key):
            raise gos.KeyRejected("HTTP 401")
        monkeypatch.setattr(gos, "connect", refuse)
        assert gos.main(["prog"]) == gos.EXIT_KEY_REJECTED
        assert "KEY_REJECTED" in capsys.readouterr().err

    def test_days_back_flag_is_clamped(self):
        assert gos._int_flag(["p", "--days-back", "9999"], "--days-back", 7, 1, 365) == 365
        assert gos._int_flag(["p", "--days-back", "x"], "--days-back", 7, 1, 365) == 7
        assert gos._int_flag(["p"], "--days-back", 7, 1, 365) == 7
