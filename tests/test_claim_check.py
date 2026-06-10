"""Tests for the claim-vs-action enforcement (backend/claim_check.py +
the AgenticCoach wiring).

Motivating incident (2026-05-30, thread coach_20260530T143307Z's trace):
the user answered every intake slot in one message; the model replied
"收到…我已将以下信息更新至你的档案…" with ZERO tool calls. The recording
was claimed, not performed. These tests pin three things:

  1. the detector — which sentences count as a completed-write claim
     (future-tense promises and descriptive reads must NOT trigger);
  2. the sync enforcement helper — claim without call → one correction
     round; still lying → deterministic warning appended;
  3. the streaming path — correction round streams as a continuation,
     real writes emit fact_recorded events, history carries the
     facts_recorded field derived from checkpointed tool calls.

Pure sync — asyncio.run at call sites per project convention.
"""

from __future__ import annotations

import asyncio
from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from backend.agentic_coach import AgenticCoach
from backend.claim_check import (
    CORRECTION_PROMPT,
    SENTINEL,
    WARNING_LINE,
    claims_recording,
    has_recording_call,
    recorded_areas,
)


# ---------------------------------------------------------------------------
# Detector — claims_recording
# ---------------------------------------------------------------------------


class TestClaimsRecordingPositives:
    def test_the_exact_may30_lie(self):
        """The production incident, verbatim shape."""
        assert claims_recording(
            "收到，非常清晰！根据你的反馈，我已将以下信息更新至你的档案：目标：纽马（2026-11-01）"
        )

    def test_bare_perfective(self):
        assert claims_recording("好的，已记录。")

    def test_perfective_with_beneficiary(self):
        assert claims_recording("我已经为你记录在案，后续会按此安排。")

    def test_profile_updated(self):
        assert claims_recording("你的档案已更新，目标比赛为纽约马拉松。")

    def test_perfective_update_to_profile(self):
        assert claims_recording("已更新你的档案。")

    def test_english_perfect_tense(self):
        assert claims_recording("I have recorded this in your profile.")

    def test_english_passive(self):
        assert claims_recording("Your goal race has been recorded.")


class TestClaimsRecordingNegatives:
    def test_future_promise_hui(self):
        """The legitimate ask-then-record flow says exactly this while
        waiting for the user's answer — must NOT trigger."""
        assert not claims_recording("收到后我会记录下来，我们再逐步完善其他部分。")

    def test_future_promise_idiom(self):
        assert not claims_recording("等你回答后我会把它记录在案。")

    def test_future_jiang(self):
        assert not claims_recording("我将为你更新档案。")

    def test_descriptive_read_of_existing_profile(self):
        """Reading existing state ("the profile says X") is not a
        write claim."""
        assert not claims_recording("你的长期档案记录着「极其讨厌下雨天跑步」。")

    def test_instructional_mention(self):
        assert not claims_recording("用户回答后先用 record_coach_fact 存下，再出计划。")

    def test_suggestion_to_user(self):
        assert not claims_recording("建议你记录一下今天的体感。")

    def test_empty_and_none_like(self):
        assert not claims_recording("")

    def test_ordinary_coaching_prose(self):
        assert not claims_recording(
            "今天的训练心率控制得很好，保持这个节奏，周六长跑注意补给。"
        )


# ---------------------------------------------------------------------------
# Detector — tool-call helpers
# ---------------------------------------------------------------------------


class TestToolCallHelpers:
    def test_has_recording_call_true(self):
        calls = [
            {"name": "get_cycle_config"},
            {"name": "record_coach_fact", "args": {"area": "Cycle.goal"}},
        ]
        assert has_recording_call(calls)

    def test_has_recording_call_false_on_reads_only(self):
        calls = [
            {"name": "get_cycle_config"},
            {"name": "recall_topics"},
            {"name": "_prefetch_batch", "prefetched": True},
        ]
        assert not has_recording_call(calls)

    def test_has_recording_call_empty_and_none(self):
        assert not has_recording_call([])
        assert not has_recording_call(None)  # type: ignore[arg-type]

    def test_recorded_areas_dict_args(self):
        calls = [
            {"name": "record_coach_fact", "args": {"area": "Cycle.goal"}},
            {"name": "get_readiness"},
            {"name": "record_coach_fact", "args": {"area": "Profile.devices"}},
        ]
        assert recorded_areas(calls) == ["Cycle.goal", "Profile.devices"]

    def test_recorded_areas_string_args(self):
        """Trace entries may store args as a truncated repr string —
        extract area from that shape too."""
        calls = [
            {
                "name": "record_coach_fact",
                "args": "{'area': 'Cycle.goal', 'raw_text': '纽马…'}",
            },
        ]
        assert recorded_areas(calls) == ["Cycle.goal"]

    def test_recorded_areas_falls_back_to_question_mark(self):
        """A write whose args got truncated past the area key must not
        disappear from the badge — fall back to '?'."""
        calls = [{"name": "record_coach_fact", "args": "{'raw_te…"}]
        assert recorded_areas(calls) == ["?"]


