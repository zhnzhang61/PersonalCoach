"""Unit tests for backend/google_calendar.py.

Per docs/IMPROVEMENTS.md §1 Phase 3: `google_calendar.py` is a single-
user OAuth-token + read-only-events adapter. The bulk of its behavior
is plumbing — env-var read, code_verifier stashing across the
redirect, credential file round-trip, and one event-shape projection.

What we mock:
  • `googleapiclient.discovery.build` — the `service.events().list()`
    chain that fans out into the real HTTP API. We swap it for a
    MagicMock whose `.execute()` returns a canned response dict.
  • `google_auth_oauthlib.flow.Flow` — the OAuth flow class. We patch
    `Flow.from_client_config` so neither authorization_url nor
    finish_flow contacts Google.
  • `google.oauth2.credentials.Credentials` — for the token-load /
    refresh path. We patch `Credentials.from_authorized_user_file` so
    `_load_creds` reads our test fixture instead of a real JSON.

Pure sync — no pytest-asyncio dep.
"""

from __future__ import annotations

import datetime
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from backend import google_calendar as gc


# ---------------------------------------------------------------------------
# _maybe_allow_insecure_localhost — gated env-flip
# ---------------------------------------------------------------------------


class TestMaybeAllowInsecureLocalhost:
    """The OAUTHLIB_INSECURE_TRANSPORT escape hatch must only flip for
    real localhost redirects. A future TLS deploy that imports this
    module shouldn't quietly disable Google's HTTPS-only check."""

    def test_flips_env_for_localhost(self, monkeypatch):
        monkeypatch.setenv(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "http://localhost:8765/oauth/google/callback",
        )
        monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
        gc._maybe_allow_insecure_localhost()
        assert os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") == "1"

    def test_flips_env_for_127_0_0_1(self, monkeypatch):
        monkeypatch.setenv(
            "GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:8765/cb"
        )
        monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
        gc._maybe_allow_insecure_localhost()
        assert os.environ.get("OAUTHLIB_INSECURE_TRANSPORT") == "1"

    def test_does_not_flip_for_https(self, monkeypatch):
        monkeypatch.setenv(
            "GOOGLE_OAUTH_REDIRECT_URI", "https://app.example.com/cb"
        )
        monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
        gc._maybe_allow_insecure_localhost()
        assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ

    def test_does_not_flip_when_redirect_unset(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
        monkeypatch.delenv("OAUTHLIB_INSECURE_TRANSPORT", raising=False)
        gc._maybe_allow_insecure_localhost()
        assert "OAUTHLIB_INSECURE_TRANSPORT" not in os.environ

    def test_does_not_overwrite_existing_setting(self, monkeypatch):
        # setdefault, not assign — operator's choice wins.
        monkeypatch.setenv(
            "GOOGLE_OAUTH_REDIRECT_URI",
            "http://localhost:8765/cb",
        )
        monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "explicit_value")
        gc._maybe_allow_insecure_localhost()
        assert os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "explicit_value"


# ---------------------------------------------------------------------------
# _client_config — env-var read
# ---------------------------------------------------------------------------


