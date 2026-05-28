"""LangSmith tracing wiring (PR E).

Pure env-var-state tests + endpoint smoke. We don't talk to the
actual LangSmith API in tests — that'd require credentials + a
live network. The wiring contract is: this module's status must
NEVER disagree with what `langsmith.utils.tracing_is_enabled`
would say given the same env. Two specific contracts:

  1. Strict-truthy flag: lowercase literal `"true"` only
     (langsmith does `var_result == "true"`).
  2. Two-namespace lookup: `LANGSMITH_*` AND `LANGCHAIN_*`,
     plus `_V2` / non-`_V2` variants per namespace.

Tests below pin both contracts directly + via the
`/api/admin/observability` endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# All four env-var name variants the module reads. Used to
# parametrize the namespace-fallback tests so each path through
# the lookup chain gets its own test row.
ALL_FLAG_VARS = [
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING",
]
ALL_KEY_VARS = ["LANGSMITH_API_KEY", "LANGCHAIN_API_KEY"]


def _clear_all_langsmith_env(monkeypatch):
    """Strip every env var langsmith / this module reads so a test
    starts from a clean slate regardless of what the shell exported."""
    for var in (
        *ALL_FLAG_VARS, *ALL_KEY_VARS,
        "LANGSMITH_PROJECT", "LANGCHAIN_PROJECT",
        "LANGSMITH_ENDPOINT", "LANGCHAIN_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# langsmith_tracing_enabled — strict-truthy contract
# --------------------------------------------------------------------------


class TestLangSmithTracingEnabled:
    """Flag MUST be the literal lowercase string `"true"`. Anything
    else — case variants, "1"/"yes"/"on" aliases, the empty string —
    means langsmith won't trace, so we must report OFF too."""

    def test_lowercase_true_with_key_enables(self, monkeypatch):
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is True

    @pytest.mark.parametrize(
        "value",
        [
            "True",   # capital T — langsmith REJECTS this
            "TRUE",   # all caps
            "1",      # the "obvious" truthy alias langsmith REJECTS
            "yes",
            "on",
        ],
    )
    def test_non_strict_truthy_values_are_OFF(self, monkeypatch, value):
        """The PR #90 review caught this: my initial _TRUTHY set
        included {1, yes, on, True}. langsmith's actual check is
        `var_result == "true"` — strict lowercase. So any of these
        values means langsmith says OFF, and our module MUST agree
        or the status lies in the most dangerous direction
        (operator sees ON, no spans actually flow)."""
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", value)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is False, (
            f"Flag value {value!r} should NOT enable tracing — "
            "langsmith only accepts lowercase 'true' exactly."
        )

    @pytest.mark.parametrize(
        "value", ["false", "0", "no", "off", "", "FALSE"],
    )
    def test_obviously_falsy_values_are_OFF(self, monkeypatch, value):
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", value)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is False

    def test_flag_without_key_returns_false(self, monkeypatch):
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        assert langsmith_tracing_enabled() is False

    def test_key_without_flag_returns_false(self, monkeypatch):
        """Key set but flag off → still off. langsmith won't trace
        without the flag, so we must agree."""
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is False

    def test_whitespace_only_key_treated_as_missing(self, monkeypatch):
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "   ")
        assert langsmith_tracing_enabled() is False


# --------------------------------------------------------------------------
# Two-namespace lookup — every path langsmith reads
# --------------------------------------------------------------------------


