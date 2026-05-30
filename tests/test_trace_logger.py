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


# ---------------------------------------------------------------------------
# truncate_for_trace + ToolCallCaptureHandler + tool_calls field
# ---------------------------------------------------------------------------


class TestTruncateForTrace:
    def test_short_passes_through(self):
        from backend.trace_logger import truncate_for_trace
        assert truncate_for_trace("hi", 10) == "hi"

    def test_at_limit_passes(self):
        from backend.trace_logger import truncate_for_trace
        assert truncate_for_trace("x" * 10, 10) == "x" * 10

    def test_over_limit_gets_suffix(self):
        from backend.trace_logger import truncate_for_trace
        out = truncate_for_trace("x" * 25, 10)
        assert out.startswith("x" * 10)
        assert "+15 more" in out  # 25 - 10 = 15 dropped

    def test_none_becomes_empty(self):
        from backend.trace_logger import truncate_for_trace
        assert truncate_for_trace(None, 10) == ""

    def test_non_string_is_stringified(self):
        from backend.trace_logger import truncate_for_trace
        assert truncate_for_trace({"a": 1}, 100) == "{'a': 1}"


class TestToolCallCaptureHandler:
    def _make(self, **overrides):
        import uuid
        from backend.trace_logger import ToolCallCaptureHandler
        sink: list[dict] = []
        h = ToolCallCaptureHandler(sink, **overrides)
        return sink, h, uuid.uuid4()

    def test_start_then_end_records_one_entry(self):
        sink, h, rid = self._make()
        h.on_tool_start({"name": "get_readiness"}, '{"date":"2026-05-29"}', run_id=rid)
        # inflight before end, sink empty
        assert sink == []
        h.on_tool_end({"score": 75}, run_id=rid)
        assert len(sink) == 1
        e = sink[0]
        assert e["name"] == "get_readiness"
        assert e["args"] == '{"date":"2026-05-29"}'
        assert "score" in e["result"]
        assert "duration_ms" in e and e["duration_ms"] >= 0

    def test_end_without_matching_start_is_noop(self):
        """Defensive: a stray on_tool_end (e.g. handler attached
        mid-stream) must not append a half-empty row or KeyError."""
        import uuid
        sink, h, _ = self._make()
        h.on_tool_end("late", run_id=uuid.uuid4())
        assert sink == []

    def test_error_records_error_not_result(self):
        sink, h, rid = self._make()
        h.on_tool_start({"name": "get_run_detail"}, '{"activity_id":1}', run_id=rid)
        h.on_tool_error(RuntimeError("404"), run_id=rid)
        assert len(sink) == 1
        assert "error" in sink[0]
        assert "result" not in sink[0]
        assert "404" in sink[0]["error"]

    def test_serialized_none_falls_back_to_unknown(self):
        sink, h, rid = self._make()
        h.on_tool_start(None, "x", run_id=rid)
        h.on_tool_end("y", run_id=rid)
        assert sink[0]["name"] == "unknown"

    def test_args_and_result_get_truncated(self):
        sink, h, rid = self._make(args_trunc=5, result_trunc=5)
        h.on_tool_start({"name": "telemetry"}, "x" * 20, run_id=rid)
        h.on_tool_end("y" * 20, run_id=rid)
        assert "+15 more" in sink[0]["args"]
        assert "+15 more" in sink[0]["result"]

    def test_concurrent_runs_correlated_by_run_id(self):
        """Two tools in flight at the same time — common when prefetch
        fans out or the model emits parallel tool calls. The handler
        must correlate end→start by run_id, not by order."""
        import uuid
        from backend.trace_logger import ToolCallCaptureHandler
        sink: list[dict] = []
        h = ToolCallCaptureHandler(sink)
        rid_a = uuid.uuid4()
        rid_b = uuid.uuid4()
        h.on_tool_start({"name": "A"}, "a-args", run_id=rid_a)
        h.on_tool_start({"name": "B"}, "b-args", run_id=rid_b)
        # B ends first (parallel order)
        h.on_tool_end("b-result", run_id=rid_b)
        h.on_tool_end("a-result", run_id=rid_a)
        by_name = {e["name"]: e for e in sink}
        assert by_name["A"]["args"] == "a-args"
        assert by_name["A"]["result"] == "a-result"
        assert by_name["B"]["args"] == "b-args"
        assert by_name["B"]["result"] == "b-result"


class TestTraceToolCallsRoundTrip:
    def test_tool_calls_default_empty(self):
        from backend.trace_logger import Trace
        t = Trace(
            turn_id="x", thread_id="y", timestamp="z",
            kind="chat", prompt_version="v10", prompt_hash="h",
            user_input="",
        )
        assert t.tool_calls == []

    def test_tool_calls_round_trip_through_jsonl(self, tmp_path):
        """Writing a Trace with non-empty tool_calls and reading the
        JSONL line back yields the same list — the schema add is real,
        not just in-memory."""
        import json
        from backend.trace_logger import Trace, TraceLogger
        tl = TraceLogger(root=tmp_path)
        with tl.turn(
            kind="chat", thread_id="t1",
            prompt_version="v10", prompt_hash="h",
            user_input="hi",
        ) as trace:
            trace.tool_calls.append({
                "name": "get_recent_checkins",
                "args": '{"days":7}',
                "result": "[{...}]",
                "duration_ms": 12.3,
            })
            trace.final_answer = "ok"
        files = list(tmp_path.glob("*.jsonl"))
        assert len(files) == 1
        row = json.loads(files[0].read_text().strip())
        assert row["tool_calls"][0]["name"] == "get_recent_checkins"
        assert row["tool_calls"][0]["duration_ms"] == 12.3
