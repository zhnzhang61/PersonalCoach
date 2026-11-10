"""Sync from Garmin's official Personal API into the legacy on-disk layout.

    python -m backend.garmin_official_sync [--days-back N] [--force-fit]

Why this exists next to `garmin_sync.py`: the legacy sync logs in through
Garmin Connect's web SSO (unofficial; its token expires and needs a
hand-pasted ticket). The Personal API uses a long-lived API key, so it can
run unattended. It exposes fewer endpoints, so this module rebuilds the
files the rest of the app reads from what it does expose:

  search activities      → get_activities/{id}_summary.json   (as returned)
  activity               → get_activity/{id}.json             (as returned)
  activity FIT download  → fit/{id}.fit, then via fit_convert:
                           get_activity_splits/{id}.json
                           get_activity_details/{id}.json
                           get_activity_hr_in_timezones/{id}.json
  usersummary daily      → get_stats_and_body/{date}.json     (as returned)
                           + derived get_rhr_day, get_stress_data,
                             get_respiration_data, get_intensity_minutes_data
  daily sleep            → get_sleep_data/{date}.json   (wrapped as dailySleepDTO)
  daily HRV              → get_hrv_data/{date}.json           (as returned)
  fitness age            → get_fitnessage_data/{date}.json    (as returned)

Not available from the Personal API (left to the legacy sync, or dropped):
activity weather (the app fetches Open-Meteo itself), training status /
readiness, race predictions, gear, intraday stress/respiration curves, the
sleep-stage timeline, body-battery change during sleep.

Every dict this module writes carries `"_source": "garmin_personal_api"`.

Exit codes: 0 ok · 3 no API key configured · 4 key rejected · 1 other
(incl. a failed activity search — never reported as an empty window).
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from typing import Any, Callable

from dotenv import load_dotenv

from backend import fit_convert

load_dotenv()

SOURCE = "garmin_personal_api"
ENV_KEY = "GARMIN_PERSONAL_API_KEY"
EXIT_NO_KEY = 3
EXIT_KEY_REJECTED = 4

# Days this close to today are always re-fetched: Garmin keeps revising
# them while the watch uploads (sleep lands in the morning, stress and
# steps grow all day).
_ALWAYS_REFRESH_DAYS = 2
# Pause between requests. Garmin publishes no rate limit for the Personal
# API ("may throttle"), so stay well clear of anything bursty.
_REQUEST_GAP_S = 0.25


class KeyRejected(Exception):
    """The API key was refused (expired, deleted, or mistyped)."""


class SyncError(Exception):
    """A call the sync cannot do without failed (not an auth problem)."""


def _is_auth_error(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
    return status in (401, 403)


def connect(api_key: str):
    """Exchange the API key for a bearer token.

    Returns (client, refresh). The official library caches the bearer
    token on disk and reuses it while it has more than an hour left, so a
    token Garmin revoked early would keep being served. `refresh()` forces
    a new exchange and installs the new token on the client; the sync
    calls it once when a data call comes back 401/403.
    """
    from garmin_personal_api import GarminPersonalApiClient, PersonalApiAuthenticator

    def exchange(force: bool) -> str:
        try:
            with PersonalApiAuthenticator() as auth:
                return auth.obtain_token(api_key, force_refresh=force).access_token
        except Exception as exc:  # the library raises its own HTTP error types
            if _is_auth_error(exc):
                raise KeyRejected(str(exc)) from exc
            raise

    client = GarminPersonalApiClient(exchange(False))

    def refresh() -> None:
        client.update_bearer_token(exchange(True))

    return client, refresh


class GarminOfficialSync:
    def __init__(self, client: Any, data_dir: str = "data",
                 today: datetime.date | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 refresh_token: Callable[[], None] | None = None):
        self.client = client
        self._refresh_token = refresh_token
        self._token_refreshed = False
        self.data_dir = data_dir
        self.today = today or datetime.date.today()
        self._sleep = sleep
        self.counts: dict[str, int] = {}

    # ------------------------------------------------------------- plumbing
    def _path(self, folder: str, name: str) -> str:
        d = os.path.join(self.data_dir, folder)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)

    def _write(self, folder: str, name: str, data: Any) -> None:
        if isinstance(data, dict):
            data = {**data, "_source": SOURCE}
        path = self._path(folder, name)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=4)
        os.replace(tmp, path)
        self.counts[folder] = self.counts.get(folder, 0) + 1

    def _read(self, folder: str, name: str) -> Any:
        try:
            with open(os.path.join(self.data_dir, folder, name)) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _call(self, label: str, fn: Callable[[], Any], required: bool = False) -> Any:
        """One API call.

        401/403: the cached bearer token may have been revoked while the
        key is still fine, so refresh the token once per sync and retry;
        a second refusal means the key itself is rejected and aborts the
        sync (every later call would fail the same way).
        Any other failure: logged and skipped, so one bad endpoint does
        not cost the rest of the day's data — unless the caller cannot
        proceed without the result (`required`), then it raises SyncError
        instead of looking like an empty answer.
        """
        try:
            try:
                return fn()
            except Exception as exc:
                if not _is_auth_error(exc):
                    raise
                if self._refresh_token is None or self._token_refreshed:
                    raise KeyRejected(str(exc)) from exc
                self._token_refreshed = True
                self._refresh_token()
                try:
                    return fn()
                except Exception as again:
                    if _is_auth_error(again):
                        raise KeyRejected(str(again)) from again
                    raise
        except KeyRejected:
            raise
        except Exception as exc:
            if required:
                raise SyncError(f"{label}: {type(exc).__name__}: {str(exc)[:200]}") from exc
            print(f"   ⚠️ {label}: {type(exc).__name__}: {str(exc)[:160]}")
            return None
        finally:
            self._sleep(_REQUEST_GAP_S)

    # ------------------------------------------------------------ activities
    def sync_activities(self, start: datetime.date, end: datetime.date, force_fit: bool = False) -> list[int]:
        ids: list[int] = []
        offset, page = 0, 50
        while True:
            # Required: a failed search must not read as "no activities".
            batch = self._call("search activities", lambda: self.client.search_activities(
                start=offset, limit=page, start_date=start.isoformat(), end_date=end.isoformat()),
                required=True)
            if isinstance(batch, dict):
                batch = batch.get("activities") or []
            if not batch:
                break
            for act in batch:
                aid = act.get("activityId")
                if aid is None:
                    continue
                ids.append(aid)
                self._write("get_activities", f"{aid}_summary.json", act)
            if len(batch) < page:
                break
            offset += page

        for aid in ids:
            detail = self._call(f"activity {aid}", lambda: self.client.get_activity(aid))
            if isinstance(detail, dict):
                self._write("get_activity", f"{aid}.json", detail)
            self._sync_fit(aid, force_fit)
        return ids

    def _sync_fit(self, aid: int, force: bool) -> None:
        """Download the FIT once and derive laps / per-second / HR zones.
        The FIT never changes after upload, so an existing file is final."""
        fit_path = self._path("fit", f"{aid}.fit")
        if os.path.exists(fit_path) and not force:
            return
        raw = self._call(f"download {aid}", lambda: self.client.download_activity(aid))
        if not raw:
            return
        try:
            data = fit_convert.unzip_fit(bytes(raw))
            fit = fit_convert.read_fit(data)
        except Exception as exc:
            print(f"   ⚠️ FIT {aid} unreadable: {type(exc).__name__}: {exc}")
            return
        tmp = fit_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        laps = fit_convert.laps_to_dtos(fit)
        if laps:
            self._write("get_activity_splits", f"{aid}.json", {"activityId": aid, "lapDTOs": laps})
        details = fit_convert.records_to_details(aid, fit)
        if details["activityDetailMetrics"]:
            self._write("get_activity_details", f"{aid}.json", details)
        zones = fit_convert.hr_time_in_zones(fit)
        if zones:
            self._write("get_activity_hr_in_timezones", f"{aid}.json", zones)
        # The FIT is the "done" marker — write it last so a crash mid-way
        # leaves no marker and the next sync redoes this activity.
        os.replace(tmp, fit_path)
        self.counts["fit"] = self.counts.get("fit", 0) + 1

    # ----------------------------------------------------------------- daily
    def _needs_day(self, day: datetime.date) -> bool:
        if (self.today - day).days <= _ALWAYS_REFRESH_DAYS:
            return True
        name = f"{day.isoformat()}.json"
        stats = self._read("get_stats_and_body", name)
        sleep = self._read("get_sleep_data", name)
        hrv = self._read("get_hrv_data", name)
        sleep_ok = isinstance(sleep, dict) and bool((sleep.get("dailySleepDTO") or {}).get("sleepTimeSeconds"))
        return not (isinstance(stats, dict) and stats and sleep_ok and isinstance(hrv, dict) and hrv.get("hrvSummary"))

    def sync_day(self, day: datetime.date) -> None:
        d = day.isoformat()
        name = f"{d}.json"

        stats = self._call(f"daily summary {d}", lambda: self.client.get(
            "/usersummary-service/usersummary/daily", params={"calendarDate": d}))
        hrv = self._call(f"hrv {d}", lambda: self.client.get_daily_hrv(d))
        sleep = self._call(f"sleep {d}", lambda: self.client.get_daily_sleep(d))

        if isinstance(stats, dict) and stats:
            self._write("get_stats_and_body", name, stats)
            if stats.get("restingHeartRate") is not None:
                self._write("get_rhr_day", name, {
                    "statisticsStartDate": d, "statisticsEndDate": d,
                    "allMetrics": {"metricsMap": {"WELLNESS_RESTING_HEART_RATE": [
                        {"value": float(stats["restingHeartRate"]), "calendarDate": d}]}},
                })
            self._write("get_stress_data", name, {
                "calendarDate": d,
                "avgStressLevel": stats.get("averageStressLevel"),
                "maxStressLevel": stats.get("maxStressLevel"),
            })
            self._write("get_respiration_data", name, {
                "calendarDate": d,
                "avgWakingRespirationValue": stats.get("avgWakingRespirationValue"),
                "highestRespirationValue": stats.get("highestRespirationValue"),
                "lowestRespirationValue": stats.get("lowestRespirationValue"),
                "avgSleepRespirationValue": (sleep or {}).get("averageRespirationValue") if isinstance(sleep, dict) else None,
            })
            self._write("get_intensity_minutes_data", name, self._intensity_minutes(day, stats))

        if isinstance(hrv, dict) and hrv:
            self._write("get_hrv_data", name, hrv)

        if isinstance(sleep, dict) and sleep:
            summary = (hrv or {}).get("hrvSummary") or {} if isinstance(hrv, dict) else {}
            self._write("get_sleep_data", name, {
                "dailySleepDTO": sleep,
                "restingHeartRate": (stats or {}).get("restingHeartRate") if isinstance(stats, dict) else None,
                "avgOvernightHrv": summary.get("lastNightAvg"),
                "hrvStatus": summary.get("status"),
                # Not exposed by the Personal API (the legacy sync has it).
                "bodyBatteryChange": None,
            })

    def _intensity_minutes(self, day: datetime.date, stats: dict) -> dict:
        """The legacy file carried week-to-date totals; rebuild them from
        the daily summaries of this ISO week that are on disk."""
        moderate = stats.get("moderateIntensityMinutes") or 0
        vigorous = stats.get("vigorousIntensityMinutes") or 0
        week_mod, week_vig = moderate, vigorous
        for back in range(1, day.weekday() + 1):
            prev = self._read("get_stats_and_body", f"{(day - datetime.timedelta(days=back)).isoformat()}.json")
            if isinstance(prev, dict):
                week_mod += prev.get("moderateIntensityMinutes") or 0
                week_vig += prev.get("vigorousIntensityMinutes") or 0
        return {
            "calendarDate": day.isoformat(),
            "moderateMinutes": moderate,
            "vigorousMinutes": vigorous,
            "weeklyModerate": week_mod,
            "weeklyVigorous": week_vig,
            # Garmin counts vigorous minutes double toward the weekly goal.
            "weeklyTotal": week_mod + 2 * week_vig,
            "weekGoal": stats.get("intensityMinutesGoal"),
        }

    # ------------------------------------------------------------------ run
    def run_sync(self, days_back: int = 7, force_fit: bool = False) -> dict[str, int]:
        start = self.today - datetime.timedelta(days=days_back)
        print(f"🚀 Garmin Personal API sync: {start} → {self.today}")
        ids = self.sync_activities(start, self.today, force_fit=force_fit)
        print(f"   activities in window: {len(ids)}")
        # Oldest first, so week-to-date intensity minutes see earlier days.
        for back in range(days_back, -1, -1):
            day = self.today - datetime.timedelta(days=back)
            if self._needs_day(day):
                self.sync_day(day)
        age = self._call("fitness age", lambda: self.client.get_fitness_age(self.today.isoformat()))
        if isinstance(age, dict) and age:
            self._write("get_fitnessage_data", f"{self.today.isoformat()}.json", age)
        print("✅ wrote: " + ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items())))
        return self.counts


def _int_flag(argv: list[str], flag: str, default: int, lo: int, hi: int) -> int:
    if flag in argv:
        try:
            return max(lo, min(hi, int(argv[argv.index(flag) + 1])))
        except (IndexError, ValueError):
            return default
    return default


def main(argv: list[str]) -> int:
    api_key = os.getenv(ENV_KEY)
    if not api_key:
        print(f"NO_API_KEY: set {ENV_KEY} in .env", file=sys.stderr)
        return EXIT_NO_KEY
    try:
        client, refresh = connect(api_key)
    except KeyRejected as exc:
        print(f"KEY_REJECTED: {exc}", file=sys.stderr)
        return EXIT_KEY_REJECTED
    try:
        GarminOfficialSync(client, refresh_token=refresh).run_sync(
            days_back=_int_flag(argv, "--days-back", 7, 1, 365),
            force_fit="--force-fit" in argv,
        )
    except KeyRejected as exc:
        print(f"KEY_REJECTED: {exc}", file=sys.stderr)
        return EXIT_KEY_REJECTED
    except SyncError as exc:
        print(f"SYNC_FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        close = getattr(client, "close", None)
        if close:
            close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
