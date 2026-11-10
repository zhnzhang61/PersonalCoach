"""FIT file → the JSON shapes the legacy Garmin Connect sync wrote.

The official Garmin Personal API has no per-lap or per-second endpoints;
it hands back the original FIT file instead. Everything downstream
(`data_processor.py`, the web app, analysis scripts) reads three legacy
shapes, so this module rebuilds exactly those from the FIT file:

  laps_to_dtos()      → get_activity_splits/{id}.json        ("lapDTOs")
  records_to_details()→ get_activity_details/{id}.json       (metric table + polyline)
  hr_time_in_zones()  → get_activity_hr_in_timezones/{id}.json

Field mapping was established by matching a real marathon's FIT against
the legacy files value-for-value (data/ai-tool/garmin-official-parity).
Several Garmin-proprietary fields have no name in the public FIT profile
and are addressed by field number:

  record #90  performance condition      record #137 potential stamina
  record #138 available stamina          record #143 body battery
  record #140 grade-adjusted speed, mm/s lap    #161 avg grade-adjusted speed, mm/s
  lap    #155 BMR calories
"""

from __future__ import annotations

import datetime
import io
import zipfile
from typing import Any

import fitdecode

_SEMICIRCLE_DEG = 180.0 / 2**31
_WANTED = ("record", "lap", "session", "time_in_zone", "event")

# Below this speed a second does not count as "moving". 1.0 m/s reproduces
# Garmin Connect's per-lap movingDuration to within 0.5 s on average
# (131 real laps; 1.2 m/s and above start dropping walking breaks).
_MOVING_MPS = 1.0

# Order is the on-disk column order of activityDetailMetrics rows.
DETAIL_KEYS = (
    "directTimestamp",
    "sumElapsedDuration",
    "sumDuration",
    "sumMovingDuration",
    "sumDistance",
    "directSpeed",
    "directGradeAdjustedSpeed",
    "directElevation",
    "directVerticalSpeed",
    "directHeartRate",
    "directRespirationRate",
    "directRunCadence",
    "directDoubleCadence",
    "directFractionalCadence",
    "directStrideLength",
    "directGroundContactTime",
    "directGroundContactBalanceLeft",
    "directVerticalOscillation",
    "directVerticalRatio",
    "directPower",
    "sumAccumulatedPower",
    "directAirTemperature",
    "directPerformanceCondition",
    "directPotentialStamina",
    "directAvailableStamina",
    "directBodyBattery",
    "directLatitude",
    "directLongitude",
)

_MAX_POLYLINE_POINTS = 1500
# Garmin Connect's details endpoint thinned long activities to roughly
# this many rows; the run page ships them to the phone as-is, so match it.
# The untouched 1 Hz stream stays in the FIT file.
DEFAULT_MAX_DETAIL_ROWS = 2000


def read_fit(data: bytes) -> dict[str, list[dict[str, Any]]]:
    """Decode the messages we use. Accepts a raw .fit or the .zip the
    download endpoint returns. Unnamed fields are keyed "#<field number>"."""
    data = unzip_fit(data)
    out: dict[str, list[dict[str, Any]]] = {k: [] for k in _WANTED}
    with fitdecode.FitReader(io.BytesIO(data)) as reader:
        for msg in reader:
            if not isinstance(msg, fitdecode.FitDataMessage) or msg.name not in out:
                continue
            row: dict[str, Any] = {}
            for f in msg.fields:
                key = f"#{f.def_num}" if f.name.startswith("unknown_") else f.name
                row[key] = f.value
            out[msg.name].append(row)
    return out


def unzip_fit(data: bytes) -> bytes:
    """The raw .fit bytes, whether `data` is a .fit or the download .zip."""
    while data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".fit"))
            data = z.read(name)
    return data


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _div(v: Any, d: float) -> float | None:
    n = _num(v)
    return None if n is None else n / d


def _deg(v: Any) -> float | None:
    n = _num(v)
    return None if n is None else n * _SEMICIRCLE_DEG


def _epoch(v: Any) -> float | None:
    if isinstance(v, datetime.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=datetime.timezone.utc)
        return v.timestamp()
    return None


def _gmt(v: Any) -> str | None:
    t = _epoch(v)
    if t is None:
        return None
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0")


def _cadence(whole: Any, frac: Any) -> float | None:
    """FIT stores running cadence as strides/min in two parts; the legacy
    files use steps/min."""
    w = _num(whole)
    if w is None:
        return None
    return (w + (_num(frac) or 0.0)) * 2


