"""Strava endpoints: status, OAuth round trip, open-in-Strava redirect,
and the rename that rides on saving a run."""

import pytest

import backend.api_server as api_server
from backend.strava_client import NotConnected, StravaError

SUMMARY = {"activityId": 111, "startTimeGMT": "2026-11-01 14:12:47", "distance": 42683.0}


@pytest.fixture
def connected(monkeypatch):
    s = api_server.strava
    s.configured.return_value = True
    s.connected.return_value = True
    s.can_write.return_value = True
    s.find_activity_id.return_value = 900
    monkeypatch.setattr(api_server, "_find_run_summary", lambda _id: dict(SUMMARY))
    return s


class TestStatus:
    def test_reports_three_flags(self, client, connected):
        connected.can_write.return_value = False
        assert client.get("/api/strava/status").json() == {
            "configured": True, "connected": True, "can_write": False,
        }


class TestOAuth:
    def test_start_redirects_to_strava(self, client):
        r = client.get("/oauth/strava/start", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"].startswith("https://www.strava.com/oauth/authorize")
        api_server.strava.authorize_url.assert_called_once_with(
            "http://localhost:8765/oauth/strava/callback")

    def test_start_without_client_credentials_is_400(self, client):
        api_server.strava.authorize_url.side_effect = NotConnected("no id")
        assert client.get("/oauth/strava/start", follow_redirects=False).status_code == 400

    def test_callback_exchanges_code_and_records_granted_scope(self, client):
        r = client.get("/oauth/strava/callback?code=abc&scope=read,activity:write",
                       follow_redirects=False)
        api_server.strava.exchange_code.assert_called_once_with(
            "abc", granted_scope="read,activity:write")
        assert r.headers["location"].endswith("/setup?strava_connected=1")

    def test_callback_with_denied_consent(self, client):
        r = client.get("/oauth/strava/callback?error=access_denied", follow_redirects=False)
        api_server.strava.exchange_code.assert_not_called()
        assert "strava_error=access_denied" in r.headers["location"]

    def test_callback_exchange_failure_is_reported_not_raised(self, client):
        api_server.strava.exchange_code.side_effect = StravaError("HTTP 400")
        r = client.get("/oauth/strava/callback?code=abc", follow_redirects=False)
        assert "strava_error=StravaError" in r.headers["location"]

    def test_disconnect(self, client):
        assert client.post("/api/strava/disconnect").json() == {"ok": True}
        api_server.strava.disconnect.assert_called_once()


class TestOpenInStrava:
    def test_redirects_to_the_matched_activity(self, client, connected):
        r = client.get("/api/runs/111/strava/open", follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "https://www.strava.com/activities/900"
        args = connected.find_activity_id.call_args[0]
        assert args[0] == 111 and args[1].isoformat() == "2026-11-01T14:12:47+00:00"
        assert args[2] == 42683.0

    def test_no_match_is_404(self, client, connected):
        connected.find_activity_id.return_value = None
        assert client.get("/api/runs/111/strava/open", follow_redirects=False).status_code == 404

    def test_unknown_run_is_404(self, client, connected, monkeypatch):
        monkeypatch.setattr(api_server, "_find_run_summary", lambda _id: None)
        assert client.get("/api/runs/999/strava/open", follow_redirects=False).status_code == 404

    def test_not_connected_is_409(self, client, connected):
        connected.find_activity_id.side_effect = NotConnected("no token")
        assert client.get("/api/runs/111/strava/open", follow_redirects=False).status_code == 409

    def test_strava_outage_is_502(self, client, connected):
        connected.find_activity_id.side_effect = StravaError("HTTP 500")
        assert client.get("/api/runs/111/strava/open", follow_redirects=False).status_code == 502


def _save(client, name="NYC W22D4"):
    api_server.processor.get_run_laps.return_value = [{"distance": 1609.34}]
    api_server.processor.calculate_category_stats.return_value = []
    return client.put("/api/runs/111/laps", json={
        "week_num": 22, "run_name": name, "categories": ["Marathon"], "notes": "",
    })


class TestRenameOnSave:
    def test_saving_a_named_run_renames_the_strava_twin(self, client, connected):
        body = _save(client).json()
        assert body["ok"] is True and body["strava"] == "renamed"
        connected.rename.assert_called_once_with(900, "NYC W22D4")

    def test_not_connected_saves_and_says_so(self, client):
        body = _save(client).json()
        assert body["ok"] is True and body["strava"] == "not_connected"
        api_server.strava.rename.assert_not_called()

    def test_read_only_authorization_does_not_attempt_a_rename(self, client, connected):
        connected.can_write.return_value = False
        assert _save(client).json()["strava"] == "read_only"
        connected.rename.assert_not_called()

    def test_no_strava_twin_yet(self, client, connected):
        connected.find_activity_id.return_value = None
        assert _save(client).json()["strava"] == "not_found"

    def test_strava_failure_never_fails_the_save(self, client, connected):
        connected.rename.side_effect = StravaError("HTTP 429")
        r = _save(client)
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["strava"] == "error"
        api_server.processor.save_run_metadata.assert_called_once()

    def test_blank_name_is_not_pushed(self, client, connected):
        assert _save(client, name="  ").json()["strava"] == "skipped"
        connected.rename.assert_not_called()


class TestManualRename:
    def test_pushes_the_saved_name(self, client, connected, tmp_path):
        (tmp_path / "run_111_meta.json").write_text('{"name": "NYC W22D4"}')
        api_server.processor.paths = {"manual": str(tmp_path)}
        body = client.post("/api/runs/111/strava/rename").json()
        assert body == {"ok": True, "strava": "renamed"}
        connected.rename.assert_called_once_with(900, "NYC W22D4")

    def test_run_without_a_saved_name_is_skipped(self, client, connected, tmp_path):
        api_server.processor.paths = {"manual": str(tmp_path)}
        assert client.post("/api/runs/111/strava/rename").json() == {"ok": False, "strava": "skipped"}
