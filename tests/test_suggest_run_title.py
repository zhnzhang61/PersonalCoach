"""suggest_run_title: prefill names like "NYC W15D3" / "NYC W16D1 - 2".

The fixture week mirrors the user's real mid-September weeks, which are
also the weeks his hand-typed names were verified against: W1 anchors to
Monday 2026-06-01, so 9/7 falls in W15 and 9/14 in W16. D counts distinct
run-days inside a Monday week; same-day recordings get " - k" by start
time; non-running activities are invisible to the whole scheme.
"""

import json
import os

import pytest

from backend.data_processor import DataProcessor


def _write_activity(dp, activity_id, start_local, type_key="running"):
    os.makedirs(dp.paths["activities"], exist_ok=True)
    with open(f"{dp.paths['activities']}/{activity_id}_summary.json", "w") as f:
        json.dump(
            {
                "activityId": activity_id,
                "activityType": {"typeKey": type_key},
                "startTimeLocal": start_local,
            },
            f,
        )


@pytest.fixture
def dp(tmp_path):
    p = DataProcessor(data_dir=str(tmp_path))
    # Week of 2026-09-07 (W15): speed day Wed 9/9 (3 recordings),
    # easy Thu 9/10, long Sat 9/12 — plus a Sunday swim that must not
    # consume a day slot.
    _write_activity(p, 101, "2026-09-09 18:49:00")
    _write_activity(p, 102, "2026-09-09 19:06:00", "track_running")
    _write_activity(p, 103, "2026-09-09 19:57:00")
    _write_activity(p, 104, "2026-09-10 18:54:00")
    _write_activity(p, 105, "2026-09-12 07:27:00")
    _write_activity(p, 106, "2026-09-13 16:11:00", "lap_swimming")
    # Week of 2026-09-14 (W16): treadmill Thu 9/17, Sunday race 9/20
    # with warmup (Sunday still belongs to the Monday-started week).
    _write_activity(p, 201, "2026-09-17 18:50:00", "treadmill_running")
    _write_activity(p, 202, "2026-09-20 06:54:00")
    _write_activity(p, 203, "2026-09-20 07:03:00")
    return p


class TestWeekAndDayNumbers:
    def test_single_run_day_has_no_suffix(self, dp):
        assert dp.suggest_run_title(105) == {
            "suggested_title": "NYC W15D3",
            "week_num": 15,
        }

    def test_day_counts_run_days_not_weekdays(self, dp):
        # Thu 9/10 is weekday 4 but only the second day with runs.
        assert dp.suggest_run_title(104)["suggested_title"] == "NYC W15D2"

    def test_sunday_belongs_to_monday_started_week(self, dp):
        out = dp.suggest_run_title(202)
        assert out["week_num"] == 16
        # Thu treadmill = D1, Sunday race = D2.
        assert out["suggested_title"].startswith("NYC W16D2")


class TestSameDayRecordings:
    def test_suffixes_follow_start_time(self, dp):
        assert dp.suggest_run_title(101)["suggested_title"] == "NYC W15D1 - 1"
        assert dp.suggest_run_title(102)["suggested_title"] == "NYC W15D1 - 2"
        assert dp.suggest_run_title(103)["suggested_title"] == "NYC W15D1 - 3"

    def test_two_recordings_still_suffixed(self, dp):
        assert dp.suggest_run_title(202)["suggested_title"] == "NYC W16D2 - 1"
        assert dp.suggest_run_title(203)["suggested_title"] == "NYC W16D2 - 2"


class TestExclusions:
    def test_non_run_gets_no_title(self, dp):
        assert dp.suggest_run_title(106) == {
            "suggested_title": None,
            "week_num": None,
        }

    def test_swim_does_not_consume_a_day_slot(self, dp):
        # If the Sunday swim counted, Sat 9/12 would still be D3 but the
        # swim would add a phantom D4; guard the day-date set directly.
        assert dp.suggest_run_title(105)["suggested_title"] == "NYC W15D3"

    def test_unknown_id_gets_no_title(self, dp):
        assert dp.suggest_run_title(999) == {
            "suggested_title": None,
            "week_num": None,
        }

    def test_pre_era_date_gets_no_title(self, dp):
        _write_activity(dp, 301, "2026-05-15 08:00:00")
        assert dp.suggest_run_title(301) == {
            "suggested_title": None,
            "week_num": None,
        }
