"""Structured tracing for agent turns.

One JSONL row per agent turn, written to `data/traces/YYYY-MM-DD.jsonl`
(daily rotation). Used to debug "why did the agent do that?" without
having to repro from chat history — every turn carries the prompt
version that produced it, the user input, the final answer, latency,
and any error.

Scope of this MVP (IMPROVEMENTS §3 first half):
- Captures: turn_id, thread_id, timestamp, kind, prompt_version,
  prompt_hash, user_input, final_answer, duration_ms, error.
- Does NOT yet capture: per-tool latency, LLM token counts. Those
  require digging into LangGraph's internal message stream + provider
  response objects; deferred to v2 of this trace shape.

Storage is append-only JSONL — easy to grep, easy to rotate (weekly
prune script if disk fills). Traces are LOCAL ONLY: contain user
inputs verbatim, so they go in .gitignore + the directory is created
on first use. They never ship to a remote — LangSmith wiring (PR E)
is a separate opt-in layer on top.

Failure mode: tracing must NEVER break a turn. Disk full, permission
denied, malformed unicode — all swallowed silently with the trace row
lost. Defensive `try: ... except Exception: pass` around every write.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Trace:
    """One agent turn. Field set is deliberately small for MVP — the
    `extras` dict catches forward-compat additions without schema
    migration.

    `tool_calls` carries one row per tool invocation observed during the
    turn (via LangChain callbacks for the ReAct loop, or appended
    manually for prefetched tools). Each row: `{name, args, result|error,
    duration_ms, prefetched?}`. args + result are truncated by
    TraceLogger constants. Empty when the turn made no tool calls.
    """

    turn_id: str
    thread_id: str
    timestamp: str          # ISO 8601, UTC, with tz suffix
    kind: str               # "chat" | "chat_stream" | "action.<name>" | "consolidate"
    prompt_version: str     # e.g. "v7" — bumped on system-prompt edits
    prompt_hash: str        # short sha1 of the prompt actually used
    user_input: str         # truncated to 500 chars
    final_answer: str = ""  # truncated to 1000 chars
    duration_ms: float = 0.0
    error: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


def prompt_hash(prompt: str) -> str:
    """First 12 hex chars of sha1(prompt). Short enough to grep, wide
    enough that hash collisions are not a worry for our ~100 prompt
    versions over the project lifetime."""
    return hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]


class TraceLogger:
    """JSONL trace writer. One file per UTC day.

    Construction is cheap (creates directory if missing); use a single
    instance per AgenticCoach / MemoryOS to avoid stat'ing the
    directory on every turn.
    """

    DEFAULT_ROOT = Path("data/traces")
    PAYLOAD_SUBDIR = "payloads"
    USER_INPUT_TRUNC = 500
    FINAL_ANSWER_TRUNC = 1000
    ERROR_TRUNC = 500
    # Per-tool-call truncation. Some tools return many KB (run telemetry,
    # route profile, big topic lists); keep the JSONL line lean so daily
    # files stay greppable. When a payload exceeds the limit, the full
    # bytes go to a content-addressed file under
    # `data/traces/<PAYLOAD_SUBDIR>/<sha>.txt` and the trace entry gains
    # `<field>_sha` + `<field>_len` for lookup. See `record_payload`.
    TOOL_ARGS_TRUNC = 500
    TOOL_RESULT_TRUNC = 500

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else self.DEFAULT_ROOT
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception:
            # If we can't even create the dir, every write() below
            # will silently fail too — but don't block construction.
            pass

    def _path_for_today(self) -> Path:
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        return self.root / f"{today}.jsonl"

    def write(self, trace: Trace) -> None:
        """Append one trace row. Swallows any write failure so a
        broken trace path can never take down a chat turn."""
        try:
            line = json.dumps(asdict(trace), ensure_ascii=False)
            with self._path_for_today().open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            # Tracing failure is silent by contract.
            pass

    @contextmanager
    def turn(
        self,
        *,
        kind: str,
        thread_id: str,
        prompt_version: str,
        prompt_hash: str,
        user_input: str = "",
    ):
        """Context manager that accumulates a Trace and writes on
        exit (success or failure).

        Usage:
            with tracer.turn(kind="chat", thread_id=tid, ...) as trace:
                result = await self._agent.ainvoke(...)
                trace.final_answer = result

        Caller can also mutate `trace.extras[...]` mid-turn to attach
        arbitrary diagnostic fields. On exception, error is captured
        and the exception re-raised — tracing must be transparent.
        """
        trace = Trace(
            turn_id=str(uuid.uuid4()),
            thread_id=thread_id,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            kind=kind,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            user_input=(user_input or "")[: self.USER_INPUT_TRUNC],
        )
        start = time.perf_counter()
        try:
            yield trace
        except Exception as e:
            trace.error = f"{type(e).__name__}: {e}"[: self.ERROR_TRUNC]
            raise
        finally:
            trace.duration_ms = round((time.perf_counter() - start) * 1000, 2)
            # Truncate final_answer here so callers can set it without
            # worrying about size budget.
            trace.final_answer = (trace.final_answer or "")[
                : self.FINAL_ANSWER_TRUNC
            ]
            self.write(trace)


# ---------------------------------------------------------------------------
# Tool-call capture — LangChain callback handler
# ---------------------------------------------------------------------------
#
# Captures `on_tool_start` / `on_tool_end` / `on_tool_error` from LangGraph's
# ReAct loop into a list (typically `trace.tool_calls`). Works for both
# `ainvoke()` and `astream_events()` paths — just pass the handler via
# `config={"callbacks": [handler]}`.
#
# Prefetched MCP calls (parallel asyncio.gather in _action_turn) bypass the
# LangChain loop and don't fire callbacks; the agent appends those to the
# same list manually with `prefetched=True`, so one trace row tells the full
# story regardless of where a tool was invoked.
#
# Why a class lives in this module: the capture format is part of the trace
# schema. Keeping it co-located with `Trace` means a schema change has one
# home — no dance between trace_logger + a separate handlers module.


def truncate_for_trace(value: object, limit: int) -> str:
    """Stringify + truncate to fit a trace field. Suffix with `…(+N more)`
    when cut so a debugger can tell at a glance whether they're looking at
    the full payload. Primitive — most callers want `record_payload` instead,
    which also caches the full payload to disk and emits a sha for lookup."""
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return s
    return s[:limit] + f"…(+{len(s) - limit} more)"


def payload_sha(s: str) -> str:
    """16 hex chars of sha1 — the lookup key into the payload cache. 64-bit
    collision space is comfortably more than the number of distinct tool
    responses we'll ever store; short enough to grep / paste."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def _payload_dir(root: str | Path | None) -> Path:
    base = Path(root) if root is not None else TraceLogger.DEFAULT_ROOT
    return base / TraceLogger.PAYLOAD_SUBDIR