class TestNamespaceFallback:
    """langsmith reads `LANGSMITH_*` AND `LANGCHAIN_*` (legacy),
    plus `_V2` / non-`_V2` variants. An operator who followed an
    older LangChain tutorial exported `LANGCHAIN_TRACING_V2=true`
    + `LANGCHAIN_API_KEY=...` — langsmith sees those and traces,
    but a module that only reads `LANGSMITH_*` would falsely
    report OFF.

    Pin each of the four flag-var paths + both key-var paths.
    """

    @pytest.mark.parametrize("flag_var", ALL_FLAG_VARS)
    def test_any_flag_var_enables_when_set_to_true(
        self, monkeypatch, flag_var,
    ):
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv(flag_var, "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        assert langsmith_tracing_enabled() is True, (
            f"langsmith would pick up {flag_var}=true — we must too."
        )

    @pytest.mark.parametrize("key_var", ALL_KEY_VARS)
    def test_any_key_var_satisfies_the_key_requirement(
        self, monkeypatch, key_var,
    ):
        from backend.langsmith_setup import langsmith_tracing_enabled

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv(key_var, "lsv2_test_key")
        assert langsmith_tracing_enabled() is True

    def test_v2_variant_wins_over_non_v2(self, monkeypatch):
        """langsmith.utils.tracing_is_enabled reads
        `get_env_var("TRACING_V2", default=get_env_var("TRACING"))`
        — `_V2` checked first per namespace. So an empty
        `LANGSMITH_TRACING_V2` paired with a truthy
        `LANGSMITH_TRACING` should NOT enable tracing (V2 found
        empty string and short-circuited)... actually, langsmith's
        `get_env_var` skips empty strings (re-read the source), so
        the fallback DOES occur. Pin the precedence we actually
        implement: V2 wins when both set, V1 picked up when only it
        is set."""
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING_V2", "true")
        monkeypatch.setenv("LANGSMITH_TRACING", "false")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        s = langsmith_status()
        assert s["tracing_enabled"] is True
        assert s["tracing_flag_source"] == "LANGSMITH_TRACING_V2"

    def test_canonical_namespace_wins_over_legacy(self, monkeypatch):
        """`LANGSMITH_*` checked before `LANGCHAIN_*`. If both are
        set the canonical value wins, matching langsmith's lookup
        order."""
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGCHAIN_TRACING", "false")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_test_key")
        s = langsmith_status()
        assert s["tracing_enabled"] is True
        assert s["tracing_flag_source"] == "LANGSMITH_TRACING"

    def test_legacy_only_still_works(self, monkeypatch):
        """`LANGCHAIN_TRACING_V2=true` + `LANGCHAIN_API_KEY=...`
        is the form older LangChain tutorials still ship.
        langsmith picks them up; this module must too — the
        exact failure mode reviewer #90 P1 flagged."""
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
        monkeypatch.setenv("LANGCHAIN_API_KEY", "lsv2_legacy_key")
        s = langsmith_status()
        assert s["tracing_enabled"] is True
        assert s["tracing_flag_source"] == "LANGCHAIN_TRACING_V2"
        assert s["api_key_source"] == "LANGCHAIN_API_KEY"


# --------------------------------------------------------------------------
# langsmith_status payload + no-key-leak
# --------------------------------------------------------------------------


class TestLangSmithStatus:
    def test_full_state_when_everything_set(self, monkeypatch):
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.setenv("LANGSMITH_PROJECT", "personalcoach")
        monkeypatch.setenv(
            "LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"
        )

        s = langsmith_status()
        assert s["tracing_enabled"] is True
        assert s["tracing_flag"] == "true"
        assert s["tracing_flag_source"] == "LANGSMITH_TRACING"
        assert s["api_key_set"] is True
        assert s["api_key_source"] == "LANGSMITH_API_KEY"
        assert s["project"] == "personalcoach"
        assert s["endpoint"] == "https://api.smith.langchain.com"

    def test_never_echoes_api_key_value(self, monkeypatch):
        """Status is exposed via `/api/admin/observability`. The
        key is a secret and must NEVER appear in the response —
        regression-pin so a future refactor that adds
        `api_key=value` to the dict surfaces immediately."""
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_super_secret")

        s = langsmith_status()
        for v in s.values():
            assert "lsv2_super_secret" not in str(v)

    def test_project_defaults_to_default(self, monkeypatch):
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        assert langsmith_status()["project"] == "default"

    def test_endpoint_defaults_to_hosted_saas(self, monkeypatch):
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        assert (
            langsmith_status()["endpoint"]
            == "https://api.smith.langchain.com"
        )

    def test_legacy_project_namespace(self, monkeypatch):
        """`LANGCHAIN_PROJECT` also valid (mirror of API_KEY
        fallback). Verify the status payload picks it up."""
        from backend.langsmith_setup import langsmith_status

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGCHAIN_PROJECT", "my-legacy-project")
        assert langsmith_status()["project"] == "my-legacy-project"


