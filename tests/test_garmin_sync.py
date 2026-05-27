"""Unit tests for backend/garmin_sync.py.

Two failure modes from production (2026-05-26, docs/IMPROVEMENTS.md §5):

  (a) `run_sync(days_back=5)` made gaps >5 days unrecoverable —
      `/api/health/timeline` returned 8 nulls because the missing
      range was outside the window forever. Fix: default is now 30.

  (b) The existence check (`os.path.exists`) treated any on-disk JSON
      as "synced," but Garmin returns an empty husk (`sleepTimeSeconds=
      None`, missing `hrvSummary`, empty `WELLNESS_RESTING_HEART_RATE`)
      if you sync before the watch has uploaded the night's data. The
      stub then stuck forever. Fix: `_is_stub(method, path)` runs
      alongside the existence check and triggers a refetch.

We mock `garminconnect.Garmin` so nothing in this file hits the real
API. Pure sync — no pytest-asyncio dep.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from backend import garmin_sync


# ---------------------------------------------------------------------------
# _is_stub — per-method stub signatures
# ---------------------------------------------------------------------------


def _write(tmp_path, method: str, day: str, payload) -> str:
    """Write `payload` (any json-serializable value) under <tmp>/<method>/<day>.json."""
    folder = tmp_path / method
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day}.json"
    with open(path, "w") as f:
        json.dump(payload, f)
    return str(path)


class TestIsStubSleep:
    """get_sleep_data: dailySleepDTO present, sleepTimeSeconds is None or 0."""

    def test_stub_when_sleep_time_seconds_is_none(self, tmp_path):
        path = _write(tmp_path, "get_sleep_data", "2026-05-07", {
            "dailySleepDTO": {"sleepTimeSeconds": None, "calendarDate": "2026-05-07"},
            "sleepScores": {"overall": {"value": None}},
        })
        assert garmin_sync._is_stub("get_sleep_data", path) is True

    def test_stub_when_sleep_time_seconds_is_zero(self, tmp_path):
        path = _write(tmp_path, "get_sleep_data", "2026-05-07", {
            "dailySleepDTO": {"sleepTimeSeconds": 0},
        })
        assert garmin_sync._is_stub("get_sleep_data", path) is True

    def test_stub_when_daily_sleep_dto_missing(self, tmp_path):
        path = _write(tmp_path, "get_sleep_data", "2026-05-07", {})
        assert garmin_sync._is_stub("get_sleep_data", path) is True

    def test_not_stub_when_sleep_time_seconds_present(self, tmp_path):
        path = _write(tmp_path, "get_sleep_data", "2026-05-07", {
            "dailySleepDTO": {"sleepTimeSeconds": 28800, "calendarDate": "2026-05-07"},
            "sleepScores": {"overall": {"value": 85}},
        })
        assert garmin_sync._is_stub("get_sleep_data", path) is False


class TestIsStubHrv:
    """get_hrv_data: stub omits hrvSummary entirely (or empty)."""

    def test_stub_when_hrv_summary_missing(self, tmp_path):
        path = _write(tmp_path, "get_hrv_data", "2026-05-07", {
            "userProfilePk": 1, "hrvReadings": []
        })
        assert garmin_sync._is_stub("get_hrv_data", path) is True

    def test_stub_when_hrv_summary_empty(self, tmp_path):
        path = _write(tmp_path, "get_hrv_data", "2026-05-07", {
            "hrvSummary": {},
        })
        assert garmin_sync._is_stub("get_hrv_data", path) is True

    def test_stub_when_hrv_summary_null(self, tmp_path):
        path = _write(tmp_path, "get_hrv_data", "2026-05-07", {
            "hrvSummary": None,
        })
        assert garmin_sync._is_stub("get_hrv_data", path) is True

    def test_not_stub_when_hrv_summary_present(self, tmp_path):
        path = _write(tmp_path, "get_hrv_data", "2026-05-07", {
            "hrvSummary": {"calendarDate": "2026-05-07", "lastNightAvg": 62},
        })
        assert garmin_sync._is_stub("get_hrv_data", path) is False


class TestIsStubRhr:
    """get_rhr_day: WELLNESS_RESTING_HEART_RATE empty list = stub."""

    def test_stub_when_metric_list_empty(self, tmp_path):
        path = _write(tmp_path, "get_rhr_day", "2026-05-07", {
            "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": []}},
        })
        assert garmin_sync._is_stub("get_rhr_day", path) is True

    def test_stub_when_metric_list_missing(self, tmp_path):
        path = _write(tmp_path, "get_rhr_day", "2026-05-07", {
            "allMetrics": {"metricsMap": {}},
        })
        assert garmin_sync._is_stub("get_rhr_day", path) is True

    def test_stub_when_all_metrics_null(self, tmp_path):
        # Saw this shape in the wild on early-morning syncs.
        path = _write(tmp_path, "get_rhr_day", "2026-05-07", {
            "allMetrics": None,
        })
        assert garmin_sync._is_stub("get_rhr_day", path) is True

    def test_not_stub_when_metric_list_populated(self, tmp_path):
        path = _write(tmp_path, "get_rhr_day", "2026-05-07", {
            "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
                {"value": 53.0, "calendarDate": "2026-05-07"}
            ]}},
        })
        assert garmin_sync._is_stub("get_rhr_day", path) is False


class TestIsStubUnknownMethod:
    """Conservative default: an unrecognized method name is never
    treated as a stub. Better to skip a refetch than to mis-handle a
    data shape we haven't characterized."""

    def test_unknown_method_returns_false(self, tmp_path):
        path = _write(tmp_path, "get_something_new", "2026-05-07", {"foo": "bar"})
        assert garmin_sync._is_stub("get_something_new", path) is False

    def test_unknown_method_returns_false_even_if_file_missing(self, tmp_path):
        # Sanity: the unknown-method short-circuit happens before any
        # file read, so a missing path doesn't matter.
        path = str(tmp_path / "get_something_new" / "missing.json")
        assert garmin_sync._is_stub("get_something_new", path) is False


