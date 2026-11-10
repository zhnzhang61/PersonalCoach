"""Strava: rename the athlete's own activity and link to it.

Two jobs, both for the single user's own account:
  1. when a run is named in this app, give its Strava twin the same title;
  2. give the app a URL to jump to that Strava activity.

The activities reach Strava through Garmin's own auto-sync, so there is no
shared id. The twin is found by start time: list the athlete's Strava
activities around the Garmin start and take the nearest.

Strava API Policy (effective 2026-06-01) shapes the design:
  - §5.3 forbids putting Strava-read data into any AI application. This
    module therefore returns only what the two jobs need (an activity id);
    nothing read from Strava is written to disk, logged, or handed to the
    coach. Titles are generated from Garmin/app data only.
  - Cached Strava data may be kept 7 days at most, so the Garmin→Strava id
    pairing is looked up on demand (kept in memory only, for a day at most).

Credentials: STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET in .env (the user's
own API application). Tokens: data/oauth/strava_token.json.
"""

from __future__ import annotations

import datetime
import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv

load_dotenv()

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
SCOPES = "activity:read_all,activity:write"
ACTIVITY_URL = "https://www.strava.com/activities/{id}"

# Garmin and Strava normally agree on the start second, but offsets of a
# few minutes have been reported. Speed days put three recordings 15+ min
# apart, so 5 minutes still cannot confuse warm-up with main set.
MATCH_TOLERANCE_S = 300
_REFRESH_MARGIN_S = 300
# How long a Garmin→Strava id pairing is remembered. Policy §6.2 caps any
# cache of Strava data at 7 days; a day is plenty to cover "save the name,
# then tap the link" and stays far inside that even if the server never
# restarts.
_ID_CACHE_TTL_S = 24 * 3600


class StravaError(Exception):
    pass


class NotConnected(StravaError):
    """No client credentials, or the user has not authorized yet."""


class StravaClient:
    def __init__(self, token_path: str | Path = "data/oauth/strava_token.json",
                 http: Any = requests, now: Callable[[], float] = time.time,
                 env: Callable[[str], str | None] = os.getenv):
        self.token_path = Path(token_path)
        self._http = http
        self._now = now
        self._env = env
        # garmin activity id → (strava activity id, time it was looked up)
        self._ids: dict[int, tuple[int, float]] = {}

    # ------------------------------------------------------------ credentials
    def _client(self) -> tuple[str, str]:
        cid, secret = self._env("STRAVA_CLIENT_ID"), self._env("STRAVA_CLIENT_SECRET")
        if not cid or not secret:
            raise NotConnected("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET not set in .env")
        return cid, secret

    def configured(self) -> bool:
        return bool(self._env("STRAVA_CLIENT_ID") and self._env("STRAVA_CLIENT_SECRET"))

    def _load(self) -> dict | None:
        try:
            return json.loads(self.token_path.read_text())
        except (OSError, ValueError):
            return None

    def _save(self, tok: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.token_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "access_token": tok["access_token"],
            "refresh_token": tok["refresh_token"],
            "expires_at": tok["expires_at"],
            "scope": tok.get("scope"),
        }))
        os.replace(tmp, self.token_path)

    def connected(self) -> bool:
        tok = self._load()
        return bool(self.configured() and tok and tok.get("refresh_token"))

    def can_write(self) -> bool:
        """The user may untick "edit activities" on Strava's consent page;
        then links work but renames would 401."""
        tok = self._load() or {}
        scope = tok.get("scope")
        return self.connected() and (scope is None or "activity:write" in scope)

    def disconnect(self) -> None:
        self.token_path.unlink(missing_ok=True)
        self._ids.clear()

    # ------------------------------------------------------------------ oauth
    def authorize_url(self, redirect_uri: str) -> str:
        cid, _ = self._client()
        return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
            "client_id": cid, "response_type": "code", "redirect_uri": redirect_uri,
            "approval_prompt": "auto", "scope": SCOPES,
        })

    def _token_request(self, **form: str) -> dict:
        cid, secret = self._client()
        r = self._http.post(TOKEN_URL, data={"client_id": cid, "client_secret": secret, **form}, timeout=15)
        if r.status_code != 200:
            raise StravaError(f"token endpoint returned HTTP {r.status_code}")
        return r.json()

    def exchange_code(self, code: str, granted_scope: str | None = None) -> None:
        tok = self._token_request(code=code, grant_type="authorization_code")
        tok["scope"] = granted_scope
        self._save(tok)

    def _access_token(self) -> str:
        tok = self._load()
        if not tok or not tok.get("refresh_token"):
            raise NotConnected("Strava not authorized yet")
        if tok.get("expires_at", 0) - self._now() > _REFRESH_MARGIN_S:
            return tok["access_token"]
        new = self._token_request(grant_type="refresh_token", refresh_token=tok["refresh_token"])
        # Strava may rotate the refresh token; the old one dies at once.
        new["scope"] = tok.get("scope")
        new.setdefault("refresh_token", tok["refresh_token"])
        self._save(new)
        return new["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    # --------------------------------------------------------------- matching
    def find_activity_id(self, garmin_id: int, start_utc: datetime.datetime,
                         distance_m: float | None = None) -> int | None:
        """Strava id of the activity that started at `start_utc`, or None."""
        cached = self._ids.get(garmin_id)
        if cached is not None:
            if self._now() - cached[1] < _ID_CACHE_TTL_S:
                return cached[0]
            del self._ids[garmin_id]
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=datetime.timezone.utc)
        t = start_utc.timestamp()
        r = self._http.get(f"{API}/athlete/activities", headers=self._headers(), timeout=15, params={
            "after": int(t - MATCH_TOLERANCE_S - 1), "before": int(t + MATCH_TOLERANCE_S + 1), "per_page": 30,
        })
        if r.status_code != 200:
            raise StravaError(f"activity list returned HTTP {r.status_code}")
        best: tuple[float, float, int] | None = None
        for act in r.json() or []:
            try:
                st = datetime.datetime.fromisoformat(str(act["start_date"]).replace("Z", "+00:00")).timestamp()
                sid = int(act["id"])
            except (KeyError, TypeError, ValueError):
                continue
            dt = abs(st - t)
            if dt > MATCH_TOLERANCE_S:
                continue
            # Start time decides; distance only breaks ties between
            # recordings that began in the same window.
            dd = abs((act.get("distance") or 0) - distance_m) if distance_m else 0.0
            key = (dt, dd, sid)
            if best is None or key < best:
                best = key
        if best is None:
            return None
        self._ids[garmin_id] = (best[2], self._now())
        return best[2]

    def activity_url(self, strava_id: int) -> str:
        return ACTIVITY_URL.format(id=strava_id)

    # ----------------------------------------------------------------- rename
    def rename(self, strava_id: int, name: str) -> None:
        r = self._http.put(f"{API}/activities/{strava_id}", headers=self._headers(),
                           data={"name": name}, timeout=15)
        if r.status_code != 200:
            raise StravaError(f"rename returned HTTP {r.status_code}")


def garmin_start_utc(summary: dict) -> datetime.datetime | None:
    """`startTimeGMT` of a Garmin activity summary ("YYYY-MM-DD HH:MM:SS"
    or ISO with a trailing ".0") as an aware UTC datetime."""
    raw = summary.get("startTimeGMT")
    if not raw:
        return None
    raw = str(raw).replace("T", " ").split(".")[0]
    try:
        return datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
