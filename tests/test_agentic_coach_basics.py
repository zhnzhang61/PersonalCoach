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
      3. Keep the persona body (`_SYSTEM_PROMPT`) intact so a refactor
         that drops it from the f-string can't slip through.
    """

    def test_prepends_today_system_message(self):
        import datetime
        from zoneinfo import ZoneInfo

        from langchain_core.messages import HumanMessage, SystemMessage

        from backend.agentic_coach import _SYSTEM_PROMPT, _user_tz

        msgs = _build_prompt({"messages": [HumanMessage(content="hi")]})
        assert isinstance(msgs[0], SystemMessage)
        # ISO date appears in the system message — and it's the date
        # in the USER's tz, not the server's. Compute the expected
        # date the same way _build_prompt does so the test passes
        # regardless of where it runs (UTC CI, local dev, etc.).
        expected_today = datetime.datetime.now(_user_tz()).date().isoformat()
        assert expected_today in msgs[0].content
        # Pin the exact anti-past-scheduling clause — a softer
        # phrasing like "be careful about past dates" would still
        # contain the word "past" but defeats the actual fix.
        assert (
            "Never schedule planned workouts in the past."
            in msgs[0].content
        )
        # Persona body survives the concatenation. Without this,
        # a refactor that swaps `content=f"{header}\n\n{_SYSTEM_PROMPT}"`
        # to `content=header` would still pass every other assertion
        # and leave the agent with no persona prompt at all.
        assert _SYSTEM_PROMPT in msgs[0].content
        # Original conversation still there, untouched.
        assert isinstance(msgs[-1], HumanMessage)
        assert msgs[-1].content == "hi"

    def test_chinese_relative_time_words_covered(self):
        """Repro that motivated the PR was Chinese ("今天40min easy
        run"). The header has to enumerate Chinese relative-time words
        too — a literalist LLM may not generalize from "today" to
        "今天"."""
        msgs = _build_prompt({})
        content = msgs[0].content
        for word in ("今天", "明天", "后天", "这周"):
            assert word in content, f"header missing {word!r}"

    def test_empty_messages_state_ok(self):
        """First turn — state may not have messages yet."""
        msgs = _build_prompt({})
        assert len(msgs) == 1  # just the system message
        assert "Today is" in msgs[0].content

    def test_create_react_agent_receives_callable_not_list(self):
        """The fix only works if LangGraph re-invokes _build_prompt
        every turn. Pin that the `prompt=` arg in `_ensure_agent` is
        the function object, not its eagerly-evaluated result — a
        future refactor that swaps `prompt=_build_prompt,` to
        `prompt=_build_prompt({}),` would silently freeze "today" at
        agent construction (back to the v7-era bug)."""
        import inspect

        from backend import agentic_coach

        src = inspect.getsource(agentic_coach.AgenticCoach._ensure_agent)
        assert "prompt=_build_prompt," in src
        assert "prompt=_build_prompt(" not in src

    def test_tool_errors_are_recoverable_not_fatal(self):
        """create_react_agent's default re-raises ToolException (MCP
        tool 4xx/5xx) and crashes the turn. _ensure_agent must wrap the
        tools in a ToolNode with our handle_tool_errors so a backend
        tool failure becomes a ToolMessage the model can recover from.
        Pin both the wiring and the handler's contract."""
        import inspect

        from backend import agentic_coach
        from backend.agentic_coach import _handle_tool_error

        src = inspect.getsource(agentic_coach.AgenticCoach._ensure_agent)
        assert "ToolNode(" in src
        assert "handle_tool_errors=_handle_tool_error" in src

        # Handler turns any exception into a non-empty recovery string
        # (becomes the ToolMessage). It must carry the original error so
        # the model can self-correct, and must NOT claim success.
        msg = _handle_tool_error(
            RuntimeError("/api/memory/coach-fact → HTTP 400: "
                         "Unknown coach-intake area 'Cycle.psychology'. "
                         "Did you mean 'Profile.psychology'?")
        )
        assert "Profile.psychology" in msg  # original error preserved
        assert "重试" in msg or "retry" in msg.lower()

    def test_user_tz_override_via_env(self, monkeypatch):
        """PERSONAL_COACH_TZ env var overrides process-local tz. A
        UTC server with a Shanghai user needs this to avoid putting
        the agent in a different day than the user."""
        from zoneinfo import ZoneInfo

        from backend.agentic_coach import _user_tz

        monkeypatch.setenv("PERSONAL_COACH_TZ", "Asia/Shanghai")
        assert _user_tz() == ZoneInfo("Asia/Shanghai")

    def test_user_tz_bad_override_falls_back(self, monkeypatch):
        """An unparseable IANA name shouldn't crash the agent — fall
        back to process-local rather than raising at every turn."""
        from backend.agentic_coach import _user_tz

        monkeypatch.setenv("PERSONAL_COACH_TZ", "Not/A_Real_Zone")
        tz = _user_tz()
        # Anything truthy is fine — we just don't want a raise.
        assert tz is not None

    def test_prompt_hash_covers_wrapper(self, tmp_chat_db):
        """The recorded `self._prompt_hash` has to move when the
        wrapper template changes — otherwise PROMPT_CHANGELOG can
        drift from what the LLM actually saw. Verify by comparing to
        the hash of the sentinel-rendered template."""
        from backend.agentic_coach import (
            _HEADER_TEMPLATE,
            _SYSTEM_PROMPT,
        )
        from backend.trace_logger import prompt_hash

        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        expected = prompt_hash(
            _HEADER_TEMPLATE.format(
                today_iso="0000-00-00", weekday="Sentinel"
            )
            + "\n\n"
            + _SYSTEM_PROMPT
        )
        assert coach._prompt_hash == expected
        # And just to make sure the hash is NOT the persona-only hash —
        # the regression we're guarding against.
        assert coach._prompt_hash != prompt_hash(_SYSTEM_PROMPT)


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