# ---------------------------------------------------------------------------
# Sync enforcement — AgenticCoach._enforce_record_claim
# ---------------------------------------------------------------------------


def _coach(tmp_chat_db) -> AgenticCoach:
    return AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)


def _trace() -> SimpleNamespace:
    return SimpleNamespace(extras={})


class TestEnforceRecordClaim:
    def test_no_claim_passes_through(self, tmp_chat_db):
        coach = _coach(tmp_chat_db)
        coach._run_turn_inner = AsyncMock()  # must not be called
        trace = _trace()
        out = asyncio.run(
            coach._enforce_record_claim("普通回答，没有声称。", "t", [], trace)
        )
        assert out == "普通回答，没有声称。"
        coach._run_turn_inner.assert_not_called()
        assert "claim_check" not in trace.extras

    def test_claim_with_real_call_passes_through(self, tmp_chat_db):
        coach = _coach(tmp_chat_db)
        coach._run_turn_inner = AsyncMock()
        trace = _trace()
        calls = [{"name": "record_coach_fact", "args": {"area": "Cycle.goal"}}]
        out = asyncio.run(
            coach._enforce_record_claim("已记录。", "t", calls, trace)
        )
        assert out == "已记录。"
        coach._run_turn_inner.assert_not_called()

    def test_lie_triggers_correction_that_records(self, tmp_chat_db):
        """The forcing function: claim + no call → correction round runs
        with CORRECTION_PROMPT; the model records for real → combined
        answer, extras mark corrected=True with the area."""
        coach = _coach(tmp_chat_db)

        async def _fake_correction(user_input, thread_id, ctx, tool_calls_sink=None):
            assert user_input == CORRECTION_PROMPT
            assert user_input.startswith(SENTINEL)
            tool_calls_sink.append(
                {"name": "record_coach_fact", "args": {"area": "Cycle.goal"}}
            )
            return "已通过工具完成记录：纽马 2026-11-01。\n\n[Generated by gemini-x]"

        coach._run_turn_inner = _fake_correction
        trace = _trace()
        calls: list[dict] = [{"name": "get_cycle_config"}]
        out = asyncio.run(
            coach._enforce_record_claim(
                "我已将以下信息更新至你的档案。\n\n[Generated by gemini-x]",
                "t",
                calls,
                trace,
            )
        )
        assert "已通过工具完成记录" in out
        # Inner footer stripped — exactly one footer in the combined text.
        assert out.count("[Generated by") == 1
        assert WARNING_LINE not in out
        assert trace.extras["claim_check"]["triggered"] is True
        assert trace.extras["claim_check"]["corrected"] is True
        assert trace.extras["claim_check"]["areas"] == ["Cycle.goal"]

    def test_double_lie_gets_warning(self, tmp_chat_db):
        """Correction round STILL claims without calling → the
        deterministic warning is appended so the false claim can never
        present itself as clean."""
        coach = _coach(tmp_chat_db)

        async def _still_lying(user_input, thread_id, ctx, tool_calls_sink=None):
            return "我已经记录在案了。\n\n[Generated by gemini-x]"

        coach._run_turn_inner = _still_lying
        trace = _trace()
        out = asyncio.run(
            coach._enforce_record_claim("已记录。", "t", [], trace)
        )
        assert WARNING_LINE in out
        assert trace.extras["claim_check"]["corrected"] is False

    def test_retraction_gets_no_warning(self, tmp_chat_db):
        """Correction round retracts honestly (option 2 in the prompt) —
        no write happened, but no false claim stands either: no warning."""
        coach = _coach(tmp_chat_db)

        async def _retracts(user_input, thread_id, ctx, tool_calls_sink=None):
            return "更正：该信息尚未被写入档案，请在下一轮确认后我再提交。"

        coach._run_turn_inner = _retracts
        trace = _trace()
        out = asyncio.run(
            coach._enforce_record_claim("已记录。", "t", [], trace)
        )
        assert WARNING_LINE not in out
        assert "更正" in out
        assert trace.extras["claim_check"]["corrected"] is False