def laps_to_dtos(fit: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    records = fit.get("record") or []
    rec_t = [_epoch(r.get("timestamp")) for r in records]
    dtos = []
    for i, lap in enumerate(fit.get("lap") or []):
        dist = _num(lap.get("total_distance")) or 0.0
        elapsed = _num(lap.get("total_elapsed_time")) or 0.0
        timer = _num(lap.get("total_timer_time")) or elapsed
        # A lap's own `timestamp` is not reliably its end time in Garmin's
        # files, so the window is start_time + elapsed.
        t0 = _epoch(lap.get("start_time"))
        t1 = t0 + elapsed if t0 is not None else None

        # movingDuration, maxSpeed and the elevation range are not stored
        # per lap (or differ from Garmin Connect's); derive them
        # from the lap's own records.
        moving, max_speed, prev_t = 0.0, None, None
        alts: list[float] = []
        if t0 is not None and t1 is not None:
            for t, r in zip(rec_t, records):
                if t is None or t <= t0 or t > t1:
                    continue
                a = _num(r.get("enhanced_altitude"))
                if a is not None:
                    alts.append(a)
                s = _num(r.get("enhanced_speed"))
                if s is not None:
                    max_speed = s if max_speed is None else max(max_speed, s)
                    dt = t - (prev_t if prev_t is not None else t0)
                    if s >= _MOVING_MPS and 0 < dt <= 10:
                        moving += dt
                prev_t = t
        moving = min(moving, timer) if moving > 0 else timer
        native_moving = _num(lap.get("total_moving_time"))
        if native_moving:
            moving = native_moving

        intensity = lap.get("intensity")
        idx = int(_num(lap.get("message_index")) or i)
        dtos.append({
            "startTimeGMT": _gmt(lap.get("start_time")),
            "startLatitude": _deg(lap.get("start_position_lat")),
            "startLongitude": _deg(lap.get("start_position_long")),
            "endLatitude": _deg(lap.get("end_position_lat")),
            "endLongitude": _deg(lap.get("end_position_long")),
            "distance": dist,
            "duration": timer,
            "movingDuration": moving,
            "elapsedDuration": elapsed,
            "elevationGain": _num(lap.get("total_ascent")),
            "elevationLoss": _num(lap.get("total_descent")),
            "maxElevation": max(alts) if alts else _num(lap.get("enhanced_max_altitude")),
            "minElevation": min(alts) if alts else _num(lap.get("enhanced_min_altitude")),
            "averageSpeed": dist / timer if timer else None,
            "averageMovingSpeed": dist / moving if moving else None,
            "maxSpeed": _num(lap.get("enhanced_max_speed")) or max_speed,
            "avgGradeAdjustedSpeed": _div(lap.get("#161"), 1000.0),
            "calories": _num(lap.get("total_calories")),
            "bmrCalories": _num(lap.get("#155")),
            "averageHR": _num(lap.get("avg_heart_rate")),
            "maxHR": _num(lap.get("max_heart_rate")),
            "averageRunCadence": _cadence(lap.get("avg_running_cadence"), lap.get("avg_fractional_cadence")),
            "maxRunCadence": _cadence(lap.get("max_running_cadence"), lap.get("max_fractional_cadence")),
            "averageTemperature": _num(lap.get("avg_temperature")),
            "maxTemperature": _num(lap.get("max_temperature")),
            "minTemperature": _num(lap.get("min_temperature")),
            "averagePower": _num(lap.get("avg_power")),
            "maxPower": _num(lap.get("max_power")),
            "normalizedPower": _num(lap.get("normalized_power")),
            "groundContactTime": _num(lap.get("avg_stance_time")),
            "groundContactBalanceLeft": _num(lap.get("avg_stance_time_balance")),
            "strideLength": _div(lap.get("avg_step_length"), 10.0),
            "verticalOscillation": _div(lap.get("avg_vertical_oscillation"), 10.0),
            "verticalRatio": _num(lap.get("avg_vertical_ratio")),
            "avgRespirationRate": _num(lap.get("enhanced_avg_respiration_rate")),
            "maxRespirationRate": _num(lap.get("enhanced_max_respiration_rate")),
            "lapIndex": idx + 1,
            "lengthDTOs": [],
            "connectIQMeasurement": [],
            "intensityType": intensity.upper() if isinstance(intensity, str) else None,
            "messageIndex": idx,
        })
    return dtos


def _pauses(fit: dict[str, list[dict[str, Any]]]) -> list[tuple[float, float]]:
    """[(stop, start)] intervals when the timer was paused."""
    out, stop = [], None
    for ev in fit.get("event") or []:
        if ev.get("event") != "timer":
            continue
        t = _epoch(ev.get("timestamp"))
        if t is None:
            continue
        kind = ev.get("event_type")
        if isinstance(kind, str) and kind.startswith("stop"):
            stop = t if stop is None else stop
        elif kind == "start" and stop is not None:
            out.append((stop, t))
            stop = None
    return out


def _thin(items: list, limit: int | None) -> list:
    """Every k-th item so at most ~limit remain, always keeping the last."""
    if not limit or len(items) <= limit:
        return items
    step = -(-len(items) // limit)
    out = items[::step]
    if out[-1] is not items[-1]:
        out.append(items[-1])
    return out


def records_to_details(activity_id: int, fit: dict[str, list[dict[str, Any]]],
                       max_rows: int | None = DEFAULT_MAX_DETAIL_ROWS) -> dict[str, Any]:
    records = [r for r in (fit.get("record") or []) if _epoch(r.get("timestamp")) is not None]
    session = (fit.get("session") or [{}])[0]
    t_start = _epoch(session.get("start_time"))
    if t_start is None and records:
        t_start = _epoch(records[0]["timestamp"])
    pauses = _pauses(fit)

    rows, poly = [], []
    moving, prev_t, prev_alt, prev_alt_t = 0.0, None, None, None
    for r in records:
        t = _epoch(r["timestamp"])
        elapsed = t - t_start
        paused = sum(min(t, b) - a for a, b in pauses if a < t)
        speed = _num(r.get("enhanced_speed"))
        if prev_t is not None and speed is not None and speed >= _MOVING_MPS and 0 < t - prev_t <= 10:
            moving += t - prev_t
        prev_t = t

        alt = _num(r.get("enhanced_altitude"))
        vspeed = None
        if alt is not None:
            if prev_alt is not None and t > prev_alt_t:
                vspeed = (alt - prev_alt) / (t - prev_alt_t)
            prev_alt, prev_alt_t = alt, t

        cad = _num(r.get("cadence"))
        frac = _num(r.get("fractional_cadence")) or 0.0
        lat, lon = _deg(r.get("position_lat")), _deg(r.get("position_long"))
        values = {
            "directTimestamp": int(t * 1000),
            "sumElapsedDuration": elapsed,
            "sumDuration": max(0.0, elapsed - paused),
            "sumMovingDuration": moving,
            "sumDistance": _num(r.get("distance")),
            "directSpeed": speed,
            "directGradeAdjustedSpeed": _div(r.get("#140"), 1000.0),
            "directElevation": alt,
            "directVerticalSpeed": vspeed,
            "directHeartRate": _num(r.get("heart_rate")),
            "directRespirationRate": _num(r.get("enhanced_respiration_rate")),
            "directRunCadence": cad,
            "directDoubleCadence": None if cad is None else (cad + frac) * 2,
            "directFractionalCadence": _num(r.get("fractional_cadence")),
            "directStrideLength": _div(r.get("step_length"), 10.0),
            "directGroundContactTime": _num(r.get("stance_time")),
            "directGroundContactBalanceLeft": _num(r.get("stance_time_balance")),
            "directVerticalOscillation": _div(r.get("vertical_oscillation"), 10.0),
            "directVerticalRatio": _num(r.get("vertical_ratio")),
            "directPower": _num(r.get("power")),
            "sumAccumulatedPower": _num(r.get("accumulated_power")),
            "directAirTemperature": _num(r.get("temperature")),
            "directPerformanceCondition": _num(r.get("#90")),
            "directPotentialStamina": _num(r.get("#137")),
            "directAvailableStamina": _num(r.get("#138")),
            "directBodyBattery": _num(r.get("#143")),
            "directLatitude": lat,
            "directLongitude": lon,
        }
        rows.append({"metrics": [values[k] for k in DETAIL_KEYS]})
        if lat is not None and lon is not None:
            poly.append({"lat": lat, "lon": lon, "time": int(t * 1000), "altitude": alt,
                         "speed": speed, "valid": True})

    geo: dict[str, Any] = {"startPoint": None, "endPoint": None, "polyline": []}
    if poly:
        thinned = _thin(poly, _MAX_POLYLINE_POINTS)
        geo = {
            "startPoint": poly[0],
            "endPoint": poly[-1],
            "minLat": min(p["lat"] for p in poly),
            "maxLat": max(p["lat"] for p in poly),
            "minLon": min(p["lon"] for p in poly),
            "maxLon": max(p["lon"] for p in poly),
            "polyline": thinned,
        }
    total = len(rows)
    # Cumulative columns were computed on every record above, so thinning
    # rows afterwards keeps them exact.
    rows = _thin(rows, max_rows)
    return {
        "activityId": activity_id,
        "measurementCount": len(DETAIL_KEYS),
        "metricsCount": len(rows),
        "totalMetricsCount": total,
        "metricDescriptors": [{"metricsIndex": i, "key": k} for i, k in enumerate(DETAIL_KEYS)],
        "activityDetailMetrics": rows,
        "geoPolylineDTO": geo,
        "heartRateDTOs": None,
        "pendingData": None,
        "detailsAvailable": bool(rows),
    }


def hr_time_in_zones(fit: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Seconds per heart-rate zone for the whole activity. FIT's array has
    a leading "below zone 1" bucket; zone N is index N and starts at the
    previous zone's upper boundary."""
    for tz in fit.get("time_in_zone") or []:
        if tz.get("reference_mesg") != "session":
            continue
        secs = tz.get("time_in_hr_zone") or ()
        bounds = tz.get("hr_zone_high_boundary") or ()
        out = []
        for n in range(1, len(secs)):
            if n - 1 >= len(bounds) or n > 5:
                break
            s = _num(secs[n]) or 0.0
            if n == 5:  # everything above the zone-5 floor, incl. over max HR
                s += sum(_num(x) or 0.0 for x in secs[6:])
            out.append({"zoneNumber": n, "secsInZone": s, "zoneLowBoundary": _num(bounds[n - 1])})
        return out
    return []
