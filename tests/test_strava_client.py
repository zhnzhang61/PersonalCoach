"""StravaClient: OAuth token handling, Garmin→Strava matching, rename.

`http` is a fake that records calls; no network. Fixture payloads are
hand-written — nothing here comes from a real Strava response.
"""

import datetime
import json

import pytest

from backend.strava_client import (
    MATCH_TOLERANCE_S, NotConnected, StravaClient, StravaError, garmin_start_utc,
)

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 11, 1, 14, 12, 47, tzinfo=UTC)
ENV = {"STRAVA_CLIENT_ID": "123", "STRAVA_CLIENT_SECRET": "shh"}


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.token_payload = {"access_token": "A2", "refresh_token": "R2", "expires_at": 9_000}
        self.activities = []
        self.status = {}

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        return Resp(self.status.get("token", 200), dict(self.token_payload))

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(("GET", url, params, headers))
        return Resp(self.status.get("list", 200), list(self.activities))

    def put(self, url, headers=None, data=None, timeout=None):
        self.calls.append(("PUT", url, data, headers))
        return Resp(self.status.get("rename", 200), {})


def _client(tmp_path, http=None, now=1_000.0, env=ENV, token=None):
    path = tmp_path / "oauth" / "strava_token.json"
    if token is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token))
    return StravaClient(token_path=path, http=http or FakeHttp(), now=lambda: now, env=env.get), path


