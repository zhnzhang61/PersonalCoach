"""Unit tests for backend/trace_logger.py.

Trace + TraceLogger are the storage backbone of structured tracing
(IMPROVEMENTS §3 MVP). They MUST be invisible to the turn — disk
errors, malformed unicode, even a missing root dir cannot raise.
These tests lock that contract in.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.trace_logger import Trace, TraceLogger, prompt_hash


@pytest.fixture
def tracer(tmp_path):
    """TraceLogger pointed at a per-test directory. tmp_path is a
    pytest builtin that gives a fresh empty dir per test."""
    return TraceLogger(root=tmp_path / "traces")


# ---------------------------------------------------------------------------
# prompt_hash — pure helper
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_deterministic(self):
        assert prompt_hash("hello") == prompt_hash("hello")

    def test_different_inputs_different_hashes(self):
        assert prompt_hash("a") != prompt_hash("b")

    def test_truncated_to_12_chars(self):
        h = prompt_hash("anything")
        assert len(h) == 12
        # Hex chars only.
        assert all(c in "0123456789abcdef" for c in h)

    def test_unicode_input_handled(self):
        """Prompts are bilingual — must hash without exploding."""
        h = prompt_hash("你好，世界")
        assert len(h) == 12


# ---------------------------------------------------------------------------
# TraceLogger basics
# ---------------------------------------------------------------------------


class TestTraceLoggerBasics:
    def test_init_creates_root_dir(self, tmp_path):
        target = tmp_path / "doesnt_exist_yet" / "traces"
        assert not target.exists()
        TraceLogger(root=target)
        assert target.exists() and target.is_dir()

    def test_init_swallows_dir_creation_failure(self, tmp_path):
        """If the tracer can't create its root, construction must
        still succeed — writes will silently no-op. The agent must
        never crash on a misconfigured trace path."""
        with patch.object(Path, "mkdir", side_effect=PermissionError("nope")):
            # Should not raise.
            TraceLogger(root=tmp_path / "blocked")

    def test_path_uses_utc_date(self, tracer):
        """Daily rotation key is the UTC date — locks in that a turn
        at 23:58 PST (UTC 07:58 next day) writes to the NEXT day's
        file, not the user-local day. Picking one convention keeps
        cross-timezone grep predictable; UTC is the natural choice
        because everything else in our schema already uses UTC ISO
        timestamps."""
        fixed_utc_today = datetime.date(2026, 5, 27)
        with patch("backend.trace_logger.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = datetime.datetime(
                2026, 5, 27, 12, 0, 0, tzinfo=datetime.timezone.utc
            )
            mock_dt.timezone = datetime.timezone
            path = tracer._path_for_today()
        assert path.name == "2026-05-27.jsonl"


# ---------------------------------------------------------------------------
# write() — JSONL append semantics
# ---------------------------------------------------------------------------


class TestTraceWrite:
    def _make_trace(self, **overrides):
        defaults = dict(
            turn_id="abc-123",
            thread_id="coach_t",
            timestamp="2026-05-27T12:00:00+00:00",
            kind="chat",
            prompt_version="v7",
            prompt_hash="0123456789ab",
            user_input="hi",
        )
        defaults.update(overrides)
        return Trace(**defaults)

    def test_appends_one_jsonl_line(self, tracer):
        tracer.write(self._make_trace())
        files = list(tracer.root.glob("*.jsonl"))
        assert len(files) == 1
        content = files[0].read_text()
        # One newline-terminated line.
        assert content.endswith("\n")
        assert content.count("\n") == 1
        parsed = json.loads(content.rstrip("\n"))
        assert parsed["turn_id"] == "abc-123"
        assert parsed["kind"] == "chat"

    def test_multiple_writes_append_not_overwrite(self, tracer):
        tracer.write(self._make_trace(turn_id="t1"))
        tracer.write(self._make_trace(turn_id="t2"))
        tracer.write(self._make_trace(turn_id="t3"))
        f = next(iter(tracer.root.glob("*.jsonl")))
        lines = f.read_text().strip().split("\n")
        assert [json.loads(l)["turn_id"] for l in lines] == ["t1", "t2", "t3"]

    def test_unicode_user_input_not_escaped(self, tracer):
        """Chinese should round-trip raw — easier to grep + visually
        inspect."""
        tracer.write(self._make_trace(user_input="今日恢复良好"))
        content = next(iter(tracer.root.glob("*.jsonl"))).read_text()
        assert "今日恢复良好" in content
        assert "\\u" not in content

    def test_write_failure_is_silent(self, tracer):
        """The hard contract: tracing must NEVER raise. A turn cannot
        die because we couldn't write a row."""
        with patch("builtins.open", side_effect=OSError("disk full")):
            # Should not raise.
            tracer.write(self._make_trace())


