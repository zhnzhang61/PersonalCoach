"""LangSmith tracing wiring (PR E).

Pure env-var-state tests + endpoint smoke. We don't talk to the
actual LangSmith API in tests — that'd require credentials + a
live network. The wiring contract is: when the env vars are set
in the expected combinations, the helpers report the right state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# langsmith_tracing_enabled / langsmith_status
# --------------------------------------------------------------------------


class TestLangSmithTracingEnabled:
    """Master flag + API key. BOTH must be present + truthy.
    Anything else → off."""

    def test_both_set_true(self, monkeypatch):
        from backend.langsmith_setup import langsmith_tracing_enabled

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is True

    def test_flag_off_returns_false_even_with_key(self, monkeypatch):
        """Key set but flag off → still off. Common state when
        a user disables tracing for a debugging session but
        doesn't bother clearing the key."""
        from backend.langsmith_setup import langsmith_tracing_enabled

        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is False

    def test_key_missing_returns_false_even_with_flag(self, monkeypatch):
        """Flag on but no key → also off. langchain would silently
        401 on every span otherwise — better to short-circuit AND
        surface "misconfigured" via startup_log_line()."""
        from backend.langsmith_setup import langsmith_tracing_enabled

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        assert langsmith_tracing_enabled() is False

    @pytest.mark.parametrize(
        "value", ["true", "True", "TRUE", "1", "yes", "on"],
    )
    def test_truthy_flag_values(self, monkeypatch, value):
        """Match langchain's own permissive parsing — case
        insensitive + several aliases. Otherwise users export
        `LANGSMITH_TRACING=1` and our status says "off" while
        langchain itself says "on", which is the worst kind of
        bug to debug."""
        from backend.langsmith_setup import langsmith_tracing_enabled

        monkeypatch.setenv("LANGSMITH_TRACING", value)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is True

    @pytest.mark.parametrize(
        "value", ["false", "0", "no", "off", "", "FALSE"],
    )
    def test_falsy_flag_values(self, monkeypatch, value):
        from backend.langsmith_setup import langsmith_tracing_enabled

        monkeypatch.setenv("LANGSMITH_TRACING", value)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is False

    def test_whitespace_only_key_treated_as_missing(self, monkeypatch):
        """Forgot to actually paste the key but had quotes →
        `LANGSMITH_API_KEY="   "`. Don't pretend it's set."""
        from backend.langsmith_setup import langsmith_tracing_enabled

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "   ")
        assert langsmith_tracing_enabled() is False


class TestLangSmithStatus:
    """Structured status payload. Same env-var rules as
    `langsmith_tracing_enabled` but exposes everything for the
    `/api/debug/observability` endpoint."""

    def test_full_state_when_everything_set(self, monkeypatch):
        from backend.langsmith_setup import langsmith_status

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.setenv("LANGSMITH_PROJECT", "personalcoach")
        monkeypatch.setenv(
            "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
        )

        s = langsmith_status()
        assert s["tracing_enabled"] is True
        assert s["tracing_flag"] == "true"
        assert s["api_key_set"] is True
        assert s["project"] == "personalcoach"
        assert s["endpoint"] == "https://api.smith.langchain.com"

    def test_never_echoes_api_key_value(self, monkeypatch):
        """Status is exposed via `/api/debug/observability`. The
        key is a secret and must NEVER appear in the response —
        regression-pin so a future refactor that adds
        `api_key=value` to the dict surfaces immediately."""
        from backend.langsmith_setup import langsmith_status

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_super_secret")

        s = langsmith_status()
        # No key in any value (status reports only `api_key_set: bool`).
        for v in s.values():
            assert "lsv2_super_secret" not in str(v)

    def test_project_defaults_to_default(self, monkeypatch):
        """LangSmith's own default when LANGSMITH_PROJECT is unset.
        Match it so the surfaced "project" matches where traces
        actually land."""
        from backend.langsmith_setup import langsmith_status

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.delenv("LANGSMITH_PROJECT", raising=False)
        assert langsmith_status()["project"] == "default"

    def test_endpoint_defaults_to_hosted_saas(self, monkeypatch):
        from backend.langsmith_setup import langsmith_status

        monkeypatch.delenv("LANGSMITH_ENDPOINT", raising=False)
        assert (
            langsmith_status()["endpoint"]
            == "https://api.smith.langchain.com"
        )

    def test_self_hosted_endpoint_surfaced(self, monkeypatch):
        """For self-hosted LangSmith deployments, expose the
        custom endpoint so the operator can confirm traces are
        landing on their server vs the SaaS by accident."""
        from backend.langsmith_setup import langsmith_status

        monkeypatch.setenv(
            "LANGSMITH_ENDPOINT", "https://langsmith.internal.example.com"
        )
        assert (
            langsmith_status()["endpoint"]
            == "https://langsmith.internal.example.com"
        )


class TestStartupLogLine:
    """Three states map to three distinct log-line shapes so an
    operator can grep / eyeball the wiring without running the
    debug endpoint."""

    def test_on_state_includes_project_and_endpoint(self, monkeypatch):
        from backend.langsmith_setup import startup_log_line

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.setenv("LANGSMITH_PROJECT", "personalcoach")
        line = startup_log_line()
        assert "ON" in line
        assert "personalcoach" in line
        assert "api.smith.langchain.com" in line

    def test_off_state_when_flag_missing(self, monkeypatch):
        from backend.langsmith_setup import startup_log_line

        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        line = startup_log_line()
        assert "OFF" in line
        # Reference the var name so the operator knows what to set.
        assert "LANGSMITH_TRACING" in line

    def test_misconfigured_state_when_flag_set_but_no_key(self, monkeypatch):
        """The dangerous silent-fail mode: flag on, no key, every
        span 401s on send. The log line MUST call this out
        explicitly so the operator sees something's wrong even
        though tracing-related code paths are running."""
        from backend.langsmith_setup import startup_log_line

        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        line = startup_log_line()
        assert "MISCONFIGURED" in line
        assert "LANGSMITH_API_KEY" in line


# --------------------------------------------------------------------------
# /api/debug/observability
# --------------------------------------------------------------------------


class TestObservabilityEndpoint:
    def test_returns_status_shape(self, client, monkeypatch):
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.setenv("LANGSMITH_PROJECT", "personalcoach")

        resp = client.get("/api/debug/observability")
        assert resp.status_code == 200
        body = resp.json()
        # Same keys langsmith_status() returns — pin the contract
        # so a future endpoint refactor that renames fields
        # surfaces visibly (clients may read these).
        assert set(body.keys()) == {
            "tracing_enabled", "tracing_flag", "api_key_set",
            "project", "endpoint",
        }
        assert body["tracing_enabled"] is True
        assert body["api_key_set"] is True

    def test_endpoint_never_returns_key_value(self, client, monkeypatch):
        """Belt-and-suspenders regression test at the HTTP layer
        (separate from the langsmith_status unit test above) —
        catches a refactor that bypasses the helper and writes
        the response dict directly."""
        secret = "lsv2_endpoint_secret_token_xyz"
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", secret)

        resp = client.get("/api/debug/observability")
        assert secret not in resp.text

    def test_off_state_reported(self, client, monkeypatch):
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
        resp = client.get("/api/debug/observability")
        body = resp.json()
        assert body["tracing_enabled"] is False
        assert body["api_key_set"] is False