def _act(sid, start, distance=5000.0):
    return {"id": sid, "start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "distance": distance}


FRESH = {"access_token": "A1", "refresh_token": "R1", "expires_at": 9_999, "scope": "read,activity:read_all,activity:write"}


class TestConnectionState:
    def test_not_configured_without_env(self, tmp_path):
        c, _ = _client(tmp_path, env={})
        assert c.configured() is False and c.connected() is False
        with pytest.raises(NotConnected):
            c.authorize_url("http://localhost:8765/cb")

    def test_configured_but_not_authorized(self, tmp_path):
        c, _ = _client(tmp_path)
        assert c.configured() is True and c.connected() is False and c.can_write() is False

    def test_connected_with_write_scope(self, tmp_path):
        c, _ = _client(tmp_path, token=FRESH)
        assert c.connected() is True and c.can_write() is True

    def test_user_unticked_write_on_the_consent_page(self, tmp_path):
        c, _ = _client(tmp_path, token={**FRESH, "scope": "read,activity:read_all"})
        assert c.connected() is True and c.can_write() is False

    def test_disconnect_removes_the_token(self, tmp_path):
        c, path = _client(tmp_path, token=FRESH)
        c.disconnect()
        assert not path.exists() and c.connected() is False


class TestOAuth:
    def test_authorize_url_asks_for_read_all_and_write(self, tmp_path):
        c, _ = _client(tmp_path)
        url = c.authorize_url("http://localhost:8765/oauth/strava/callback")
        assert url.startswith("https://www.strava.com/oauth/authorize?")
        assert "client_id=123" in url and "response_type=code" in url
        assert "activity%3Aread_all%2Cactivity%3Awrite" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Foauth%2Fstrava%2Fcallback" in url

    def test_code_exchange_persists_tokens_and_granted_scope(self, tmp_path):
        http = FakeHttp()
        c, path = _client(tmp_path, http=http)
        c.exchange_code("the-code", granted_scope="read,activity:write,activity:read_all")
        method, url, data = http.calls[0]
        assert (method, url) == ("POST", "https://www.strava.com/oauth/token")
        assert data == {"client_id": "123", "client_secret": "shh", "code": "the-code",
                        "grant_type": "authorization_code"}
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "R2" and "activity:write" in saved["scope"]

    def test_valid_token_is_used_without_a_refresh(self, tmp_path):
        http = FakeHttp()
        c, _ = _client(tmp_path, http=http, token=FRESH, now=1_000.0)
        c.rename(55, "NYC W22D4")
        assert [call[0] for call in http.calls] == ["PUT"]
        assert http.calls[0][3] == {"Authorization": "Bearer A1"}

    def test_expiring_token_is_refreshed_and_the_rotated_refresh_token_saved(self, tmp_path):
        # Strava may hand back a NEW refresh token; the old one is dead.
        http = FakeHttp()
        c, path = _client(tmp_path, http=http, token={**FRESH, "expires_at": 1_100}, now=1_000.0)
        c.rename(55, "x")
        assert http.calls[0][2]["grant_type"] == "refresh_token"
        assert http.calls[0][2]["refresh_token"] == "R1"
        saved = json.loads(path.read_text())
        assert saved["refresh_token"] == "R2" and saved["access_token"] == "A2"
        assert "activity:write" in saved["scope"]          # scope survives the refresh
        assert http.calls[1][3] == {"Authorization": "Bearer A2"}

    def test_token_endpoint_failure_raises(self, tmp_path):
        http = FakeHttp()
        http.status["token"] = 400
        c, _ = _client(tmp_path, http=http)
        with pytest.raises(StravaError):
            c.exchange_code("bad")

    def test_calls_without_authorization_raise_not_connected(self, tmp_path):
        c, _ = _client(tmp_path)
        with pytest.raises(NotConnected):
            c.rename(1, "x")


class TestMatching:
    def test_exact_start_time_match(self, tmp_path):
        http = FakeHttp()
        http.activities = [_act(900, START)]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(24933190682, START) == 900
        _, url, params, _ = http.calls[0]
        assert url.endswith("/athlete/activities")
        assert params["after"] < START.timestamp() < params["before"]

    def test_nearest_start_wins_on_a_three_recording_speed_day(self, tmp_path):
        # warm-up / main set / cool-down, as Garmin→Strava syncs them
        http = FakeHttp()
        http.activities = [
            _act(1, START - datetime.timedelta(minutes=4, seconds=50)),
            _act(2, START + datetime.timedelta(seconds=3)),
            _act(3, START + datetime.timedelta(minutes=4)),
        ]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(111, START) == 2

    def test_outside_tolerance_is_no_match(self, tmp_path):
        http = FakeHttp()
        http.activities = [_act(1, START + datetime.timedelta(seconds=MATCH_TOLERANCE_S + 30))]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(111, START) is None

    def test_distance_breaks_a_tie_between_same_second_starts(self, tmp_path):
        http = FakeHttp()
        http.activities = [_act(1, START, distance=400.0), _act(2, START, distance=42_100.0)]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(111, START, distance_m=42_683.0) == 2

    def test_naive_datetime_is_treated_as_utc(self, tmp_path):
        http = FakeHttp()
        http.activities = [_act(7, START)]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(111, START.replace(tzinfo=None)) == 7

    def test_malformed_rows_are_skipped(self, tmp_path):
        http = FakeHttp()
        http.activities = [{"id": "x"}, {"start_date": "nope", "id": 4}, _act(5, START)]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(111, START) == 5

    def test_second_lookup_is_served_from_memory(self, tmp_path):
        http = FakeHttp()
        http.activities = [_act(900, START)]
        c, _ = _client(tmp_path, http=http, token=FRESH)
        c.find_activity_id(111, START)
        c.find_activity_id(111, START)
        assert sum(1 for call in http.calls if call[0] == "GET") == 1

    def test_a_miss_is_not_cached(self, tmp_path):
        # Garmin→Strava sync can lag; the next tap must look again.
        http = FakeHttp()
        c, _ = _client(tmp_path, http=http, token=FRESH)
        assert c.find_activity_id(111, START) is None
        http.activities = [_act(900, START)]
        assert c.find_activity_id(111, START) == 900

    def test_list_failure_raises(self, tmp_path):
        http = FakeHttp()
        http.status["list"] = 429
        c, _ = _client(tmp_path, http=http, token=FRESH)
        with pytest.raises(StravaError):
            c.find_activity_id(111, START)

    def test_nothing_from_strava_is_written_to_disk(self, tmp_path):
        http = FakeHttp()
        http.activities = [{**_act(900, START), "name": "Morning Run"}]
        c, path = _client(tmp_path, http=http, token=FRESH)
        c.find_activity_id(111, START)
        files = [p for p in tmp_path.rglob("*") if p.is_file()]
        assert files == [path]
        assert "Morning Run" not in path.read_text() and "900" not in path.read_text()


class TestRename:
    def test_put_sets_only_the_name(self, tmp_path):
        http = FakeHttp()
        c, _ = _client(tmp_path, http=http, token=FRESH)
        c.rename(900, "NYC W22D4")
        method, url, data, _ = http.calls[0]
        assert method == "PUT" and url.endswith("/activities/900") and data == {"name": "NYC W22D4"}

    def test_failure_raises(self, tmp_path):
        http = FakeHttp()
        http.status["rename"] = 401
        c, _ = _client(tmp_path, http=http, token=FRESH)
        with pytest.raises(StravaError):
            c.rename(900, "x")


class TestGarminStart:
    @pytest.mark.parametrize("raw", ["2026-11-01 14:12:47", "2026-11-01T14:12:47.0"])
    def test_both_garmin_formats(self, raw):
        assert garmin_start_utc({"startTimeGMT": raw}) == START

    def test_missing_or_garbage(self):
        assert garmin_start_utc({}) is None
        assert garmin_start_utc({"startTimeGMT": "soon"}) is None


def test_activity_url(tmp_path):
    c, _ = _client(tmp_path)
    assert c.activity_url(900) == "https://www.strava.com/activities/900"
