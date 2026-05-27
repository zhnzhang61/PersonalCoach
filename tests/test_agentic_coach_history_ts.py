"""Unit tests for AgenticCoach.get_history_with_ts.

This method walks the LangGraph checkpointer chronologically and tags
each message with the ts of the first checkpoint where its list
position appeared. The /api/ai/history endpoint uses it so the Coach
UI can insert day-boundary dividers in long sessions that span
multiple calendar days (see PR A — "fix-coach-multi-day-timeline").

We mock the checkpointer directly rather than running real LangGraph
state — the unit under test is the position-based ts derivation, not
LangGraph itself. The mock returns a list of pseudo-CheckpointTuples
(namedtuples) with the (checkpoint, …) shape the production code reads.
"""

from __future__ import annotations

from collections import namedtuple
from unittest.mock import MagicMock

import pytest

from backend.agentic_coach import AgenticCoach


# Minimal stand-ins. Production CheckpointTuple has more fields but
# get_history_with_ts only touches `.checkpoint`.
StubTuple = namedtuple("StubTuple", ["checkpoint"])


def _msg(role: str, content: str):
    """Build a minimal stand-in BaseMessage. AgenticCoach reads `.type`
    and `.content`; nothing else."""
    m = MagicMock()
    m.type = role
    m.content = content
    return m


def _ckpt(ts: str | None, messages: list):
    """Build a checkpoint dict matching the LangGraph SqliteSaver shape:
    {ts, channel_values: {messages: [...]}}."""
    return {"ts": ts, "channel_values": {"messages": messages}}


def _make_coach_with_checkpoints(tmp_chat_db, tuples_newest_first):
    """Build an AgenticCoach with its checkpointer.list patched to
    return the given checkpoint tuples (newest-first, matching real
    SqliteSaver behavior). Production code reverses internally."""
    coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
    coach.checkpointer = MagicMock()
    coach.checkpointer.list = MagicMock(return_value=iter(tuples_newest_first))
    return coach


class TestGetHistoryWithTsHappyPath:
    """Single-day session, two turns, monotonic message growth."""

    def test_one_human_one_ai_same_day(self, tmp_chat_db):
        h1 = _msg("human", "hi")
        a1 = _msg("ai", "hello")
        # Chronological: ckpt @ T1 has [h1]; ckpt @ T2 has [h1, a1].
        # Production receives newest-first.
        tuples = [
            StubTuple(_ckpt("2026-05-11T10:00:01Z", [h1, a1])),
            StubTuple(_ckpt("2026-05-11T10:00:00Z", [h1])),
        ]
        coach = _make_coach_with_checkpoints(tmp_chat_db, tuples)

        result = coach.get_history_with_ts("thread")

        assert len(result) == 2
        assert result[0] == {
            "role": "human",
            "content": "hi",
            "ts": "2026-05-11T10:00:00Z",
        }
        assert result[1] == {
            "role": "ai",
            "content": "hello",
            "ts": "2026-05-11T10:00:01Z",
        }


class TestGetHistoryWithTsMultiDay:
    """The actual scenario PR A fixes: same content asked across
    multiple days. Position-based keying must NOT collapse them to
    the first day's ts (content-based keying would — that was the bug
    caught in smoke-testing)."""

    def test_same_question_three_days_each_gets_own_ts(self, tmp_chat_db):
        # Day 1: user asks Q, agent replies → 2 msgs
        # Day 2: user asks same Q, agent replies → 4 msgs
        # Day 3: user asks same Q again → 5 msgs
        q = "请评估我今天的恢复状态。"
        h1, a1 = _msg("human", q), _msg("ai", "良好")
        h2, a2 = _msg("human", q), _msg("ai", "黄灯")
        h3 = _msg("human", q)
        tuples = [
            StubTuple(_ckpt("2026-05-13T08:00:00Z", [h1, a1, h2, a2, h3])),
            StubTuple(_ckpt("2026-05-12T09:00:00Z", [h1, a1, h2, a2])),
            StubTuple(_ckpt("2026-05-12T08:59:00Z", [h1, a1, h2])),
            StubTuple(_ckpt("2026-05-11T15:00:01Z", [h1, a1])),
            StubTuple(_ckpt("2026-05-11T15:00:00Z", [h1])),
        ]
        coach = _make_coach_with_checkpoints(tmp_chat_db, tuples)

        result = coach.get_history_with_ts("thread")

        assert [r["ts"] for r in result] == [
            "2026-05-11T15:00:00Z",  # h1
            "2026-05-11T15:00:01Z",  # a1
            "2026-05-12T08:59:00Z",  # h2 (same content as h1, different day)
            "2026-05-12T09:00:00Z",  # a2
            "2026-05-13T08:00:00Z",  # h3 (same content again)
        ]
        # All three human messages have identical content — sanity check
        # that we're testing the bug we said we are.
        assert result[0]["content"] == result[2]["content"] == result[4]["content"]


