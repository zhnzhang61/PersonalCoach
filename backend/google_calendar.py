"""Google Calendar integration — OAuth flow + event fetch.

The Personal Coach app authenticates against the user's personal Google
account and reads events from their primary calendar. This is single-user
(the owner runs it locally), so we store the refresh token in a JSON file
under data/oauth/ — sqlite would be overkill for one row.

Phase 1 is read-only (calendar.readonly). Phase 2 will add write scope
for AI-driven workout scheduling.
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

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Phase 1: read-only. Phase 2 will append "calendar.events" for write.
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


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

    def is_connected(self) -> bool:
        try:
            return self._load_creds() is not None
        except Exception:
            return False

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
