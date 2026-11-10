"""get_run_full_stats: the grouped "everything Garmin shows" payload."""

import json
import os

import pytest

from backend.data_processor import DataProcessor

AID = 777


def _write(dp, folder, name, obj):
    d = os.path.join(dp.data_dir, folder)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as fh:
        json.dump(obj, fh)


SUMMARY = {
    "distance": 16093.4, "duration": 4830.0, "movingDuration": 4800.0, "elapsedDuration": 4900.0,
    "averageSpeed": 3.3320, "averageMovingSpeed": 3.3528, "maxSpeed": 5.0, "avgGradeAdjustedSpeed": 3.40,
    "averageHR": 165.0, "maxHR": 181.0, "minHR": 95.0,
    "averageRunCadence": 175.6, "maxRunCadence": 190.0, "strideLength": 110.1,
    "groundContactTime": 233.4, "groundContactBalanceLeft": 51.47,
    "verticalOscillation": 9.17, "verticalRatio": 8.15, "steps": 14000,
    "averagePower": 382.0, "maxPower": 580.0, "normalizedPower": 390.0,
    "elevationGain": 100.0, "elevationLoss": 90.0, "minElevation": 4.0, "maxElevation": 84.2,
    "trainingEffect": 4.2, "aerobicTrainingEffectMessage": "HIGHLY_IMPACTING_TEMPO_23",
    "anaerobicTrainingEffect": 1.0, "anaerobicTrainingEffectMessage": "MINOR_ANAEROBIC_BENEFIT_0",
    "trainingEffectLabel": "LACTATE_THRESHOLD", "activityTrainingLoad": 250.4,
    "moderateIntensityMinutes": 5, "vigorousIntensityMinutes": 70,
    "avgRespirationRate": 36.24, "maxRespirationRate": 44.0, "minRespirationRate": 20.0,
    "beginPotentialStamina": 99.0, "endPotentialStamina": 41.0, "minAvailableStamina": 30.0,
    "differenceBodyBattery": -18, "calories": 1100.0, "waterEstimated": 1400.0,
    "averageTemperature": 20.0, "directWorkoutFeel": 75, "directWorkoutRpe": 60,
}
LISTING = {
    "activityId": AID, "activityType": {"typeKey": "running"},
    "fastestSplit_1000": 270.0, "fastestSplit_1609": 440.0, "fastestSplit_5000": 1400.0,
    "hrTimeInZone_1": 100.0, "hrTimeInZone_2": 300.0,
    "powerTimeInZone_1": 60.0, "powerTimeInZone_2": 40.0,
}
LAPS = [
    {"distance": 1609.34, "duration": 480.0, "averageHR": 160.0, "maxHR": 170.0,
     "avgGradeAdjustedSpeed": 3.40, "averageRunCadence": 176.4, "strideLength": 112.0,
     "groundContactTime": 240.2, "groundContactBalanceLeft": 51.04, "verticalOscillation": 9.04,
     "verticalRatio": 8.06, "averagePower": 380.4, "elevationGain": 10.0, "elevationLoss": 3.0,
     "avgRespirationRate": 35.52, "intensityType": "ACTIVE"},
    {"distance": 804.67, "duration": 300.0, "averageHR": 150.0, "maxHR": 158.0, "intensityType": "REST"},
]


@pytest.fixture
def dp(tmp_path):
    p = DataProcessor(data_dir=str(tmp_path))
    _write(p, "get_activity", f"{AID}.json", {"summaryDTO": SUMMARY})
    _write(p, "get_activities", f"{AID}_summary.json", LISTING)
    _write(p, "get_activity_splits", f"{AID}.json", {"lapDTOs": LAPS})
    _write(p, "get_activity_hr_in_timezones", f"{AID}.json", [
        {"zoneNumber": 1, "secsInZone": 100.0, "zoneLowBoundary": 130},
        {"zoneNumber": 2, "secsInZone": 300.0, "zoneLowBoundary": 162},
    ])
    _write(p, "manual_inputs", f"run_{AID}_meta.json", {"lap_categories": ["Marathon", "Rest"]})
    return p


def _items(stats, group):
    return {i["key"]: i for i in next(g for g in stats["groups"] if g["key"] == group)["items"]}


class TestGroups:
    def test_unknown_activity_is_none(self, dp):
        assert dp.get_run_full_stats(1) is None

    def test_distance_and_time_in_user_units(self, dp):
        g = _items(dp.get_run_full_stats(AID), "distance_time")
        assert g["distance"]["value"] == 10.0 and g["distance"]["display"] == "10.00 mi"
        assert g["duration"]["display"] == "1:20:30" and g["duration"]["value"] == 4830.0
        assert g["avg_pace"]["display"] == "8:03 /mi" and g["avg_pace"]["unit"] == "min/mi"
        assert g["gap"]["display"] == "7:53 /mi"
        assert g["best_pace"]["display"] == "5:22 /mi"

    def test_every_item_is_self_describing(self, dp):
        for g in dp.get_run_full_stats(AID)["groups"]:
            for i in g["items"]:
                assert {"key", "label", "value", "unit", "display"} <= set(i)
                assert i["value"] is not None and i["display"]

    def test_training_effect_codes_become_words(self, dp):
        g = _items(dp.get_run_full_stats(AID), "training_effect")
        assert g["aerobic_te"]["value"] == 4.2
        assert g["aerobic_te"]["display"] == "4.2 · Highly impacting tempo"
        assert g["anaerobic_te"]["display"] == "1.0 · Minor anaerobic benefit"
        assert g["primary_benefit"]["display"] == "Lactate threshold"
        assert g["training_load"]["display"] == "250"

    def test_running_dynamics_units(self, dp):
        g = _items(dp.get_run_full_stats(AID), "running_dynamics")
        assert g["stride_length"]["display"] == "1.10 m"       # stored in cm
        assert g["gct_balance"]["display"] == "51.5 / 48.5 %"
        assert g["avg_cadence"]["display"] == "176 spm"

    def test_elevation_in_feet(self, dp):
        g = _items(dp.get_run_full_stats(AID), "elevation")
        assert g["elev_gain"]["display"] == "328 ft" and g["elev_max"]["display"] == "276 ft"

    def test_body_and_self_report(self, dp):
        stats = dp.get_run_full_stats(AID)
        body = _items(stats, "body")
        assert body["stamina"]["display"] == "99 → 41 %"
        assert body["body_battery"]["display"] == "-18"
        assert body["watch_temp"]["display"] == "68 °F" and "warmer" in body["watch_temp"]["note"]
        felt = _items(stats, "self_report")
        assert felt["rpe"]["display"] == "6 / 10"            # Garmin stores 10-100

    def test_missing_values_and_empty_groups_are_dropped(self, dp):
        _write(dp, "get_activity", f"{AID}.json", {"summaryDTO": {"distance": 1609.34, "duration": 600.0}})
        stats = dp.get_run_full_stats(AID)
        assert [g["key"] for g in stats["groups"]] == ["distance_time"]
        assert {i["key"] for i in stats["groups"][0]["items"]} == {"distance", "duration"}