class TestGetHistoryWithTsEdgeCases:
    def test_empty_thread_returns_empty(self, tmp_chat_db):
        coach = _make_coach_with_checkpoints(tmp_chat_db, [])
        assert coach.get_history_with_ts("thread") == []

    def test_checkpointer_list_raises_returns_empty(self, tmp_chat_db):
        coach = AgenticCoach(db_path=tmp_chat_db, skip_api_probe=True)
        coach.checkpointer = MagicMock()
        coach.checkpointer.list = MagicMock(side_effect=RuntimeError("db gone"))
        # Same swallow-and-empty contract as get_history.
        assert coach.get_history_with_ts("thread") == []

    def test_missing_ts_falls_back_to_none(self, tmp_chat_db):
        """Legacy checkpoint without `ts` field: message gets ts=None,
        which the UI treats as 'no day anchor' (no divider triggered)."""
        h = _msg("human", "no ts")
        tuples = [StubTuple(_ckpt(None, [h]))]
        coach = _make_coach_with_checkpoints(tmp_chat_db, tuples)

        result = coach.get_history_with_ts("thread")
        assert result == [{"role": "human", "content": "no ts", "ts": None}]

    def test_content_as_block_list_is_flattened(self, tmp_chat_db):
        """Some providers return content as [{type:'text', text:...}].
        get_history_with_ts must flatten same as get_history does, so
        wire shape matches what /api/ai/history previously returned."""
        ai_blocks = MagicMock()
        ai_blocks.type = "ai"
        ai_blocks.content = [
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
            {"type": "tool_use", "id": "should_be_dropped"},  # no `text` key
        ]
        tuples = [StubTuple(_ckpt("2026-05-11T10:00:00Z", [ai_blocks]))]
        coach = _make_coach_with_checkpoints(tmp_chat_db, tuples)

        result = coach.get_history_with_ts("thread")
        assert result == [
            {
                "role": "ai",
                "content": "Hello world",
                "ts": "2026-05-11T10:00:00Z",
            }
        ]

    def test_messages_only_in_intermediate_checkpoint_are_dropped(self, tmp_chat_db):
        """Source of truth for the final list is the NEWEST checkpoint.
        If a message existed in an earlier checkpoint but is gone in the
        latest (e.g. agent rolled back state), it doesn't appear in
        output. ts_by_index might still have an entry but no message
        consumes it — that's correct."""
        h1 = _msg("human", "rolled back")
        h2 = _msg("human", "kept")
        tuples = [
            StubTuple(_ckpt("2026-05-11T11:00:00Z", [h2])),  # newest, no h1
            StubTuple(_ckpt("2026-05-11T10:00:00Z", [h1])),
        ]
        coach = _make_coach_with_checkpoints(tmp_chat_db, tuples)

        result = coach.get_history_with_ts("thread")
        assert len(result) == 1
        assert result[0]["content"] == "kept"


class TestGetHistoryWithTsWireShape:
    """The /api/ai/history endpoint forwards the helper's output 1:1.
    Verify the wire shape matches the frontend CoachMessage type:
    {role, content, ts}."""

    def test_keys_exactly_match_coach_message_shape(self, tmp_chat_db):
        h = _msg("human", "x")
        tuples = [StubTuple(_ckpt("2026-05-11T10:00:00Z", [h]))]
        coach = _make_coach_with_checkpoints(tmp_chat_db, tuples)

        result = coach.get_history_with_ts("thread")
        assert set(result[0].keys()) == {"role", "content", "ts"}