class TestClientConfig:
    def test_raises_when_client_id_missing(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
        with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_CLIENT_ID"):
            gc._client_config()

    def test_raises_when_client_secret_missing(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        with pytest.raises(RuntimeError, match="GOOGLE_OAUTH_CLIENT_SECRET"):
            gc._client_config()

    def test_returns_web_shaped_dict(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "test-secret")
        monkeypatch.setenv(
            "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8765/cb"
        )
        cfg = gc._client_config()
        assert cfg["web"]["client_id"] == "test-id"
        assert cfg["web"]["client_secret"] == "test-secret"
        assert cfg["web"]["redirect_uris"] == ["http://localhost:8765/cb"]
        # Auth + token endpoints should always be the canonical Google ones,
        # not configurable — if they were, a swapped value would be a
        # security hole.
        assert cfg["web"]["auth_uri"] == "https://accounts.google.com/o/oauth2/auth"
        assert cfg["web"]["token_uri"] == "https://oauth2.googleapis.com/token"

    def test_default_redirect_when_env_missing(self, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "s")
        monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
        cfg = gc._client_config()
        assert cfg["web"]["redirect_uris"] == [
            "http://localhost:8765/oauth/google/callback"
        ]


# ---------------------------------------------------------------------------
# GoogleCalendar.__init__ — token-dir bootstrap
# ---------------------------------------------------------------------------


class TestInit:
    def test_creates_oauth_dir(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        assert (tmp_path / "oauth").is_dir()
        assert cal.token_path == tmp_path / "oauth" / "google_token.json"

    def test_pending_verifiers_starts_empty(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        assert cal._pending_verifiers == {}

    def test_init_idempotent_when_dir_exists(self, tmp_path):
        # Re-running shouldn't blow up — exist_ok=True.
        (tmp_path / "oauth").mkdir()
        gc.GoogleCalendar(data_dir=str(tmp_path))
        gc.GoogleCalendar(data_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# OAuth flow: authorization_url + finish_flow
# ---------------------------------------------------------------------------


@pytest.fixture
def env(monkeypatch):
    """Minimal env vars for _client_config to succeed."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8765/cb"
    )


class TestAuthorizationUrl:
    def test_returns_url_and_state_tuple(self, tmp_path, env):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        fake_flow = MagicMock(name="Flow")
        fake_flow.authorization_url.return_value = (
            "https://accounts.google.com/o/oauth2/auth?x=1",
            "state-abc",
        )
        fake_flow.code_verifier = "verifier-xyz"
        with patch.object(gc.Flow, "from_client_config", return_value=fake_flow):
            url, state = cal.authorization_url()
        assert url == "https://accounts.google.com/o/oauth2/auth?x=1"
        assert state == "state-abc"

    def test_stashes_verifier_under_state(self, tmp_path, env):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        fake_flow = MagicMock()
        fake_flow.authorization_url.return_value = ("u", "state-1")
        fake_flow.code_verifier = "verifier-1"
        with patch.object(gc.Flow, "from_client_config", return_value=fake_flow):
            cal.authorization_url()
        # Critical: the verifier must be retrievable by `state` for the
        # callback-side fetch_token to succeed with PKCE.
        assert cal._pending_verifiers["state-1"] == "verifier-1"

    def test_passes_offline_and_consent_to_flow(self, tmp_path, env):
        """offline + consent are load-bearing: without them Google won't
        return a refresh_token, and the user would re-prompt every hour.
        Pin them so a refactor can't quietly drop them."""
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        fake_flow = MagicMock()
        fake_flow.authorization_url.return_value = ("u", "s")
        fake_flow.code_verifier = "v"
        with patch.object(gc.Flow, "from_client_config", return_value=fake_flow):
            cal.authorization_url()
        kwargs = fake_flow.authorization_url.call_args.kwargs
        assert kwargs["access_type"] == "offline"
        assert kwargs["prompt"] == "consent"


class TestFinishFlow:
    def test_pops_verifier_and_calls_fetch_token(self, tmp_path, env):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal._pending_verifiers["state-1"] = "verifier-1"

        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token":"abc"}'
        fake_flow = MagicMock()
        fake_flow.credentials = fake_creds

        with patch.object(gc.Flow, "from_client_config", return_value=fake_flow):
            cal.finish_flow(
                authorization_response="http://localhost:8765/cb?code=xyz&state=state-1",
                state="state-1",
            )

        # Verifier consumed (pop semantics — abandoned flows could leak
        # otherwise; finished ones must be cleared).
        assert "state-1" not in cal._pending_verifiers
        assert fake_flow.code_verifier == "verifier-1"
        # Token exchange called with the full callback URL.
        fake_flow.fetch_token.assert_called_once_with(
            authorization_response="http://localhost:8765/cb?code=xyz&state=state-1"
        )
        # Creds written to disk.
        assert cal.token_path.read_text() == '{"token":"abc"}'

    def test_finishes_when_verifier_missing(self, tmp_path, env):
        """If for some reason the verifier wasn't stashed (e.g. process
        restart between authorization_url and the callback), we still
        try the exchange — Google may have an older-style flow that
        works without PKCE. Failure mode is just a normal fetch_token
        error, not a KeyError."""
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = "{}"
        fake_flow = MagicMock()
        fake_flow.credentials = fake_creds
        with patch.object(gc.Flow, "from_client_config", return_value=fake_flow):
            # Should not raise — pop with default None.
            cal.finish_flow(
                authorization_response="http://localhost:8765/cb?code=x&state=missing",
                state="missing",
            )
        fake_flow.fetch_token.assert_called_once()


# ---------------------------------------------------------------------------
# Token storage: _load_creds, is_connected, disconnect
# ---------------------------------------------------------------------------


class TestLoadCreds:
    def test_returns_none_when_token_missing(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        assert cal._load_creds() is None

    def test_returns_creds_when_file_present_and_not_expired(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text('{"token":"abc"}')
        fake_creds = MagicMock()
        fake_creds.expired = False
        fake_creds.refresh_token = "refresh-tok"
        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            return_value=fake_creds,
        ):
            result = cal._load_creds()
        assert result is fake_creds
        fake_creds.refresh.assert_not_called()

    def test_refreshes_when_expired_with_refresh_token(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text('{"token":"abc"}')
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "refresh-tok"
        fake_creds.to_json.return_value = '{"refreshed":true}'
        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            return_value=fake_creds,
        ):
            result = cal._load_creds()
        assert result is fake_creds
        fake_creds.refresh.assert_called_once()
        # Rotated token written back so it sticks across restarts.
        assert cal.token_path.read_text() == '{"refreshed":true}'

    def test_does_not_refresh_when_expired_without_refresh_token(self, tmp_path):
        """No refresh_token = nothing we can do; don't try refresh()
        (it would 400). Caller upstream gets the expired creds and the
        first API call fails — that's the trigger to re-auth."""
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text("{}")
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = None
        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            return_value=fake_creds,
        ):
            cal._load_creds()
        fake_creds.refresh.assert_not_called()


class TestIsConnected:
    def test_false_when_no_token(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        assert cal.is_connected() is False

    def test_true_when_token_present(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text("{}")
        fake_creds = MagicMock()
        fake_creds.expired = False
        fake_creds.refresh_token = "r"
        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            return_value=fake_creds,
        ):
            assert cal.is_connected() is True

    def test_false_when_load_raises(self, tmp_path):
        """A corrupt or schema-incompatible token file shouldn't crash
        the caller (the FastAPI handler asks is_connected on every
        page load) — return False and let the user re-auth."""
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text("not-json")
        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            side_effect=ValueError("corrupt"),
        ):
            assert cal.is_connected() is False


class TestConnectionState:
    """connection_state() must distinguish 'expired' (a link exists but
    the refresh token died — Testing-mode 7-day cap / revoked) from
    'disconnected' (never linked). is_connected() collapsed both to
    False, so the UI couldn't tell 'reconnect' from 'first-time connect'."""

    def test_disconnected_when_no_token(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        assert cal.connection_state() == "disconnected"

    def test_connected_when_healthy(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text("{}")
        fake_creds = MagicMock()
        fake_creds.expired = False
        fake_creds.refresh_token = "r"
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ):
            assert cal.connection_state() == "connected"

    def test_expired_when_refresh_revoked(self, tmp_path):
        """The exact production failure: token on disk, expired, and the
        refresh call raises RefreshError (invalid_grant). Must report
        'expired', not 'disconnected'."""
        from google.auth.exceptions import RefreshError

        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text('{"token":"abc"}')
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "revoked-tok"
        fake_creds.refresh.side_effect = RefreshError(
            "invalid_grant: Token has been expired or revoked."
        )
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ):
            assert cal.connection_state() == "expired"

    def test_error_when_refresh_unreachable(self, tmp_path):
        """Token may be valid, but the refresh() network call couldn't
        reach Google (TransportError != RefreshError). Must report
        'error' — a transient outage, NOT 'expired'. Telling the user to
        reconnect here would be wrong: the token still works once the
        network recovers."""
        from google.auth.exceptions import TransportError

        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text('{"token":"abc"}')
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "valid-tok"
        fake_creds.refresh.side_effect = TransportError("Connection refused")
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ):
            assert cal.connection_state() == "error"
            # is_connected stays False (== 'connected' only), unchanged.
            assert cal.is_connected() is False

    def test_expired_when_token_file_corrupt(self, tmp_path):
        """A file exists but won't parse — there IS a link to repair, so
        'expired' (reconnect), not 'disconnected' (never connected)."""
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text("not-json")
        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            side_effect=ValueError("corrupt"),
        ):
            assert cal.connection_state() == "expired"

    def test_is_connected_delegates_to_state(self, tmp_path):
        """is_connected() is now a thin bool over connection_state(): True
        only for 'connected', False for 'expired'/'disconnected'."""
        from google.auth.exceptions import RefreshError

        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text('{"token":"abc"}')
        fake_creds = MagicMock()
        fake_creds.expired = True
        fake_creds.refresh_token = "revoked-tok"
        fake_creds.refresh.side_effect = RefreshError("invalid_grant")
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ):
            assert cal.connection_state() == "expired"
            assert cal.is_connected() is False


class TestDisconnect:
    def test_removes_token_file(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        cal.token_path.write_text("{}")
        cal.disconnect()
        assert not cal.token_path.exists()

    def test_disconnect_when_already_disconnected_is_noop(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        # No token to start with; must not raise.
        cal.disconnect()


# ---------------------------------------------------------------------------
# list_events — the event-shape projection
# ---------------------------------------------------------------------------


def _connected_calendar(tmp_path) -> tuple[gc.GoogleCalendar, MagicMock]:
    """Build a GoogleCalendar whose `_load_creds` returns a stub
    Credentials, sparing each test the boilerplate."""
    cal = gc.GoogleCalendar(data_dir=str(tmp_path))
    cal.token_path.write_text("{}")
    fake_creds = MagicMock()
    fake_creds.expired = False
    fake_creds.refresh_token = "r"
    return cal, fake_creds


class TestListEventsConnectivity:
    def test_raises_when_not_connected(self, tmp_path):
        cal = gc.GoogleCalendar(data_dir=str(tmp_path))
        start = datetime.datetime(2026, 5, 27, 0, 0)
        end = datetime.datetime(2026, 5, 28, 0, 0)
        with pytest.raises(RuntimeError, match="not connected"):
            cal.list_events(start, end)

    def test_http_error_wrapped(self, tmp_path):
        cal, fake_creds = _connected_calendar(tmp_path)
        # HttpError needs `resp` + `content` positional args; the
        # easiest path is to subclass-mock with the same shape.
        from googleapiclient.errors import HttpError

        class FakeResp:
            status = 500
            reason = "Server Error"

        err = HttpError(FakeResp(), b"boom")
        fake_service = MagicMock()
        fake_service.events().list().execute.side_effect = err

        with patch.object(
            gc.Credentials,
            "from_authorized_user_file",
            return_value=fake_creds,
        ), patch.object(gc, "build", return_value=fake_service):
            with pytest.raises(RuntimeError, match="Google Calendar API error"):
                cal.list_events(
                    datetime.datetime(2026, 5, 27),
                    datetime.datetime(2026, 5, 28),
                )


class TestListEventsMapping:
    """The frontend calendar and the AI prompts both consume this
    shape. Pin the field names so a refactor can't quietly rename them."""

    @staticmethod
    def _stub_service(items: list[dict]) -> MagicMock:
        fake_service = MagicMock()
        fake_service.events().list().execute.return_value = {"items": items}
        return fake_service

    def test_maps_timed_event(self, tmp_path):
        cal, fake_creds = _connected_calendar(tmp_path)
        service = self._stub_service([{
            "id": "evt_1",
            "summary": "Easy run",
            "start": {"dateTime": "2026-05-27T07:00:00-04:00"},
            "end": {"dateTime": "2026-05-27T07:45:00-04:00"},
            "location": "Hudson River Park",
            "description": "Z2",
        }])
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=service):
            out = cal.list_events(
                datetime.datetime(2026, 5, 27),
                datetime.datetime(2026, 5, 28),
            )
        assert out == [{
            "source": "google",
            "id": "evt_1",
            "title": "Easy run",
            "start": "2026-05-27T07:00:00-04:00",
            "end": "2026-05-27T07:45:00-04:00",
            "all_day": False,
            "location": "Hudson River Park",
            "description": "Z2",
            "calendar_id": "primary",
        }]

    def test_maps_all_day_event(self, tmp_path):
        """All-day events come through with `date` instead of
        `dateTime`. `all_day` must flip True and the value flows through
        the same `start`/`end` keys (the frontend differentiates on
        all_day, not on field name)."""
        cal, fake_creds = _connected_calendar(tmp_path)
        service = self._stub_service([{
            "id": "evt_holiday",
            "summary": "Memorial Day",
            "start": {"date": "2026-05-25"},
            "end": {"date": "2026-05-26"},
        }])
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=service):
            out = cal.list_events(
                datetime.datetime(2026, 5, 25),
                datetime.datetime(2026, 5, 27),
            )
        assert len(out) == 1
        ev = out[0]
        assert ev["all_day"] is True
        assert ev["start"] == "2026-05-25"
        assert ev["end"] == "2026-05-26"
        assert ev["location"] is None
        assert ev["description"] is None

    def test_missing_summary_falls_back_to_placeholder(self, tmp_path):
        """Google allows events without a summary (e.g. private/declined
        ones the user is invited to). The dict-shape downstream
        consumers expect title to be a string, so we substitute a
        sentinel rather than passing None and forcing every consumer to
        coalesce."""
        cal, fake_creds = _connected_calendar(tmp_path)
        service = self._stub_service([{
            "id": "evt_2",
            "start": {"dateTime": "2026-05-27T08:00:00Z"},
            "end": {"dateTime": "2026-05-27T09:00:00Z"},
        }])
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=service):
            out = cal.list_events(
                datetime.datetime(2026, 5, 27),
                datetime.datetime(2026, 5, 28),
            )
        assert out[0]["title"] == "(no title)"

    def test_empty_response(self, tmp_path):
        cal, fake_creds = _connected_calendar(tmp_path)
        service = self._stub_service([])
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=service):
            out = cal.list_events(
                datetime.datetime(2026, 5, 27),
                datetime.datetime(2026, 5, 28),
            )
        assert out == []

    def test_response_missing_items_key(self, tmp_path):
        """Google sometimes returns no `items` key at all (rather than
        an empty list) — we should treat it as empty, not KeyError."""
        cal, fake_creds = _connected_calendar(tmp_path)
        fake_service = MagicMock()
        fake_service.events().list().execute.return_value = {}
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=fake_service):
            out = cal.list_events(
                datetime.datetime(2026, 5, 27),
                datetime.datetime(2026, 5, 28),
            )
        assert out == []

    def test_custom_calendar_id_passed_through(self, tmp_path):
        cal, fake_creds = _connected_calendar(tmp_path)
        service = self._stub_service([{
            "id": "x",
            "summary": "thing",
            "start": {"dateTime": "2026-05-27T08:00:00Z"},
            "end": {"dateTime": "2026-05-27T09:00:00Z"},
        }])
        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=service):
            out = cal.list_events(
                datetime.datetime(2026, 5, 27),
                datetime.datetime(2026, 5, 28),
                calendar_id="custom@example.com",
            )
        assert out[0]["calendar_id"] == "custom@example.com"

    def test_list_call_args(self, tmp_path):
        """Pin the kwargs to `events().list()` — singleEvents=True is
        the difference between concrete instances and RRULE strings,
        and orderBy=startTime is what makes the frontend's render
        deterministic."""
        cal, fake_creds = _connected_calendar(tmp_path)
        fake_service = MagicMock()
        fake_service.events().list().execute.return_value = {"items": []}
        # Clear out the warm-up calls so we can assert on the real one.
        fake_service.reset_mock()
        fake_service.events().list().execute.return_value = {"items": []}

        with patch.object(
            gc.Credentials, "from_authorized_user_file", return_value=fake_creds,
        ), patch.object(gc, "build", return_value=fake_service):
            start = datetime.datetime(2026, 5, 27, 0, 0)
            end = datetime.datetime(2026, 5, 28, 0, 0)
            cal.list_events(start, end)

        # Find the .list(...) call with our kwargs (MagicMock chain
        # records every intermediate call; we look at the one with
        # calendarId set).
        list_calls = [
            c for c in fake_service.events.return_value.list.call_args_list
            if c.kwargs.get("calendarId") == "primary"
        ]
        assert list_calls, "expected at least one events().list(calendarId=primary) call"
        kwargs = list_calls[-1].kwargs
        assert kwargs["singleEvents"] is True
        assert kwargs["orderBy"] == "startTime"
        assert kwargs["timeMin"] == start.isoformat()
        assert kwargs["timeMax"] == end.isoformat()
