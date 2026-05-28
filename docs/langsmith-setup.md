# LangSmith tracing setup (PR E)

LangSmith is LangChain's hosted observability UI. With three env
vars set, every agent turn, tool call, and LLM round-trip flows to
their UI where you can filter by prompt version, query latency,
inspect the message tree, etc.

This is **opt-in**. Without it, the existing JSONL trace at
`data/traces/YYYY-MM-DD.jsonl` still records prompt version +
user input + final answer + duration per turn. LangSmith captures
the things JSONL doesn't:

- per-tool call inputs/outputs (which `get_model` ran with which
  key, what came back)
- LLM token counts + latency breakdown
- the full message tree (system prompt → tool call → response → ...)
- cross-version diffs (prompt v7 vs v8 on the same query)

The native JSONL trace stays the source-of-truth audit log (local,
copyright-safe, never leaves the machine). LangSmith is the
debugging layer on top.

## 1. Sign up (free tier)

1. Go to <https://smith.langchain.com>
2. Create an account (GitHub / Google SSO is fine)
3. Settings → API Keys → **Create new API key**
4. Copy it — starts with `lsv2_` or `ls__`. Treat as a secret.

Free tier: 5,000 traces / month. A single-user dev usage produces
maybe 50–200 traces/day — comfortably within the limit.

## 2. Set env vars

Add to your shell rc (`~/.zshrc` / `~/.bashrc`) or `.envrc` (if
you use direnv):

```bash
export LANGSMITH_TRACING=true                  # must be lowercase "true"
export LANGSMITH_API_KEY=lsv2_pt_...           # from step 1
export LANGSMITH_PROJECT=personalcoach         # optional, organizes traces
```

**Important — `LANGSMITH_TRACING` must be the literal lowercase
string `"true"`.** langsmith does a strict `var_result == "true"`
check; values like `1`, `yes`, `on`, or `True` (capital T) are
REJECTED. The startup log line surfaces this misconfiguration
explicitly so you don't waste time wondering why traces aren't
flowing.

**Legacy `LANGCHAIN_*` names also work** — langsmith reads
`LANGCHAIN_TRACING_V2` / `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT`
as fallbacks. So if you followed an older LangChain tutorial and
already have those exported, this module picks them up too. The
debug endpoint's `tracing_flag_source` / `api_key_source` fields
tell you which variant was actually read.

Restart your shell so the new vars are exported. Then restart the
backend:

```bash
uv run uvicorn backend.api_server:app --reload --port 8765
```

You should see one line in the startup output:

```
LangSmith tracing: ON (project=personalcoach, source=LANGSMITH_TRACING, endpoint=https://api.smith.langchain.com)
```

Four states this line can show — they tell you what's wrong:

| Line                                                              | What's wrong                                                              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `LangSmith tracing: OFF (no LANGSMITH_TRACING / LANGCHAIN_TRACING set)` | env var didn't make it into the process (forgot to restart shell)   |
| `LangSmith tracing: MISCONFIGURED — flag is 'X' but langsmith requires lowercase 'true'` | typo (`=1`, `=True`, etc.) — change to lowercase `true`  |
| `LangSmith tracing: MISCONFIGURED — tracing flag is set but no API key found` | flag is correct but no key in either namespace — spans 401 silently |
| `LangSmith tracing: ON (project=default, source=LANGCHAIN_TRACING, ...)` | working, but on the legacy namespace and no project name set        |

## 3. Verify

Two ways to confirm spans are actually flowing:

**A. Via the API:**

```bash
curl -s http://localhost:8765/api/admin/observability | python3 -m json.tool
```

Should return:
```json
{
  "tracing_enabled": true,
  "tracing_flag": "true",
  "tracing_flag_source": "LANGSMITH_TRACING",
  "api_key_set": true,
  "api_key_source": "LANGSMITH_API_KEY",
  "project": "personalcoach",
  "endpoint": "https://api.smith.langchain.com"
}
```

`tracing_flag_source` / `api_key_source` tell you which env-var
name was actually picked up (`LANGSMITH_TRACING_V2` vs
`LANGSMITH_TRACING` vs `LANGCHAIN_TRACING_V2` vs `LANGCHAIN_TRACING`).
If you set `LANGCHAIN_*` but the source says `LANGSMITH_*`, you
have an empty `LANGSMITH_*` masking the legacy export — pick one.

The endpoint lives at `/api/admin/observability` (not `/api/debug/`)
so a future auth middleware can match the `/api/admin/*` namespace
by path prefix. Local-only by convention; if you ever expose this
server publicly, gate behind auth.

`api_key_set: true` confirms the key was found (the value itself
is never echoed — it's a secret).

**B. Trigger a turn:**

Open `/coach` and send any message. Within ~10 seconds, refresh
<https://smith.langchain.com> → your project. You should see a new
trace with the full agent run: prompt → tool calls
(`get_model("..."`), `list_models`, etc.) → final answer.

## 4. Things to query in the UI

Now that traces are flowing, here are the views worth bookmarking:

- **Filter by prompt_version=v8** — see all turns since the
  date-injection fix landed (PR #84).
- **Filter by tool name** — e.g. `get_model("sleep.debt_14d")` to
  see how often the agent reads the sleep baseline + what it
  returns.
- **Sort by duration** — find slow turns. Often points at an LLM
  call that fell back to a slower provider.
- **Diff two runs** — pick two traces of the same user_input
  across prompt versions to see what changed.

## 5. Turning it off

`unset LANGSMITH_TRACING` (or set `=false`) and restart. The
JSONL trace keeps working — you just lose the hosted UI.

## Privacy notes

- LangSmith stores user prompts + LLM completions on their
  infrastructure. Don't enable this with sensitive data unless
  you've reviewed their data policy.
- The API key is read from env once at langchain import time.
  Don't put it in any tracked file (no `.env` committed to git).
- The `/api/debug/observability` endpoint returns the project
  name + endpoint URL but never the key value.