# ---------------------------------------------------------------------------
# Streaming path — correction round + fact_recorded events
# ---------------------------------------------------------------------------


def _chunk(text):
    c = MagicMock()
    c.content = text
    return c


def _ev(event: str, name: str = "", chunk=None, _input=None):
    return {
        "event": event,
        "name": name,
        "data": {
            **({"chunk": chunk} if chunk is not None else {}),
            **({"input": _input} if _input is not None else {}),
        },
    }


async def _fake_stream(events):
    for ev in events:
        yield ev


def _collect(gen):
    async def _drain():
        return [ev async for ev in gen]

    return asyncio.run(_drain())


class TestChatStreamFactRecorded:
    def test_record_tool_end_emits_fact_recorded(self, tmp_chat_db):
        events = [
            _ev("on_tool_start", name="record_coach_fact"),
            _ev(
                "on_tool_end",
                name="record_coach_fact",
                _input={"area": "Cycle.goal", "raw_text": "纽马"},
            ),
            _ev("on_chat_model_stream", chunk=_chunk("已记录：纽马。")),
        ]
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        coach._ensure_agent = AsyncMock()
        coach._agent = MagicMock()
        coach._agent.astream_events = MagicMock(
            return_value=_fake_stream(events)
        )
        coach._last_provider = "gemini"

        result = _collect(coach.chat_stream(user_input="记一下", thread_id="t"))

        assert {"type": "fact_recorded", "area": "Cycle.goal"} in result
        # The claim is backed by a real call (the tracer's callback isn't
        # active on the mocked agent, but on_tool events don't feed
        # trace.tool_calls here — what matters is no correction round:
        # astream_events called exactly once… see next test for the
        # triggered case.

    def test_other_tool_end_does_not_emit(self, tmp_chat_db):
        events = [
            _ev("on_tool_start", name="get_readiness"),
            _ev("on_tool_end", name="get_readiness"),
            _ev("on_chat_model_stream", chunk=_chunk("绿灯。")),
        ]
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        coach._ensure_agent = AsyncMock()
        coach._agent = MagicMock()
        coach._agent.astream_events = MagicMock(
            return_value=_fake_stream(events)
        )
        coach._last_provider = "gemini"

        result = _collect(coach.chat_stream(user_input="hi", thread_id="t"))
        assert not any(r["type"] == "fact_recorded" for r in result)


class TestChatStreamCorrectionRound:
    def test_streamed_lie_triggers_streamed_correction(self, tmp_chat_db):
        """First drive claims recording with no record call → a second
        astream_events drive runs with the correction prompt; its
        tokens stream as a continuation and its real write emits
        fact_recorded."""
        first = [
            _ev("on_chat_model_stream", chunk=_chunk("我已将信息更新至你的档案。")),
        ]
        second = [
            _ev("on_tool_start", name="record_coach_fact"),
            _ev(
                "on_tool_end",
                name="record_coach_fact",
                _input={"area": "Cycle.goal"},
            ),
            _ev("on_chat_model_stream", chunk=_chunk("已实际写入：Cycle.goal。")),
        ]
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        coach._ensure_agent = AsyncMock()
        coach._agent = MagicMock()
        coach._agent.astream_events = MagicMock(
            side_effect=[_fake_stream(first), _fake_stream(second)]
        )
        coach._last_provider = "gemini"

        result = _collect(coach.chat_stream(user_input="记一下", thread_id="t"))

        # Two drives happened.
        assert coach._agent.astream_events.call_count == 2
        # The second drive's messages carry the correction prompt.
        second_msgs = coach._agent.astream_events.call_args_list[1][0][0][
            "messages"
        ]
        assert second_msgs[0].content == CORRECTION_PROMPT
        # Correction tokens streamed as continuation; real write surfaced.
        tokens = "".join(
            r["content"] for r in result if r["type"] == "token"
        )
        assert "已实际写入" in tokens
        assert {"type": "fact_recorded", "area": "Cycle.goal"} in result
        assert result[-1] == {"type": "done"}

    def test_no_claim_no_second_drive(self, tmp_chat_db):
        events = [
            _ev("on_chat_model_stream", chunk=_chunk("普通回答。")),
        ]
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        coach._ensure_agent = AsyncMock()
        coach._agent = MagicMock()
        coach._agent.astream_events = MagicMock(
            return_value=_fake_stream(events)
        )
        coach._last_provider = "gemini"

        _ = _collect(coach.chat_stream(user_input="hi", thread_id="t"))
        assert coach._agent.astream_events.call_count == 1


