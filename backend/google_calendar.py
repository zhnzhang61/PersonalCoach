"""Google Calendar integration — OAuth flow + event fetch + write.

The Personal Coach app authenticates against the user's personal Google
account and reads / writes events on their primary calendar. This is
single-user (the owner runs it locally), so we store the refresh token
in a JSON file under data/oauth/ — sqlite would be overkill for one row.

Phase 1 was read-only (calendar.readonly). PR P4a (2026-05-27) switched
to calendar.events (read+write) so AI-proposed workout plans can be
written directly to the user's calendar. Existing tokens re-authorize
on next /api/calendar/connect; the consent screen will now ask only
for the events scope. (We dropped readonly from the SCOPES list — see
the SCOPES comment below for why mixing them broke OAuth.)
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

# oauthlib refuses non-HTTPS callbacks by default ("OAuth 2 MUST utilize
# https"). The OAUTHLIB_INSECURE_TRANSPORT escape hatch is documented
# strictly for local testing — flipping it on globally would also disable
# the check for any HTTPS deployment that imports this module, defeating
# the purpose. Gate the bypass to the actual localhost redirect case so
# a future TLS deploy still gets the protection.
def _maybe_allow_insecure_localhost() -> None:
    redirect = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "")
    if redirect.startswith(("http://localhost", "http://127.0.0.1")):
        # setdefault rather than overwrite so the operator can still
        # explicitly disable it via env if they want strict checking.
        os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


_maybe_allow_insecure_localhost()

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Phase 2 (PR P4a): switched to "calendar.events" so we can write
# AI-proposed workout plans back to the user's calendar.
#
# We previously listed both "calendar.readonly" AND "calendar.events" —
# but Google's consent screen consolidates them (events ⊇ readonly for
# the events resource) and returns a token whose granted scopes are
# just ["calendar.events"]. google_auth_oauthlib.fetch_token then
# raises "Scope has changed from 'A B' to 'B'" and the callback errors
# out — the user clicks Connect, returns, and is_connected() is still
# False because no token ever got persisted. (This bit a real user on
# 2026-05-27.) Listing just calendar.events avoids the mismatch and is
# functionally identical: it grants read+write on events, which is
# everything this module actually does.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
]

# Marker line we prefix every AI-written event's description with so we
# can (a) tell our events apart from the user's own when surfacing
# planned workouts in /api/planned-workouts (read merge), and (b) round-
# trip structured fields (target_pace, target_hr, etc.) through Google's
# free-form description field. Convention: `key: value` per line.
PLANNED_WORKOUT_MARKER = "personalcoach.training=true"


def _client_config() -> dict[str, Any]:
    """Build a google_auth_oauthlib client_config dict from env vars.

    We keep credentials in .env rather than a downloaded JSON so the
    secret doesn't sit in the repo and rotation is just an env change.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    redirect_uri = os.environ.get(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "http://localhost:8765/oauth/google/callback",
    )
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET missing from env"
        )
    return {
        "web": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [redirect_uri],
        }
    }