class TestIsStubFileReadErrors:
    """Known method + unreadable file → treat as stub so we refetch
    rather than silently keep garbage on disk."""

    def test_corrupt_json_treated_as_stub(self, tmp_path):
        folder = tmp_path / "get_sleep_data"
        folder.mkdir()
        path = folder / "2026-05-07.json"
        path.write_text("{not valid json")
        assert garmin_sync._is_stub("get_sleep_data", str(path)) is True

    def test_missing_file_for_known_method_treated_as_stub(self, tmp_path):
        path = str(tmp_path / "get_sleep_data" / "missing.json")
        assert garmin_sync._is_stub("get_sleep_data", path) is True

    def test_non_dict_payload_treated_as_stub(self, tmp_path):
        # Some Garmin endpoints have been observed returning a bare
        # list or null on edge cases; defensive against detector
        # crashes.
        path = _write(tmp_path, "get_sleep_data", "2026-05-07", [])
        assert garmin_sync._is_stub("get_sleep_data", path) is True


# ---------------------------------------------------------------------------
# run_sync — window, existence check, stub re-fetch behavior
# ---------------------------------------------------------------------------


def _make_syncer(tmp_path, daily_methods=None):
    """Build a GarminSync with a mocked client. Skips connect() /
    _introspect_api() (they hit real Garmin). Caller fills in
    method lists explicitly."""
    syncer = garmin_sync.GarminSync(email="x", password="y", data_dir=str(tmp_path))
    syncer.client = MagicMock(name="GarminClient")
    # get_activities is called unconditionally near the end of run_sync;
    # return an empty list so the activities loop is a no-op.
    syncer.client.get_activities.return_value = []
    # Leave the four other method lists empty unless overridden.
    syncer.daily_methods = daily_methods or []
    syncer.static_methods = []
    syncer.range_methods = []
    syncer.activity_methods = []
    syncer.special_methods = []
    return syncer


class TestRunSyncDaysBackDefault:
    def test_default_is_thirty(self):
        import inspect
        sig = inspect.signature(garmin_sync.GarminSync.run_sync)
        assert sig.parameters["days_back"].default == 30


class TestRunSyncSkipsNonStubFiles:
    """Existence check still wins for healthy older files: if the file
    is present and non-stub, run_sync must NOT call the client for that
    (date, method) pair. Critical for keeping the 30-day window cheap."""

    def test_existing_non_stub_sleep_file_is_skipped(self, tmp_path):
        # Pre-populate a 10-day-old non-stub sleep file.
        import datetime
        old_day = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        _write(tmp_path, "get_sleep_data", old_day, {
            "dailySleepDTO": {"sleepTimeSeconds": 28800},
        })

        syncer = _make_syncer(tmp_path, daily_methods=[("get_sleep_data", "cdate")])
        syncer.run_sync(days_back=30, activity_limit=0)

        # Should NOT have been called for the populated day.
        called_dates = {c.kwargs.get("cdate") for c in syncer.client.get_sleep_data.call_args_list}
        assert old_day not in called_dates