class TestCoachIntakeWiring:
    """§3.4.5 PR-2: the intake block is spliced into the persona, the
    version is bumped, and the prefetch plans read the A/B coverage."""

    def test_prompt_version_is_v11(self):
        from backend.agentic_coach import PROMPT_VERSION

        # v9 wired the A/B intake (#95); v10 wired the daily check-in;
        # v11 added the record-before-claim + memory-promotion rules to
        # the intake block (claim-vs-action enforcement PR). Each
        # behavior change is its own §3.4.3 changelog row.
        assert PROMPT_VERSION == "v11"

    def test_system_prompt_contains_intake_block(self):
        from backend.agentic_coach import _SYSTEM_PROMPT
        from backend.coach_intake import render_intake_prompt_section

        # the rendered block is appended verbatim
        assert render_intake_prompt_section() in _SYSTEM_PROMPT
        # spot-check markers + a known exemplar
        assert "get_coach_profile" in _SYSTEM_PROMPT
        assert "get_cycle_config" in _SYSTEM_PROMPT
        assert "Berlin 2026-09-21" in _SYSTEM_PROMPT

    def test_make_plan_prefetch_reads_profile_and_cycle(self):
        from backend.agentic_coach import _prefetch_make_plan

        tools = [t for t, _ in _prefetch_make_plan()]
        assert "get_coach_profile" in tools
        assert "get_cycle_config" in tools

    def test_review_workout_prefetch_reads_profile(self):
        from backend.agentic_coach import _prefetch_review_workout

        tools = [t for t, _ in _prefetch_review_workout(123, "2026-05-20")]
        assert "get_coach_profile" in tools

    def test_prompt_hash_covers_intake_block(self, tmp_chat_db):
        """The hash must cover the appended intake block, so an exemplar
        edit in coach_intake.py shows up as a prompt change (and forces a
        PROMPT_VERSION bump per §3.4.3)."""
        from backend.agentic_coach import (
            _HEADER_TEMPLATE,
            _SYSTEM_PROMPT,
            AgenticCoach,
        )
        from backend.coach_intake import render_intake_prompt_section
        from backend.trace_logger import prompt_hash

        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        block = render_intake_prompt_section()
        assert block in _SYSTEM_PROMPT
        # Hashing the persona with the intake block stripped out must differ
        # → the block is genuinely part of the hashed/versioned prompt.
        base = _SYSTEM_PROMPT.replace("\n\n" + block, "")
        header = _HEADER_TEMPLATE.format(
            today_iso="0000-00-00", weekday="Sentinel"
        )
        assert coach._prompt_hash != prompt_hash(header + "\n\n" + base)


class TestDailyCheckinWiring:
    """v10: the daily check-in (P3 perceived stream) is now consumed by the
    agent — prefetched on review_workout / make_plan / review_health, and
    the prompt instructs reading it before asking "today how do you feel"."""

    def test_prompt_describes_daily_checkin_stream(self):
        from backend.agentic_coach import _SYSTEM_PROMPT

        # the tool name + the 4 slider field names + the anti-re-ask rule
        assert "get_recent_checkins" in _SYSTEM_PROMPT
        for slider in ("sleep_quality", "soreness", "mood", "motivation"):
            assert slider in _SYSTEM_PROMPT
        # the "don't re-ask the high-level question" rule, in either language
        assert "今天感觉如何" in _SYSTEM_PROMPT

    def test_prompt_pairs_checkin_with_readiness(self):
        """The check-in is SUBJECTIVE; readiness is OBJECTIVE; the prompt
        must not collapse them into one (feedback_perceived_vs_intent)."""
        from backend.agentic_coach import _SYSTEM_PROMPT

        # both names appear in the perceived section
        assert "get_readiness" in _SYSTEM_PROMPT
        assert "SUBJECTIVE" in _SYSTEM_PROMPT and "OBJECTIVE" in _SYSTEM_PROMPT

    def test_review_health_prefetch_includes_checkin(self):
        from backend.agentic_coach import _prefetch_review_health

        plan = _prefetch_review_health()
        assert ("get_recent_checkins", {"days": 7}) in plan

    def test_make_plan_prefetch_includes_checkin(self):
        from backend.agentic_coach import _prefetch_make_plan

        plan = _prefetch_make_plan()
        assert ("get_recent_checkins", {"days": 7}) in plan

    def test_review_workout_prefetch_includes_checkin(self):
        from backend.agentic_coach import _prefetch_review_workout

        plan = _prefetch_review_workout(123, "2026-05-29")
        assert ("get_recent_checkins", {"days": 7}) in plan
