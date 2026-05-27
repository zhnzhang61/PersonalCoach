"""Phase 2 placeholder: bare-minimum unit tests for AgenticCoach that
don't need a running api_server (thanks to skip_api_probe=True).

The fuller test_agentic_coach.py — session lifecycle, idempotent
archive, delete_session guards, history wire-shape — is a Phase 3
follow-up. This file just locks in that the constructor + module-
level helpers work in isolation, so other tests can rely on it.
"""

from __future__ import annotations

import pytest

from backend.agentic_coach import (
    AgenticCoach,
    _build_prompt,
    _started_at_from_thread_id,
)


class TestSkipApiProbe:
    def test_constructs_without_api_server(self, tmp_chat_db):
        """The whole point of skip_api_probe=True: tests can build an
        AgenticCoach without spinning up api_server. Constructor sets
        up sqlite + session_meta sidecar; doesn't touch the network."""
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        assert coach.db_path == tmp_chat_db
        assert coach._skip_api_probe is True
        # session_meta table got created during __init__.
        rows = coach.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_meta'"
        ).fetchall()
        assert rows == [("session_meta",)]

    def test_default_keeps_probe_on(self, tmp_chat_db):
        """Production path: skip_api_probe defaults to False so a
        forgotten flag can't accidentally disable the api-reachable
        check in production."""
        coach = AgenticCoach(db_path=tmp_chat_db)
        assert coach._skip_api_probe is False


class TestBuildPrompt:
    """The LangGraph `prompt` callable. Has to:
      1. Inject today's date so the agent doesn't plan workouts in
         the past (real bug 2026-05-27: agent picked 2026-05-14 for
         "排个今天的 easy run").
      2. Preserve the conversation history that LangGraph already
         accumulated in state["messages"].
    """

    def test_prepends_today_system_message(self):
        from datetime import date

        from langchain_core.messages import HumanMessage, SystemMessage

        msgs = _build_prompt({"messages": [HumanMessage(content="hi")]})
        assert isinstance(msgs[0], SystemMessage)
        # ISO date appears in the system message.
        assert date.today().isoformat() in msgs[0].content
        # Plus an explicit anti-past-scheduling instruction.
        assert "past" in msgs[0].content.lower()
        # Original conversation still there, untouched.
        assert isinstance(msgs[-1], HumanMessage)
        assert msgs[-1].content == "hi"

    def test_empty_messages_state_ok(self):
        """First turn — state may not have messages yet."""
        msgs = _build_prompt({})
        assert len(msgs) == 1  # just the system message
        assert "Today is" in msgs[0].content


class TestStartedAtFromThreadId:
    """Module-level helper. Pure function, easy to test."""

    def test_well_formed_id_parses(self):
        assert (
            _started_at_from_thread_id("coach_20260510T172021Z")
            == "2026-05-10T17:20:21Z"
        )

    def test_non_coach_prefix_returns_none(self):
        assert _started_at_from_thread_id("run_analysis_12345") is None

    def test_truncated_id_returns_none(self):
        assert _started_at_from_thread_id("coach_short") is None

    def test_empty_returns_none(self):
        assert _started_at_from_thread_id("") is None


class TestDeleteSessionGuard:
    """The real delete_session has a hard guard against non-coach
    thread_ids — important enough to test the actual production code,
    not just the mocked version."""

    def test_rejects_non_coach_thread_id(self, tmp_chat_db):
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        with pytest.raises(ValueError, match="non-coach thread_id"):
            coach.delete_session("evil_thread_id")

    def test_rejects_coach_prefix_but_no_z(self, tmp_chat_db):
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        with pytest.raises(ValueError, match="non-coach thread_id"):
            coach.delete_session("coach_20260510T172021")  # missing Z

    def test_accepts_well_formed_id(self, tmp_chat_db):
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        # No row exists yet — returns zero counts, doesn't raise.
        result = coach.delete_session("coach_20260510T172021Z")
        assert result["thread_id"] == "coach_20260510T172021Z"
        assert result["checkpoints_deleted"] == 0
        assert result["session_meta_deleted"] == 0