# --------------------------------------------------------------------------
# startup_log_line — four states the operator should be able to grep
# --------------------------------------------------------------------------


class TestStartupLogLine:
    def test_on_state_includes_project_endpoint_and_source(
        self, monkeypatch,
    ):
        from backend.langsmith_setup import startup_log_line

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.setenv("LANGSMITH_PROJECT", "personalcoach")
        line = startup_log_line()
        assert "ON" in line
        assert "personalcoach" in line
        # Source so operator knows WHICH env var was picked up
        # (canonical vs legacy).
        assert "LANGSMITH_TRACING" in line
        assert "api.smith.langchain.com" in line

    def test_off_state_when_nothing_set(self, monkeypatch):
        from backend.langsmith_setup import startup_log_line

        _clear_all_langsmith_env(monkeypatch)
        line = startup_log_line()
        assert "OFF" in line
        assert "LANGSMITH_TRACING" in line
        assert "LANGCHAIN_TRACING" in line  # legacy named too

    def test_misconfigured_flag_set_but_no_key(self, monkeypatch):
        """Dangerous silent-fail: flag is correctly "true" but no
        key in either namespace → langsmith would 401 every span."""
        from backend.langsmith_setup import startup_log_line

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        line = startup_log_line()
        assert "MISCONFIGURED" in line
        assert "LANGSMITH_API_KEY" in line
        assert "LANGCHAIN_API_KEY" in line  # mention legacy form too

    @pytest.mark.parametrize(
        "bad_flag", ["True", "TRUE", "1", "yes", "on"],
    )
    def test_misconfigured_when_flag_is_non_strict_truthy(
        self, monkeypatch, bad_flag,
    ):
        """Critical new state — operator set the flag but to a
        value langsmith won't accept. Without surfacing this,
        the operator stares at `LANGSMITH_TRACING=1` thinking
        "it's on" while langsmith says OFF."""
        from backend.langsmith_setup import startup_log_line

        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", bad_flag)
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        line = startup_log_line()
        assert "MISCONFIGURED" in line
        # Explain the contract so operator knows what to fix.
        assert "true" in line.lower()


# --------------------------------------------------------------------------
# /api/admin/observability endpoint
# --------------------------------------------------------------------------


class TestObservabilityEndpoint:
    def test_returns_status_shape(self, client, monkeypatch):
        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_xxx")
        monkeypatch.setenv("LANGSMITH_PROJECT", "personalcoach")

        resp = client.get("/api/admin/observability")
        assert resp.status_code == 200
        body = resp.json()
        # Pin the contract — clients may read these.
        assert set(body.keys()) == {
            "tracing_enabled", "tracing_flag", "tracing_flag_source",
            "api_key_set", "api_key_source", "project", "endpoint",
        }
        assert body["tracing_enabled"] is True
        assert body["api_key_set"] is True

    def test_endpoint_never_returns_key_value(self, client, monkeypatch):
        """HTTP-layer pin (separate from the langsmith_status unit
        test) — catches a refactor that bypasses the helper and
        writes the response dict directly."""
        secret = "lsv2_endpoint_secret_token_xyz"
        _clear_all_langsmith_env(monkeypatch)
        monkeypatch.setenv("LANGSMITH_TRACING", "true")
        monkeypatch.setenv("LANGSMITH_API_KEY", secret)

        resp = client.get("/api/admin/observability")
        assert secret not in resp.text

    def test_off_state_reported(self, client, monkeypatch):
        _clear_all_langsmith_env(monkeypatch)
        resp = client.get("/api/admin/observability")
        body = resp.json()
        assert body["tracing_enabled"] is False
        assert body["api_key_set"] is False
        assert body["tracing_flag_source"] is None
        assert body["api_key_source"] is None

    def test_endpoint_lives_under_admin_namespace(self, client):
        """Path lives under `/api/admin/*` so a future auth
        middleware can match by path prefix. Pin the URL so a
        well-meaning rename doesn't silently un-namespace the
        endpoint."""
        # Old path returns 404.
        resp_old = client.get("/api/debug/observability")
        assert resp_old.status_code == 404
        # New path works.
        resp_new = client.get("/api/admin/observability")
        assert resp_new.status_code == 200