class GoogleCalendar:
    """Single-user Google Calendar adapter.

    Token state lives at <data_dir>/oauth/google_token.json. Frontend
    triggers OAuth via /oauth/google/start; on success the callback writes
    the refresh-token-bearing creds to disk and subsequent calls reuse them
    until revoked.
    """

    def __init__(self, data_dir: str = "data") -> None:
        self.token_path = Path(data_dir) / "oauth" / "google_token.json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        # state → PKCE code_verifier. Library generates a fresh verifier
        # in authorization_url(); the same one needs to be re-attached to
        # the Flow we build for token exchange in finish_flow(), or
        # Google rejects with "(invalid_grant) Missing code verifier".
        # Single-user dev → in-memory dict is fine; abandoned flows leak
        # entries until process restart.
        self._pending_verifiers: dict[str, str] = {}

    # ---- OAuth flow ------------------------------------------------------

    def build_flow(self, state: str | None = None) -> Flow:
        cfg = _client_config()
        flow = Flow.from_client_config(cfg, scopes=SCOPES, state=state)
        flow.redirect_uri = cfg["web"]["redirect_uris"][0]
        return flow

    def authorization_url(self) -> tuple[str, str]:
        flow = self.build_flow()
        url, state = flow.authorization_url(
            # offline so Google issues a refresh_token (otherwise only the
            # access_token comes back and we'd re-prompt every hour).
            access_type="offline",
            # force consent the first time so refresh_token always returns;
            # subsequent runs without prompt would skip it.
            prompt="consent",
            include_granted_scopes="true",
        )
        self._pending_verifiers[state] = flow.code_verifier
        return url, state

    def finish_flow(self, authorization_response: str, state: str) -> None:
        flow = self.build_flow(state=state)
        # Re-attach the verifier the library stashed on the previous Flow
        # instance. Pop rather than read so a successful exchange clears
        # the entry; an abandoned flow leaks until restart, which is fine
        # for single-user dev.
        verifier = self._pending_verifiers.pop(state, None)
        if verifier:
            flow.code_verifier = verifier
        flow.fetch_token(authorization_response=authorization_response)
        creds = flow.credentials
        self._save_creds(creds)

    # ---- Token storage ---------------------------------------------------

    def _save_creds(self, creds: Credentials) -> None:
        self.token_path.write_text(creds.to_json())

    def _load_creds(self) -> Credentials | None:
        if not self.token_path.exists():
            return None
        creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if not creds:
            return None
        # Refresh if expired and we have a refresh token. Save back so the
        # rotated access_token sticks across restarts.
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self._save_creds(creds)
        return creds

    def connection_state(self) -> str:
        """Coarse connection status for the UI. One of:

        - ``"disconnected"`` — no token on disk: never linked (or the
          user disconnected). UI shows a first-time "Connect".
        - ``"expired"`` — a token IS on disk but loading/refreshing it
          failed: the refresh token was revoked or hit Google's 7-day
          "Testing"-mode expiry, or the file is corrupt. UI should say
          "session expired, reconnect" — NOT pretend it was never linked.
        - ``"connected"`` — creds load (refreshing if needed) cleanly.

        Why this exists: ``is_connected()`` collapsed "expired" and
        "never connected" into a single ``False``, so the Training tab
        showed a first-time "Connect Google Calendar" prompt every time
        the daily refresh token died — giving no hint that the fix is a
        re-auth of an existing link. (The refresh-token death itself is
        expected on a "Testing"-status OAuth app; see the SCOPES note and
        docs/PROJECT_GUIDE.md §3.2.)
        """
        if not self.token_path.exists():
            return "disconnected"
        try:
            return "connected" if self._load_creds() is not None else "disconnected"
        except RefreshError:
            # Refresh token revoked / expired (Testing-mode 7-day cap).
            return "expired"
        except Exception:
            # Corrupt / schema-incompatible token file. There IS a file,
            # so surface "reconnect", not "never connected".
            return "expired"

    def is_connected(self) -> bool:
        return self.connection_state() == "connected"

    def disconnect(self) -> None:
        if self.token_path.exists():
            self.token_path.unlink()

    # ---- Events ----------------------------------------------------------

    def list_events(
        self,
        start: datetime.datetime,
        end: datetime.datetime,
        calendar_id: str = "primary",
    ) -> list[dict[str, Any]]:
        """Fetch events overlapping [start, end] from the user's primary
        calendar. Returns a normalised dict shape suitable for both the
        frontend calendar and AI prompts:

            {
              source: "google",
              id: str,
              title: str,
              start: ISO datetime,
              end: ISO datetime,
              all_day: bool,
              location: str | None,
              description: str | None,
              calendar_id: str,
            }

        Recurring events are expanded server-side (singleEvents=True), so
        the caller sees concrete instances rather than RRULEs.
        """
        creds = self._load_creds()
        if creds is None:
            raise RuntimeError("Google Calendar not connected")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        try:
            resp = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=2500,
                )
                .execute()
            )
        except HttpError as e:
            raise RuntimeError(f"Google Calendar API error: {e}") from e

        out: list[dict[str, Any]] = []
        for ev in resp.get("items", []):
            start_obj = ev.get("start", {})
            end_obj = ev.get("end", {})
            all_day = "date" in start_obj
            out.append({
                "source": "google",
                "id": ev.get("id"),
                "title": ev.get("summary") or "(no title)",
                "start": start_obj.get("dateTime") or start_obj.get("date"),
                "end": end_obj.get("dateTime") or end_obj.get("date"),
                "all_day": all_day,
                "location": ev.get("location"),
                "description": ev.get("description"),
                "calendar_id": calendar_id,
            })
        return out

    # ---- Write (PR P4a) --------------------------------------------------
    #
    # `insert_event` / `update_event` / `delete_event` mirror the
    # google.events.* API surface but with our normalized shape (start
    # / end as ISO 8601 strings, description as plain str). Callers
    # are expected to embed the PLANNED_WORKOUT_MARKER + key:value
    # pairs in `description` themselves — this class doesn't know
    # about workout schema, only about Calendar events.

    def insert_event(
        self,
        *,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        reminders: dict[str, Any] | None = None,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """Create a new event on the user's calendar. `start` / `end`
        are ISO 8601: a date-only string (YYYY-MM-DD) creates an
        all-day event, datetime with time creates a timed event.
        Returns the normalized event dict (same shape list_events
        emits) so callers can stash the returned id.

        `reminders` is passed through to Google's API verbatim. None
        means "use the user's calendar default" (Google's behavior
        when the field is omitted). To force silence, pass
        `{"useDefault": False, "overrides": []}` — see the
        planned-workout writer in api_server._plan_to_cal_payload."""
        creds = self._load_creds()
        if creds is None:
            raise RuntimeError("Google Calendar not connected")
        body: dict[str, Any] = {"summary": summary}
        if description:
            body["description"] = description
        body["start"] = _iso_to_event_time(start)
        body["end"] = _iso_to_event_time(end)
        if reminders is not None:
            body["reminders"] = reminders
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        try:
            ev = (
                service.events()
                .insert(calendarId=calendar_id, body=body)
                .execute()
            )
        except HttpError as e:
            raise RuntimeError(f"Google Calendar insert error: {e}") from e
        return _normalize_event(ev, calendar_id)

    def update_event(
        self,
        event_id: str,
        *,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        reminders: dict[str, Any] | None = None,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """Patch an existing event. Only the fields passed get
        modified — None args leave the field as-is on Google's side.

        Note on `reminders`: patch semantics mean omitting the field
        leaves whatever the user (or another writer) set last. If you
        pass a value here we WILL overwrite the user's manual setting;
        callers writing planned workouts should know this and decide
        whether to push silence on every update or only on insert."""
        creds = self._load_creds()
        if creds is None:
            raise RuntimeError("Google Calendar not connected")
        body: dict[str, Any] = {}
        if summary is not None:
            body["summary"] = summary
        if description is not None:
            body["description"] = description
        if start is not None:
            body["start"] = _iso_to_event_time(start)
        if end is not None:
            body["end"] = _iso_to_event_time(end)
        if reminders is not None:
            body["reminders"] = reminders
        if not body:
            # Nothing to patch — return current state instead of a
            # zero-field API call.
            return self._get_event(event_id, calendar_id)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        try:
            ev = (
                service.events()
                .patch(calendarId=calendar_id, eventId=event_id, body=body)
                .execute()
            )
        except HttpError as e:
            raise RuntimeError(f"Google Calendar update error: {e}") from e
        return _normalize_event(ev, calendar_id)

    def delete_event(
        self, event_id: str, calendar_id: str = "primary"
    ) -> bool:
        """Hard-delete an event. Returns True on success, False if the
        event was already gone (404 is treated as idempotent — we
        don't want a 404 to crash a cleanup sweep)."""
        creds = self._load_creds()
        if creds is None:
            raise RuntimeError("Google Calendar not connected")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        try:
            service.events().delete(
                calendarId=calendar_id, eventId=event_id
            ).execute()
            return True
        except HttpError as e:
            if e.resp and e.resp.status == 404:
                return False
            raise RuntimeError(f"Google Calendar delete error: {e}") from e

    def _get_event(
        self, event_id: str, calendar_id: str = "primary"
    ) -> dict[str, Any]:
        """Internal — fetch a single event by id, used as the no-op
        return for update_event with no body."""
        creds = self._load_creds()
        if creds is None:
            raise RuntimeError("Google Calendar not connected")
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        try:
            ev = (
                service.events()
                .get(calendarId=calendar_id, eventId=event_id)
                .execute()
            )
        except HttpError as e:
            raise RuntimeError(f"Google Calendar get error: {e}") from e
        return _normalize_event(ev, calendar_id)


# Module helpers --------------------------------------------------------


def _iso_to_event_time(iso: str) -> dict[str, str]:
    """Convert an ISO 8601 string to the Google Calendar `start` /
    `end` shape. Date-only ('2026-05-28') → all-day event. Full
    datetime → timed event in the user's local TZ (Google figures
    out the offset from the trailing Z or +HH:MM)."""
    # YYYY-MM-DD is exactly 10 chars; longer strings carry time info.
    if len(iso) == 10:
        return {"date": iso}
    return {"dateTime": iso}


def _normalize_event(ev: dict[str, Any], calendar_id: str) -> dict[str, Any]:
    """Same shape as list_events emits per event — kept in lockstep so
    callers can pass write results through the same downstream
    handlers."""
    start_obj = ev.get("start", {}) or {}
    end_obj = ev.get("end", {}) or {}
    return {
        "source": "google",
        "id": ev.get("id"),
        "title": ev.get("summary") or "(no title)",
        "start": start_obj.get("dateTime") or start_obj.get("date"),
        "end": end_obj.get("dateTime") or end_obj.get("date"),
        "all_day": "date" in start_obj,
        "location": ev.get("location"),
        "description": ev.get("description"),
        "calendar_id": calendar_id,
    }
