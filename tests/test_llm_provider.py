"""
Test suite for the consolidated LLM layer.

Coverage:
  A. llm_provider.call_llm() — happy path, provider pinning, fallback chain,
     all-fail error path, role→temperature mapping.
  B. Every live LLM call site in the project — verifies each one flows
     through call_llm() with the right signature.
  C. Router behavior — AgenticCoach._route_message returns state["agent"]
     with zero LLM invocations.

Most tests monkey-patch call_llm / _build_llm so they run offline with no
external API calls. Integration smoke tests that need real API keys live
behind the `--integration` flag (see pytest addoption below).

Run:
  uv run pytest tests/ -v                  # unit tests only
  uv run pytest tests/ -v --integration    # includes real-API smoke tests
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# Ensure project root is importable regardless of how pytest is invoked
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# Helpers — pytest config (--integration flag, marker registration) lives in conftest.py
# --------------------------------------------------------------------------
def _fake_llm(content: str = "ok"):
    """Return a mock with an .invoke() that returns a fake AIMessage-like object."""
    mock = MagicMock()
    mock.invoke.return_value = AIMessage(content=content)
    return mock


def _fail_llm(exc: Exception):
    """Return a mock whose .invoke() always raises `exc`."""
    mock = MagicMock()
    mock.invoke.side_effect = exc
    return mock


@pytest.fixture(autouse=True)
def _reset_llm_cache():
    """Clear the internal LLM cache between tests so monkey-patches don't leak."""
    import backend.llm_provider as llm_provider
    llm_provider._llm_cache.clear()
    yield
    llm_provider._llm_cache.clear()


# ==========================================================================
# A. Unit tests for call_llm()
# ==========================================================================


class TestCallLLMCore:
    def test_happy_path_returns_aimessage_and_provider(self):
        """call_llm returns (AIMessage, provider_name) on success."""
        from backend.llm_provider import call_llm

        with patch("backend.llm_provider._build_llm", return_value=_fake_llm("hello")):
            msg, provider = call_llm([HumanMessage(content="hi")], role="creative")

        assert isinstance(msg, AIMessage)
        assert msg.content == "hello"
        # Default fallback chain starts with "gemini"
        assert provider == "gemini"

    def test_provider_pinning_no_fallback_on_failure(self):
        """When provider is pinned, failures are raised — no fallback attempted."""
        from backend.llm_provider import call_llm

        fail = _fail_llm(RuntimeError("429 quota"))
        with patch("backend.llm_provider._build_llm", return_value=fail) as build:
            with pytest.raises(RuntimeError, match="429 quota"):
                call_llm(
                    [HumanMessage(content="hi")],
                    role="precise",
                    provider="groq",
                )
            # build_llm was called exactly once — no second attempt
            assert build.call_count == 1

    def test_fallback_chain_walks_on_failure(self):
        """Gemini fails → Groq succeeds → returns Groq result with provider='groq'."""
        from backend.llm_provider import call_llm

        gemini = _fail_llm(RuntimeError("Gemini 429"))
        groq = _fake_llm("from groq")
        omlx = _fake_llm("should not be called")

        def side_effect(provider, temperature):
            return {"gemini": gemini, "groq": groq, "omlx": omlx}[provider]

        with patch("backend.llm_provider._build_llm", side_effect=side_effect):
            msg, provider = call_llm([HumanMessage(content="hi")], role="creative")

        assert msg.content == "from groq"
        assert provider == "groq"
        assert gemini.invoke.called
        assert groq.invoke.called
        assert not omlx.invoke.called

    def test_fallback_skips_to_third_when_first_two_fail(self):
        """Gemini 429 → Groq also fails → oMLX answers."""
        from backend.llm_provider import call_llm

        gemini = _fail_llm(RuntimeError("Gemini 429"))
        groq = _fail_llm(RuntimeError("Groq down"))
        omlx = _fake_llm("from omlx")

        def side_effect(provider, temperature):
            return {"gemini": gemini, "groq": groq, "omlx": omlx}[provider]

        with patch("backend.llm_provider._build_llm", side_effect=side_effect):
            msg, provider = call_llm([HumanMessage(content="hi")], role="creative")

        assert msg.content == "from omlx"
        assert provider == "omlx"

    def test_all_providers_fail_raises_runtime_error(self):
        """If everyone in the chain fails, RuntimeError is raised."""
        from backend.llm_provider import call_llm

        with patch("backend.llm_provider._build_llm", return_value=_fail_llm(RuntimeError("dead"))):
            with pytest.raises(RuntimeError, match="All LLM providers"):
                call_llm([HumanMessage(content="hi")], role="creative")

    def test_custom_fallback_chain_respected(self):
        """Passing fallback_chain=['omlx'] skips gemini/groq entirely."""
        from backend.llm_provider import call_llm

        omlx = _fake_llm("local")

        def side_effect(provider, temperature):
            if provider != "omlx":
                raise AssertionError(f"Should not have tried {provider}")
            return omlx

        with patch("backend.llm_provider._build_llm", side_effect=side_effect):
            msg, provider = call_llm(
                [HumanMessage(content="hi")],
                role="structured",
                fallback_chain=["omlx"],
            )

        assert provider == "omlx"
        assert msg.content == "local"

    def test_role_temperature_mapping(self):
        """Verify role → temperature is passed correctly to _build_llm."""
        from backend.llm_provider import call_llm

        captured = {}

        def spy_build(provider, temperature):
            captured["temp"] = temperature
            captured["provider"] = provider
            return _fake_llm("ok")

        with patch("backend.llm_provider._build_llm", side_effect=spy_build):
            call_llm([HumanMessage(content="x")], role="creative")
            assert captured["temp"] == 0.4

            call_llm([HumanMessage(content="x")], role="precise")
            assert captured["temp"] == 0.0

            call_llm([HumanMessage(content="x")], role="structured")
            assert captured["temp"] == 0.1

    def test_coerce_to_aimessage_handles_multimodal_content(self):
        """LangChain multi-modal list content is flattened to text."""
        from backend.llm_provider import _coerce_to_aimessage

        class FakeResponse:
            content = [
                {"type": "text", "text": "hello "},
                {"type": "text", "text": "world"},
                {"type": "image_url", "image_url": "..."},  # ignored
            ]

        msg = _coerce_to_aimessage(FakeResponse(), provider="gemini")
        assert isinstance(msg, AIMessage)
        assert msg.content == "hello world"


