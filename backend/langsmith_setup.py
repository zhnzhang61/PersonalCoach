"""LangSmith tracing wiring (PR E).

LangChain's hosted observability UI. When the right env vars are set,
`langchain` auto-instruments every chain/agent/tool call with tracing
that flows to LangSmith — no code changes inside the agent itself.
This module just documents the contract, surfaces the current status
for the agent / operator to introspect, and logs the wiring at
startup so it's obvious whether traces are flowing.

### Why LangSmith on top of the local JSONL traces?

`trace_logger.py` writes one JSONL row per agent turn: user input,
final answer, prompt version, duration. It does NOT capture:
- per-tool calls (which `get_model` ran, what they returned)
- per-LLM-call token counts
- the message tree (system prompt → tool call → response → ...)

LangSmith captures all of that automatically because `langchain_core`
already emits structured events; LangSmith just listens. The JSONL
file stays as the source-of-truth audit log (local, never leaves the
machine, copyright-safe); LangSmith is the observability *upgrade*
for debugging hard cases.

### Wiring

Three env vars (set in your shell, .envrc, or wherever):

  LANGSMITH_TRACING=true            # the master switch
  LANGSMITH_API_KEY=ls__...         # auth, from https://smith.langchain.com
  LANGSMITH_PROJECT=personalcoach   # optional, defaults to "default"

`langchain_core` reads these at import time. With them set, every
`create_react_agent` invocation, every tool call, every LLM call
sends a span to LangSmith. With them missing, langchain runs locally
with zero overhead — JSONL traces still work.

The module-level boot log + `/api/debug/observability` endpoint give
the operator a one-glance check on whether tracing is actually
flowing (env var typos / forgotten exports are the #1 silent-fail
mode).
"""

from __future__ import annotations

import os
from typing import Any


# Env vars langchain checks. Centralized here so anywhere in the
# codebase that wants to ask "is tracing on?" doesn't hard-code the
# names.
_TRACING_FLAG_VAR = "LANGSMITH_TRACING"
_API_KEY_VAR = "LANGSMITH_API_KEY"
_PROJECT_VAR = "LANGSMITH_PROJECT"
_ENDPOINT_VAR = "LANGSMITH_ENDPOINT"

# Values that count as "enabled" for the master switch. langchain
# itself accepts "true" / "1" — match that so we don't diverge.
_TRUTHY = {"true", "1", "yes", "on"}


def langsmith_tracing_enabled() -> bool:
    """Whether LangSmith tracing is actively flowing.

    Requires BOTH the master switch AND an API key. Either alone
    isn't enough: API key without `LANGSMITH_TRACING=true` means
    langchain still doesn't auto-trace; the switch without a key
    means langchain tries to trace but every send 401s.
    """
    flag = (os.environ.get(_TRACING_FLAG_VAR) or "").strip().lower()
    api_key = (os.environ.get(_API_KEY_VAR) or "").strip()
    return flag in _TRUTHY and bool(api_key)


def langsmith_status() -> dict[str, Any]:
    """Structured status payload for the observability debug endpoint.

    Reports presence/absence of each env var WITHOUT echoing the API
    key value (that's a secret). The agent or a human operator reads
    this to confirm whether their wiring is live without having to
    re-export env vars and restart the server."""
    api_key_set = bool((os.environ.get(_API_KEY_VAR) or "").strip())
    return {
        "tracing_enabled": langsmith_tracing_enabled(),
        # The four env-var states the operator might have:
        #   tracing_flag on  + key set  → flowing ✓
        #   tracing_flag on  + key MISS → 401s silently, NOT flowing
        #   tracing_flag off + key set  → not flowing (key wasted)
        #   tracing_flag off + key MISS → not flowing (clean off state)
        "tracing_flag": (os.environ.get(_TRACING_FLAG_VAR) or "").strip().lower(),
        "api_key_set": api_key_set,
        "project": os.environ.get(_PROJECT_VAR) or "default",
        # Endpoint defaults to the hosted SaaS; self-hosted LangSmith
        # deployments override it. Surfacing it here makes the
        # self-hosted case debuggable without grep'ing env vars.
        "endpoint": (
            os.environ.get(_ENDPOINT_VAR)
            or "https://api.smith.langchain.com"
        ),
    }


def startup_log_line() -> str:
    """One-line summary suitable for uvicorn / app startup logs.

    Three states the operator should be able to grep for at a glance:
      "LangSmith tracing: ON (project=personalcoach)"
      "LangSmith tracing: OFF (LANGSMITH_TRACING not set)"
      "LangSmith tracing: MISCONFIGURED (flag set but no API key)"
    """
    status = langsmith_status()
    if status["tracing_enabled"]:
        return (
            f"LangSmith tracing: ON "
            f"(project={status['project']}, endpoint={status['endpoint']})"
        )
    flag = status["tracing_flag"]
    if flag in _TRUTHY and not status["api_key_set"]:
        return (
            "LangSmith tracing: MISCONFIGURED — "
            f"{_TRACING_FLAG_VAR} is set but {_API_KEY_VAR} is missing. "
            "Spans will be dropped silently."
        )
    return f"LangSmith tracing: OFF ({_TRACING_FLAG_VAR} not set)"