class TestRunSyncRefetchesStubFiles:
    """The whole point of this PR: a stub file present on disk should
    trigger a refetch, even when it passes the existence check."""

    def test_stub_sleep_file_is_refetched(self, tmp_path):
        import datetime
        old_day = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        # Stub: sleepTimeSeconds None — the exact shape from the 5/7
        # incident.
        _write(tmp_path, "get_sleep_data", old_day, {
            "dailySleepDTO": {"sleepTimeSeconds": None},
            "sleepScores": {"overall": {"value": None}},
        })

        syncer = _make_syncer(tmp_path, daily_methods=[("get_sleep_data", "cdate")])
        # Fresh value to write back.
        syncer.client.get_sleep_data.return_value = {
            "dailySleepDTO": {"sleepTimeSeconds": 26100},
        }

        syncer.run_sync(days_back=30, activity_limit=0)

        called_dates = {c.kwargs.get("cdate") for c in syncer.client.get_sleep_data.call_args_list}
        assert old_day in called_dates

        # The file on disk now contains the non-stub payload (the
        # legacy `_save` skip-if-exists guard was already removed; this
        # verifies the chain end-to-end).
        with open(tmp_path / "get_sleep_data" / f"{old_day}.json") as f:
            data = json.load(f)
        assert data["dailySleepDTO"]["sleepTimeSeconds"] == 26100

    def test_stub_hrv_file_is_refetched(self, tmp_path):
        import datetime
        old_day = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        _write(tmp_path, "get_hrv_data", old_day, {
            "userProfilePk": 1,
            # hrvSummary absent — the stub shape.
        })

        syncer = _make_syncer(tmp_path, daily_methods=[("get_hrv_data", "cdate")])
        syncer.client.get_hrv_data.return_value = {
            "hrvSummary": {"lastNightAvg": 62},
        }

        syncer.run_sync(days_back=30, activity_limit=0)

        called_dates = {c.kwargs.get("cdate") for c in syncer.client.get_hrv_data.call_args_list}
        assert old_day in called_dates

    def test_stub_rhr_file_is_refetched(self, tmp_path):
        import datetime
        old_day = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        _write(tmp_path, "get_rhr_day", old_day, {
            "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": []}},
        })

        syncer = _make_syncer(tmp_path, daily_methods=[("get_rhr_day", "cdate")])
        syncer.client.get_rhr_day.return_value = {
            "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
                {"value": 52.0, "calendarDate": old_day}
            ]}}
        }

        syncer.run_sync(days_back=30, activity_limit=0)

        called_dates = {c.kwargs.get("cdate") for c in syncer.client.get_rhr_day.call_args_list}
        assert old_day in called_dates


class TestRunSyncUnknownMethodSkipped:
    """Unknown daily-method names default to 'not a stub' — so an
    existing file under such a method is honored by the existence
    check and not refetched."""

    def test_unknown_method_existing_file_not_refetched(self, tmp_path):
        import datetime
        old_day = (datetime.date.today() - datetime.timedelta(days=10)).isoformat()
        _write(tmp_path, "get_some_future_metric", old_day, {"whatever": True})

        syncer = _make_syncer(
            tmp_path,
            daily_methods=[("get_some_future_metric", "cdate")],
        )
        # Wire up an attribute on the mock so getattr doesn't blow up.
        syncer.client.get_some_future_metric.return_value = {"whatever": True}

        syncer.run_sync(days_back=30, activity_limit=0)

        called_dates = {
            c.kwargs.get("cdate")
            for c in syncer.client.get_some_future_metric.call_args_list
        }
        assert old_day not in called_dates


class TestRunSyncTodayYesterdayAlwaysFetched:
    """Pre-existing behavior worth pinning: today and yesterday are
    refetched even when a non-stub file is on disk, because the watch
    keeps uploading throughout the day."""

    def test_today_refetched_even_when_non_stub_present(self, tmp_path):
        import datetime
        today = datetime.date.today().isoformat()
        _write(tmp_path, "get_sleep_data", today, {
            "dailySleepDTO": {"sleepTimeSeconds": 28800},
        })

        syncer = _make_syncer(tmp_path, daily_methods=[("get_sleep_data", "cdate")])
        syncer.run_sync(days_back=30, activity_limit=0)

        called_dates = {c.kwargs.get("cdate") for c in syncer.client.get_sleep_data.call_args_list}
        assert today in called_dates
