"""Respiration × HR distribution model + per-run effort points.

The candle payload is what the run page draws and what the coach reads,
so the contract worth pinning is: warm-up/cool-down laps stay out (they
carry HR lag that inflates the low-HR bands), thin bands are omitted
rather than drawn, and the `reliable` flag follows overlap with the
trustworthy HR window instead of a band's lower edge — judging by the
edge alone wrongly mutes Steady Effort (145-162), which is 72% inside.
"""

import json

import pytest

from backend.data_processor import DataProcessor


def _lap(hr, resp, *, dist_m=1609.34, dur=540):
    return {
        "averageHR": hr,
        "avgRespirationRate": resp,
        "distance": dist_m,
        "movingDuration": dur,
        "duration": dur,
        "startTimeGMT": "2026-07-01T12:00:00.0",
    }


def _write_run(dp, activity_id, laps, categories=None):
    import os
    os.makedirs(dp.paths["splits"], exist_ok=True)
    with open(f"{dp.paths['splits']}/{activity_id}.json", "w") as f:
        json.dump({"activityId": activity_id, "lapDTOs": laps}, f)
    if categories is not None:
        with open(f"{dp.paths['manual']}/run_{activity_id}_meta.json", "w") as f:
            json.dump({"lap_categories": categories}, f)


@pytest.fixture
def dp(tmp_path):
    p = DataProcessor(data_dir=str(tmp_path))
    # hr_to_resp is keyed off the user's own zones; without this file
    # get_hr_zones() returns [] and that whole view is empty (covered
    # by test_no_zones_yields_empty_hr_view below).
    with open(p.paths["user_zones"], "w") as f:
        json.dump({
            "VO2 Max": "> 183 bpm",
            "Lactate Threshold": "179 - 183 bpm",
            "Marathon Pace": "174 - 178 bpm",
            "Increasing Effort": "163 - 173 bpm",
            "Steady / Constant": "145 - 162 bpm",
            "Hold Back / Recovery": "< 145 bpm",
        }, f)
    return p


def test_overlap_frac():
    f = DataProcessor._overlap_frac
    assert f(145, 162, 149, 174) == pytest.approx(13 / 17)
    assert f(0, 144, 149, 174) == 0.0
    assert f(163, 173, 149, 174) == 1.0


def test_candle_five_numbers():
    c = DataProcessor._candle([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert c["median"] == pytest.approx(5.5)
    assert c["p25"] < c["median"] < c["p75"]
    assert c["p10"] < c["p25"] and c["p75"] < c["p90"]


def test_first_and_last_lap_excluded(dp):
    # Warm-up lap carries a wildly high respiration for its HR; if it
    # leaked into the population it would drag the band mean up.
    laps = [_lap(140, 45)] + [_lap(155, 32) for _ in range(6)] + [_lap(140, 44)]
    _write_run(dp, 1, laps)
    rows = list(dp._iter_model_laps())
    assert len(rows) == 6
    assert all(r["resp"] == 32 for r in rows)


def test_short_and_tiny_laps_excluded(dp):
    laps = [
        _lap(150, 30),
        _lap(155, 32, dur=20),          # too short
        _lap(155, 32, dist_m=100),      # too tiny
        _lap(155, 33),
        _lap(150, 30),
    ]
    _write_run(dp, 2, laps)
    assert len(list(dp._iter_model_laps())) == 1


def test_thin_bands_omitted_not_drawn(dp):
    _write_run(dp, 3, [_lap(150, 30)] * 8)
    d = dp.get_resp_hr_distribution(min_laps=100)
    assert d["hr_to_resp"] == []
    assert d["resp_to_hr"] == []


def test_reliable_flag_follows_overlap_not_lower_edge(dp):
    # Steady Effort starts at 145 (below the 149 floor) but is mostly
    # inside the window — it must not be muted.
    laps = [_lap(155, 32)] * 20 + [_lap(168, 37)] * 20 + [_lap(181, 41)] * 20
    _write_run(dp, 4, [_lap(150, 30)] + laps + [_lap(150, 30)])
    d = dp.get_resp_hr_distribution(min_laps=5)
    flags = {r["key"]: r["reliable"] for r in d["hr_to_resp"]}
    assert flags["Steady Effort"] is True
    assert flags["Increasing Effort"] is True
    assert flags.get("LT Effort") is False


def test_since_filter(dp):
    _write_run(dp, 5, [_lap(150, 30)] * 8)
    assert list(dp._iter_model_laps(since="2027-01-01")) == []
    assert len(list(dp._iter_model_laps(since="2026-01-01"))) == 6


def test_run_effort_points_use_user_labels(dp):
    laps = [_lap(150, 30), _lap(160, 35), _lap(160, 35), _lap(175, 40)]
    _write_run(
        dp, 6, laps,
        categories=["Hold Back Easy", "Steady Effort", "Steady Effort", "Marathon"],
    )
    pts = dp.get_run_effort_points(6)
    by = {p["category"]: p for p in pts}
    assert by["Steady Effort"]["avg_hr"] == pytest.approx(160)
    assert by["Steady Effort"]["avg_resp"] == pytest.approx(35)
    assert by["Steady Effort"]["n_laps"] == 2
    assert all(p["source"] == "user_labels" for p in pts)
    # Ordered by effort, not by dict insertion.
    assert [p["category"] for p in pts] == [
        "Hold Back Easy", "Steady Effort", "Marathon",
    ]


def test_run_effort_points_distance_weighted(dp):
    # A short lap must not swing the centroid the way an equal-weight
    # mean would: 10 mi at HR 150 plus 0.5 mi at HR 190.
    laps = [
        _lap(150, 30, dist_m=1609.34 * 10, dur=5400),
        _lap(190, 42, dist_m=1609.34 * 0.5, dur=200),
    ]
    _write_run(dp, 7, laps, categories=["Steady Effort", "Steady Effort"])
    # Interior-lap trimming does not apply here — effort points read all
    # laps, since the user's own labels define the grouping.
    pt = dp.get_run_effort_points(7)[0]
    assert 150 < pt["avg_hr"] < 155


def test_run_effort_points_falls_back_without_labels(dp):
    _write_run(dp, 8, [_lap(150, 30)] * 4)
    pts = dp.get_run_effort_points(8)
    assert pts and all(p["source"] == "hr_zone_fallback" for p in pts)


def test_run_effort_points_empty_for_unknown_run(dp):
    assert dp.get_run_effort_points(999) == []


def test_no_zones_yields_empty_hr_view_but_keeps_resp_view(tmp_path):
    # A user who hasn't annotated zones still gets the resp→HR view,
    # which needs no zone definitions. The HR→resp view has nothing to
    # key on and comes back empty rather than inventing bands.
    p = DataProcessor(data_dir=str(tmp_path))
    _write_run(p, 10, [_lap(150, 30)] + [_lap(160, 35)] * 8 + [_lap(150, 30)])
    d = p.get_resp_hr_distribution(min_laps=3)
    assert d["hr_to_resp"] == []
    assert len(d["resp_to_hr"]) >= 1
