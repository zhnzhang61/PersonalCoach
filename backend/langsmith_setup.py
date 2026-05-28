"""LangSmith tracing wiring (PR E).

LangChain's hosted observability UI. When the right env vars are
set, `langchain` auto-instruments every chain/agent/tool call with
tracing that flows to LangSmith — no code changes inside the agent
itself. This module just documents the contract, surfaces the
current status for the agent / operator to introspect, and logs
the wiring at startup so it's obvious whether traces are flowing.

### Why LangSmith on top of the local JSONL traces?

`trace_logger.py` writes one JSONL row per agent turn: user input,
final answer, prompt version, duration. It does NOT capture:
- per-tool calls (which `get_model` ran, what they returned)
- per-LLM-call token counts
- the message tree (system prompt → tool call → response → ...)

LangSmith captures all of that automatically because `langchain_core`
already emits structured events; LangSmith just listens. The JSONL
file stays the source-of-truth audit log (local, never leaves the
machine, copyright-safe); LangSmith is the observability *upgrade*
for debugging hard cases.

### Wiring — env var contract

LangSmith reads its config across TWO namespaces (`LANGSMITH_*` and
the legacy `LANGCHAIN_*`) with `LANGSMITH_*` taking precedence per
namespace position. Within each namespace it also accepts both a
canonical name and a `_V2` variant. Concretely
(`langsmith.utils.get_env_var` + `tracing_is_enabled`):

  tracing flag:  LANGSMITH_TRACING_V2  →  LANGCHAIN_TRACING_V2
              →  LANGSMITH_TRACING     →  LANGCHAIN_TRACING
  api key:       LANGSMITH_API_KEY     →  LANGCHAIN_API_KEY
  project:       LANGSMITH_PROJECT     →  LANGCHAIN_PROJECT
  endpoint:      LANGSMITH_ENDPOINT    →  LANGCHAIN_ENDPOINT

The flag must equal the literal lowercase string `"true"`. langsmith
itself does `var_result == "true"` (strict); aliases like
`"1"` / `"yes"` / `"on"` / `"True"` (capital T) are all REJECTED by
langsmith and would make our status disagree with reality. This
module mirrors that strictness so the status never lies.

The module-level boot log + `/api/admin/observability` endpoint
give the operator a one-glance check on whether tracing is actually
flowing (env var typos / forgotten exports / wrong-namespace exports
are the #1 silent-fail modes).
"""

from __future__ import annotations

import os
from typing import Any


# Two-namespace search order langsmith itself uses.
# `LANGSMITH_*` first (canonical), `LANGCHAIN_*` second (legacy
# from when LangChain shipped the tracer in-tree). Order matters:
# if both are set, the canonical wins, matching langsmith's lookup.
_NAMESPACES = ("LANGSMITH", "LANGCHAIN")

# Within each namespace, langsmith reads `_TRACING_V2` first, then
# falls back to `_TRACING`. We mirror that so the same export an
# operator already has working for langsmith is picked up here.
_FLAG_NAMES = ("TRACING_V2", "TRACING")

# Strict — matches langsmith.utils.tracing_is_enabled's
# `var_result == "true"`. See module docstring.
_ENABLED_STRING = "true"


def _lookup_namespaced(name: str) -> tuple[str, str | None]:
    """Search the (namespace × name) cross product langsmith does.

    Returns (resolved_value, source_var_name) — the source name is
    the actual env var that supplied the value (e.g.
    `"LANGCHAIN_TRACING_V2"`) so the status payload can tell the
    operator whether they're on the canonical or legacy namespace.
    Returns ("", None) when no namespace produced a non-empty value.
    """
    for ns in _NAMESPACES:
        var = f"{ns}_{name}"
        v = (os.environ.get(var) or "").strip()
        if v:
            return v, var
    return "", None


def _resolve_tracing_flag() -> tuple[str, str | None]:
    """The `_V2` variant wins over the non-`_V2` one, mirroring
    `langsmith.utils.tracing_is_enabled`'s `get_env_var("TRACING_V2",
    default=get_env_var("TRACING", default=""))` nested fallback."""
    for fname in _FLAG_NAMES:
        v, src = _lookup_namespaced(fname)
        if v:
            return v, src
    return "", None