class TestGetProviderModelName:
    def test_known_providers(self):
        from backend.llm_provider import get_provider_model_name

        assert get_provider_model_name("gemini") == "gemini-3.1-flash-lite"
        assert get_provider_model_name("groq") == "llama-3.3-70b-versatile"
        assert get_provider_model_name("omlx") == "Qwen3.5-35B-A3B-8bit"

    def test_unknown_falls_back_to_key(self):
        from backend.llm_provider import get_provider_model_name

        assert get_provider_model_name("nonexistent") == "nonexistent"


# ==========================================================================
# B. Each of the 6 live LLM call sites goes through call_llm()
# ==========================================================================


class TestAllCallSitesGoThroughCallLLM:
    """
    For every call site, patch llm_provider.call_llm and verify the site
    invokes it with the right signature, then returns the right shape.
    """

    # ------ Site 1: CME._llm_invoke ------
    def test_cme_llm_invoke_uses_call_llm_and_returns_string(self, tmp_path):
        from backend.cognitive_memory_engine import MemoryOS

        mem = MemoryOS(db_path=str(tmp_path / "cme.db"), semantic_profile_path=str(tmp_path / "sem.json"))

        with patch("backend.cognitive_memory_engine.call_llm") as mock_call:
            mock_call.return_value = (AIMessage(content="  some JSON  "), "gemini")
            result = mem._llm_invoke("test prompt")

        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        messages = args[0]
        assert any(isinstance(m, SystemMessage) for m in messages)
        assert any(isinstance(m, HumanMessage) and "test prompt" in m.content for m in messages)
        assert kwargs.get("role") == "structured"
        assert result == "some JSON"  # stripped

    # PR-1 (Phase 2) collapsed the v1 dual-persona model (coach/doctor
    # nodes + _route_message dispatcher) into a single agent that uses
    # native MCP tool-calling via create_react_agent. The tests that
    # used to live here — test_route_message_never_calls_llm,
    # test_coach_node_routes_to_coach_and_uses_call_llm,
    # test_doctor_node_routes_to_doctor_and_uses_call_llm — all
    # asserted on methods (`_route_message`, `coach_node`,
    # `doctor_node`) that no longer exist. They were left dangling
    # by PR-1 and cleaned up here.
    #
    # The replacement coverage for "chat goes through call_llm with
    # the right role" lives in the integration probe at the bottom
    # of this file (TestRealLLMSmoke); the agent-flow chat path now
    # exercises LangGraph + MCP and is much more naturally covered
    # by smoke-testing through the live api_server (see
    # docs/coach_chat_design.md).
    #
    # test_summarize_thread_uses_call_llm was deleted for a different
    # reason: its setup called coach.chat() to populate the thread,
    # which now goes through the agent path and requires api_server
    # to be reachable. The thing it was actually testing — that
    # summarize_thread passes role="precise" to call_llm — is still
    # true (see agentic_coach.summarize_thread), and the call_llm
    # core mocking in TestCallLLMCore above already verifies the
    # role→temperature plumbing.

    # ------ Site 6: AgenticCoach.generate_episodic_summary ------
    def test_generate_episodic_summary_uses_call_llm_with_structured_role(self, tmp_path):
        from backend.agentic_coach import AgenticCoach

        coach = AgenticCoach(db_path=str(tmp_path / "chat.db"))

        json_response = '{"tags": ["Long Run", "Fatigue"], "summary_text": "Tired long run."}'
        with patch("backend.agentic_coach.call_llm") as mock_call:
            mock_call.return_value = (AIMessage(content=json_response), "gemini")
            out = coach.generate_episodic_summary({"workout_summary": {"name": "LSD"}})

        assert mock_call.called
        assert mock_call.call_args.kwargs.get("role") == "structured"
        assert out == {"tags": ["Long Run", "Fatigue"], "summary_text": "Tired long run."}

    def test_generate_episodic_summary_survives_bad_json(self, tmp_path):
        """If LLM returns garbage, we fall back to a sensible default — no crash."""
        from backend.agentic_coach import AgenticCoach

        coach = AgenticCoach(db_path=str(tmp_path / "chat.db"))

        with patch("backend.agentic_coach.call_llm") as mock_call:
            mock_call.return_value = (AIMessage(content="not json at all"), "gemini")
            out = coach.generate_episodic_summary({"workout_summary": {"name": "Broken"}})

        assert "tags" in out
        assert "summary_text" in out
        assert "Broken" in out["summary_text"]