class TestLaps:
    def test_full_row(self, dp):
        lap = dp.get_run_full_stats(AID)["laps"][0]
        assert lap["lap"] == 1 and lap["category"] == "Marathon" and lap["intensity"] == "ACTIVE"
        assert lap["distance_mi"] == 1.0 and lap["time"] == "8:00" and lap["pace"] == "8:00"
        assert lap["gap"] == "7:53" and lap["avg_hr"] == 160.0 and lap["max_hr"] == 170.0
        assert lap["cadence_spm"] == 176 and lap["stride_m"] == 1.12 and lap["gct_ms"] == 240
        assert lap["gct_balance_left"] == 51.0 and lap["vertical_osc_cm"] == 9.0
        assert lap["avg_power_w"] == 380 and lap["elev_gain_ft"] == 33 and lap["elev_loss_ft"] == 10
        assert lap["resp_brpm"] == 35.5

    def test_sparse_lap_has_nulls_not_zeros(self, dp):
        lap = dp.get_run_full_stats(AID)["laps"][1]
        assert lap["category"] == "Rest" and lap["pace"] == "10:00"
        assert lap["gap"] is None and lap["cadence_spm"] is None and lap["elev_gain_ft"] is None

    def test_pace_never_shows_sixty_seconds(self, dp):
        assert DataProcessor._fmt_pace(7.9999) == "8:00"
        assert DataProcessor._fmt_clock(3599.6) == "1:00:00"


class TestZonesAndBests:
    def test_hr_zones_with_boundaries_and_shares(self, dp):
        z = dp.get_run_full_stats(AID)["hr_zones"]
        assert [(r["zone"], r["low"], r["display"], r["pct"]) for r in z] == [
            (1, 130, "1:40", 25.0), (2, 162, "5:00", 75.0)]

    def test_power_zones_fall_back_to_the_activity_listing(self, dp):
        z = dp.get_run_full_stats(AID)["power_zones"]
        assert [(r["zone"], r["seconds"], r["pct"], r["low"]) for r in z] == [(1, 60.0, 60.0, None), (2, 40.0, 40.0, None)]

    def test_no_zone_time_means_no_rows(self, dp):
        _write(dp, "get_activities", f"{AID}_summary.json", {"activityId": AID, "activityType": {"typeKey": "running"}})
        assert dp.get_run_full_stats(AID)["power_zones"] == []

    def test_best_efforts_carry_time_and_pace(self, dp):
        b = dp.get_run_full_stats(AID)["best_efforts"]
        assert [(x["label"], x["display"], x["pace"]) for x in b] == [
            ("1 km", "4:30", "7:15"), ("1 mile", "7:20", "7:20"), ("5 km", "23:20", "7:31")]


class TestTreadmill:
    def test_pace_is_flagged_and_bests_withheld(self, dp):
        _write(dp, "get_activities", f"{AID}_summary.json", {**LISTING, "activityType": {"typeKey": "treadmill_running"}})
        stats = dp.get_run_full_stats(AID)
        assert stats["treadmill"] is True and stats["best_efforts"] == []
        g = _items(stats, "distance_time")
        assert "rough" in g["avg_pace"]["note"] and "rough" in g["distance"]["note"]
        assert "note" not in g["duration"]

    def test_lap_table_carries_the_same_caveat(self, dp):
        assert dp.get_run_full_stats(AID)["lap_caveat"] is None       # outdoors
        _write(dp, "get_activities", f"{AID}_summary.json", {**LISTING, "activityType": {"typeKey": "treadmill_running"}})
        caveat = dp.get_run_full_stats(AID)["lap_caveat"]
        assert "estimates" in caveat and "distance and pace" in caveat


class TestEndpoint:
    def test_passes_the_payload_through(self, client):
        import backend.api_server as api_server

        api_server.processor.get_run_full_stats.return_value = {"activity_id": 5, "groups": []}
        r = client.get("/api/runs/5/stats")
        assert r.status_code == 200 and r.json() == {"activity_id": 5, "groups": []}
        api_server.processor.get_run_full_stats.assert_called_once_with(5)

    def test_unknown_run_is_404(self, client):
        import backend.api_server as api_server

        api_server.processor.get_run_full_stats.return_value = None
        assert client.get("/api/runs/5/stats").status_code == 404
