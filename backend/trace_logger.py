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
    migration."""

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
    USER_INPUT_TRUNC = 500
    FINAL_ANSWER_TRUNC = 1000
    ERROR_TRUNC = 500

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