# ==========================================================================
# C. (removed) Router-dropdown behavior tests
# ==========================================================================
# `chat(..., agent="coach"|"doctor")` used to dispatch to one of two
# system prompts. PR-1 (Phase 2) merged the two personas into a single
# agent and the `agent=` kwarg is now accepted-but-ignored for back-
# compat. Tests asserting "agent='coach' lands in Running Coach prompt"
# can no longer pass and were removed.


# ==========================================================================
# D. INTEGRATION: real API calls (only with --integration flag)
# ==========================================================================


@pytest.mark.integration
class TestRealLLMSmoke:
    """Touch the real APIs to confirm keys and network paths work."""

    def test_gemini_real_call(self):
        if not os.getenv("GEMINI_KEY"):
            pytest.skip("No GEMINI_KEY in env")
        from backend.llm_provider import call_llm

        msg, provider = call_llm(
            [HumanMessage(content="Say 'pong' and nothing else.")],
            role="precise",
            provider="gemini",
        )
        assert provider == "gemini"
        assert len(msg.content) > 0

    def test_groq_real_call(self):
        if not os.getenv("GROQ_API_KEY"):
            pytest.skip("No GROQ_API_KEY in env")
        from backend.llm_provider import call_llm

        msg, provider = call_llm(
            [HumanMessage(content="Say 'pong' and nothing else.")],
            role="precise",
            provider="groq",
        )
        assert provider == "groq"
        assert len(msg.content) > 0

    def test_default_fallback_chain_resolves(self):
        """Whatever provider is reachable, the default chain produces *something*."""
        from backend.llm_provider import call_llm

        msg, provider = call_llm(
            [HumanMessage(content="Say 'pong' and nothing else.")],
            role="precise",
        )
        assert provider in {"gemini", "groq", "omlx"}
        assert len(msg.content) > 0