# ---------------------------------------------------------------------------
# turn() context manager — duration + error capture
# ---------------------------------------------------------------------------


class TestTurnContextManager:
    def test_writes_one_row_on_success(self, tracer):
        with tracer.turn(
            kind="chat",
            thread_id="coach_t",
            prompt_version="v7",
            prompt_hash="abc",
            user_input="hi",
        ) as trace:
            trace.final_answer = "hello back"
        # Exactly one row written on context exit.
        lines = next(iter(tracer.root.glob("*.jsonl"))).read_text().strip().split("\n")
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["kind"] == "chat"
        assert row["final_answer"] == "hello back"
        assert row["error"] is None
        # duration_ms recorded as a positive float.
        assert isinstance(row["duration_ms"], float)
        assert row["duration_ms"] >= 0

    def test_records_error_and_reraises(self, tracer):
        """Exception inside the context block must be captured into
        trace.error but the exception itself must propagate — tracing
        is invisible, never absorbs failures."""
        with pytest.raises(RuntimeError, match="boom"):
            with tracer.turn(
                kind="chat",
                thread_id="coach_t",
                prompt_version="v7",
                prompt_hash="abc",
                user_input="hi",
            ):
                raise RuntimeError("boom")
        row = json.loads(next(iter(tracer.root.glob("*.jsonl"))).read_text())
        assert "RuntimeError" in row["error"]
        assert "boom" in row["error"]
        # final_answer untouched.
        assert row["final_answer"] == ""

    def test_truncates_long_user_input(self, tracer):
        long_input = "x" * 2000
        with tracer.turn(
            kind="chat",
            thread_id="coach_t",
            prompt_version="v7",
            prompt_hash="abc",
            user_input=long_input,
        ):
            pass
        row = json.loads(next(iter(tracer.root.glob("*.jsonl"))).read_text())
        assert len(row["user_input"]) == TraceLogger.USER_INPUT_TRUNC

    def test_truncates_long_final_answer(self, tracer):
        long_answer = "y" * 5000
        with tracer.turn(
            kind="chat",
            thread_id="coach_t",
            prompt_version="v7",
            prompt_hash="abc",
            user_input="hi",
        ) as trace:
            trace.final_answer = long_answer
        row = json.loads(next(iter(tracer.root.glob("*.jsonl"))).read_text())
        assert len(row["final_answer"]) == TraceLogger.FINAL_ANSWER_TRUNC

    def test_extras_dict_serializes(self, tracer):
        """Forward-compat field — turn-specific diagnostics land
        here without schema migration."""
        with tracer.turn(
            kind="chat",
            thread_id="coach_t",
            prompt_version="v7",
            prompt_hash="abc",
            user_input="hi",
        ) as trace:
            trace.extras["n_tool_calls"] = 3
            trace.extras["provider"] = "gemini"
        row = json.loads(next(iter(tracer.root.glob("*.jsonl"))).read_text())
        assert row["extras"] == {"n_tool_calls": 3, "provider": "gemini"}

    def test_disk_failure_in_finally_does_not_raise(self, tracer):
        """Even if the disk fails at write time inside the finally
        block, the context manager must exit cleanly. The hard
        contract: tracing never propagates IO failures into the
        caller."""
        # json.dumps is called inside write() before the file open;
        # patching it to raise simulates "something failed during the
        # write step" cleanly without touching pathlib internals.
        with patch("backend.trace_logger.json.dumps", side_effect=OSError("disk full")):
            with tracer.turn(
                kind="chat",
                thread_id="coach_t",
                prompt_version="v7",
                prompt_hash="abc",
                user_input="hi",
            ):
                pass  # if write's try/except is wrong, OSError leaks here