# ---------------------------------------------------------------------------
# History — facts_recorded derivation + correction-prompt filtering
# ---------------------------------------------------------------------------

StubTuple = namedtuple("StubTuple", ["checkpoint"])


def _msg(role: str, content: str, tool_calls: list | None = None):
    m = MagicMock()
    m.type = role
    m.content = content
    # Plain MagicMock attribute access returns a child mock; the
    # production walk only consumes real lists, so set explicitly.
    m.tool_calls = tool_calls if tool_calls is not None else None
    return m


def _ckpt(ts, messages):
    return {"ts": ts, "channel_values": {"messages": messages}}


def _coach_with(tmp_chat_db, tuples_newest_first):
    coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
    coach.checkpointer = MagicMock()
    coach.checkpointer.list = MagicMock(
        return_value=iter(tuples_newest_first)
    )
    return coach


class TestHistoryFactsRecorded:
    def test_facts_attach_to_turns_final_text_message(self, tmp_chat_db):
        """ReAct turn: tool-calling ai msg (no text) → tool msg → final
        text ai msg. The areas written land on the FINAL message."""
        h = _msg("human", "帮我记一下纽马")
        ai_tool = _msg(
            "ai",
            "",
            tool_calls=[
                {
                    "name": "record_coach_fact",
                    "args": {"area": "Cycle.goal", "raw_text": "纽马"},
                    "id": "x",
                }
            ],
        )
        tool = _msg("tool", '{"action":"created"}')
        ai_final = _msg("ai", "已记录：纽马 2026-11-01。")
        tuples = [StubTuple(_ckpt("2026-06-10T01:00:00Z", [h, ai_tool, tool, ai_final]))]
        coach = _coach_with(tmp_chat_db, tuples)

        out = coach.get_history_with_ts("t")

        final = [r for r in out if r["role"] == "ai" and r["content"]]
        assert final[-1]["facts_recorded"] == ["Cycle.goal"]
        # The empty tool-calling ai message carries no badge field.
        empties = [r for r in out if r["role"] == "ai" and not r["content"]]
        assert all("facts_recorded" not in r for r in empties)

    def test_turn_without_write_has_no_field(self, tmp_chat_db):
        h = _msg("human", "hi")
        a = _msg("ai", "hello")
        tuples = [StubTuple(_ckpt("2026-06-10T01:00:00Z", [h, a]))]
        coach = _coach_with(tmp_chat_db, tuples)
        out = coach.get_history_with_ts("t")
        assert all("facts_recorded" not in r for r in out)

    def test_correction_prompt_hidden_from_history(self, tmp_chat_db):
        """The injected [系统校验] message is a system artifact — it must
        never render as a 'human' bubble."""
        h = _msg("human", "记一下")
        a1 = _msg("ai", "我已将信息更新至你的档案。")
        corr = _msg("human", CORRECTION_PROMPT)
        a2 = _msg("ai", "已实际完成记录。")
        tuples = [
            StubTuple(_ckpt("2026-06-10T01:00:00Z", [h, a1, corr, a2]))
        ]
        coach = _coach_with(tmp_chat_db, tuples)

        out = coach.get_history_with_ts("t")

        humans = [r["content"] for r in out if r["role"] == "human"]
        assert humans == ["记一下"]
        assert all(SENTINEL not in r["content"] for r in out)

    def test_correction_prompt_hidden_from_consolidation_input(
        self, tmp_chat_db
    ):
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        coach.get_history = MagicMock(
            return_value=[
                _msg("human", "记一下"),
                _msg("ai", "我已将信息更新至你的档案。"),
                _msg("human", CORRECTION_PROMPT),
                _msg("ai", "已实际完成记录。"),
            ]
        )
        out = coach._chat_list_for_thread("t")
        humans = [r["content"] for r in out if r["role"] == "human"]
        assert humans == ["记一下"]