def langsmith_tracing_enabled() -> bool:
    """Whether LangSmith tracing is actively flowing.

    Requires BOTH a strict-truthy flag (lowercase `"true"`) AND an
    API key. Either alone isn't enough: the key without the flag
    means langsmith doesn't auto-trace at all; the flag without a
    key means langsmith tries to trace and every send 401s.

    Mirrors `langsmith.utils.tracing_is_enabled` so this module's
    answer never contradicts reality.
    """
    flag, _ = _resolve_tracing_flag()
    api_key, _ = _lookup_namespaced("API_KEY")
    return flag == _ENABLED_STRING and bool(api_key)


def langsmith_status() -> dict[str, Any]:
    """Structured status payload for the observability debug endpoint.

    Reports presence/absence of each env var WITHOUT echoing the
    API key value (that's a secret). The agent or a human operator
    reads this to confirm whether their wiring is live without
    having to re-export env vars and restart the server.

    `tracing_flag_source` / `api_key_source` reveal WHICH env var
    name carried the value (e.g. `"LANGCHAIN_TRACING_V2"` vs
    `"LANGSMITH_TRACING"`) so an operator who set the legacy name
    can confirm it's the one being picked up, not silently masked
    by an empty canonical var.
    """
    flag_value, flag_source = _resolve_tracing_flag()
    api_key_value, api_key_source = _lookup_namespaced("API_KEY")
    project_value, _ = _lookup_namespaced("PROJECT")
    endpoint_value, _ = _lookup_namespaced("ENDPOINT")
    return {
        "tracing_enabled": (
            flag_value == _ENABLED_STRING and bool(api_key_value)
        ),
        # The four env-var states the operator might have:
        #   flag truthy + key set  → flowing ✓
        #   flag truthy + key MISS → 401s silently, NOT flowing
        #   flag wrong  + key set  → not flowing (`flag=="1"` etc.
        #                            — langsmith rejects)
        #   flag empty  + key set  → not flowing (clean off state)
        "tracing_flag": flag_value,
        "tracing_flag_source": flag_source,
        "api_key_set": bool(api_key_value),
        "api_key_source": api_key_source,
        # Project defaults to langsmith's own "default" when unset.
        "project": project_value or "default",
        # Endpoint defaults to the hosted SaaS; self-hosted LangSmith
        # deployments override it.
        "endpoint": endpoint_value or "https://api.smith.langchain.com",
    }


def startup_log_line() -> str:
    """One-line summary suitable for FastAPI startup hook output.

    Four states the operator should be able to grep for at a glance:
      "LangSmith tracing: ON (project=personalcoach, source=LANGSMITH_TRACING)"
      "LangSmith tracing: OFF (no LANGSMITH_TRACING / LANGCHAIN_TRACING set)"
      "LangSmith tracing: MISCONFIGURED — flag set but no API key"
      "LangSmith tracing: MISCONFIGURED — flag is 'X' but langsmith requires lowercase 'true'"
    """
    status = langsmith_status()
    if status["tracing_enabled"]:
        return (
            f"LangSmith tracing: ON "
            f"(project={status['project']}, "
            f"source={status['tracing_flag_source']}, "
            f"endpoint={status['endpoint']})"
        )
    flag = status["tracing_flag"]
    if flag == _ENABLED_STRING and not status["api_key_set"]:
        # Flag is correctly "true" but no key in either namespace.
        return (
            "LangSmith tracing: MISCONFIGURED — tracing flag is set "
            "but no API key found in LANGSMITH_API_KEY or "
            "LANGCHAIN_API_KEY. Spans will be dropped silently."
        )
    if flag and flag != _ENABLED_STRING:
        # Flag has a value but it's not lowercase "true". The
        # operator's intent was probably "on" but langsmith won't
        # accept `1` / `yes` / `True` / etc.
        return (
            f"LangSmith tracing: MISCONFIGURED — flag is {flag!r}, but "
            "langsmith requires the literal lowercase string 'true'. "
            "Tracing is OFF."
        )
    return (
        "LangSmith tracing: OFF "
        "(no LANGSMITH_TRACING / LANGCHAIN_TRACING set)"
    )