def record_payload(
    value: object,
    limit: int,
    field: str,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Stringify + truncate `value` to `limit` chars and return a dict ready
    to splat into a trace entry under `field` (e.g. `"result"`, `"args"`,
    `"error"`):

    * Under the limit → `{field: short_str}`. Single key, same shape as
      before — readers that don't care about overflow are unaffected.
    * Over the limit → `{field: <truncated>, f"{field}_sha": ..., f"{field}_len": ...}`,
      and the FULL stringified payload is written to
      `<root>/<PAYLOAD_SUBDIR>/<sha>.txt` (idempotent, silent on failure).

    Recover the full payload with `load_payload(sha, root=...)` or, equivalently,
    `cat data/traces/payloads/<sha>.txt`. The sha + len fields are useful even if
    the cache file is missing — the reader still knows "this was truncated to N
    chars, look it up if you cared."

    Why splat-able dict instead of returning a tuple: callers (handler + agent)
    use `entry.update(record_payload(...))`, which keeps construction flat and
    makes the no-overflow case a single key.
    """
    s = "" if value is None else str(value)
    if len(s) <= limit:
        return {field: s}
    short = s[:limit] + f"…(+{len(s) - limit} more)"
    sha = payload_sha(s)
    cache_dir = _payload_dir(root)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{sha}.txt"
        # Content-addressed → idempotent. Same content across turns shares
        # one file (cheap dedup). Skip if already present.
        if not path.exists():
            path.write_text(s, encoding="utf-8")
    except Exception:
        # Cache write failure is silent — same contract as TraceLogger.write.
        # The trace row is still useful (sha + len describe what was cut).
        pass
    return {field: short, f"{field}_sha": sha, f"{field}_len": len(s)}


def load_payload(sha: str, *, root: str | Path | None = None) -> str | None:
    """Read back a cached payload by sha. Returns None if the file is
    missing (cache pruned, write originally failed, wrong sha) — callers
    should treat absent payloads as "not recoverable" and fall back to the
    truncated string in the trace row."""
    path = _payload_dir(root) / f"{sha}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except Exception:
        return None


class ToolCallCaptureHandler:
    """LangChain-compatible callback handler. Appends one dict per
    completed tool invocation to `sink`.

    Designed for the single-user single-coach case — `_inflight` is an
    in-process dict keyed by run_id, no locking. Multiple tools can be in
    flight concurrently (prefetch fan-out, or the rare case where the ReAct
    model emits parallel tool calls) and they're correlated by `run_id` per
    LangChain's contract.

    Override `on_tool_start` / `on_tool_end` / `on_tool_error` only —
    LangChain dispatches by method name; absent methods are silently
    no-ops. We deliberately do NOT subclass `BaseCallbackHandler` so this
    module stays pure-stdlib + langchain-version-agnostic; LangChain's
    callback dispatcher duck-types on method names.
    """

    # LangChain's callback dispatcher reads these two attributes (set
    # on BaseCallbackHandler) to decide whether the handler should be
    # called from sync vs async paths. We say yes to both so the same
    # handler works under ainvoke (sync-from-async) and astream_events.
    ignore_agent = False
    ignore_chain = True   # we only care about tool starts/ends
    ignore_llm = True
    ignore_retriever = True
    ignore_chat_model = True
    raise_error = False
    run_inline = False

    def __init__(
        self,
        sink: list[dict],
        *,
        args_trunc: int = TraceLogger.TOOL_ARGS_TRUNC,
        result_trunc: int = TraceLogger.TOOL_RESULT_TRUNC,
        payload_root: str | Path | None = None,
    ):
        self.sink = sink
        self._inflight: dict[Any, dict] = {}
        self._args_trunc = args_trunc
        self._result_trunc = result_trunc
        # Where the overflow cache lives. None → TraceLogger.DEFAULT_ROOT
        # (the production path). Tests pass a tmp_path to keep the
        # filesystem clean.
        self._payload_root = payload_root

    # NOTE: LangChain passes serialized as dict OR None depending on the
    # tool source (a langgraph MCP-wrapped tool sometimes hands None).
    # Defensive .get() chains throughout.

    def on_tool_start(
        self,
        serialized: dict | None,
        input_str: str,
        *,
        run_id: Any,
        **_: Any,
    ) -> None:
        name = (serialized or {}).get("name") if serialized else None
        if not name:
            name = "unknown"
        entry = {
            "name": name,
            **record_payload(
                input_str, self._args_trunc, "args", root=self._payload_root,
            ),
            "_start": time.perf_counter(),
        }
        self._inflight[run_id] = entry

    def on_tool_end(self, output: Any, *, run_id: Any, **_: Any) -> None:
        entry = self._inflight.pop(run_id, None)
        if entry is None:
            return
        t0 = entry.pop("_start", None)
        entry.update(
            record_payload(
                output, self._result_trunc, "result",
                root=self._payload_root,
            )
        )
        if t0 is not None:
            entry["duration_ms"] = round(
                (time.perf_counter() - t0) * 1000, 2
            )
        self.sink.append(entry)

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: Any,
        **_: Any,
    ) -> None:
        entry = self._inflight.pop(run_id, None)
        if entry is None:
            return
        t0 = entry.pop("_start", None)
        entry.update(
            record_payload(
                repr(error), self._result_trunc, "error",
                root=self._payload_root,
            )
        )
        if t0 is not None:
            entry["duration_ms"] = round(
                (time.perf_counter() - t0) * 1000, 2
            )
        self.sink.append(entry)
