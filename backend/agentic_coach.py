"""AgenticCoach — single-persona, MCP-tool-using AI coach.

Replaces the v1 coach/doctor split. One node, native tool calling via
LangGraph's `create_react_agent`. Tools come from the personal-coach
MCP server (stdio subprocess) loaded via `langchain-mcp-adapters` —
same `personal_coach_mcp.py` an external client (Claude Desktop,
Cursor) would use.

Design notes (see docs/mcp_tools_design.md):

- Three streams (objective / perceived / planned) stay nested under
  those keys in tool outputs. The agent learns to reason about
  mismatches between them; we never collapse them into a single
  "effort" field. (See feedback_perceived_vs_intent.md.)
- Default actions (review_workout / make_plan / review_health /
  follow_up_memory / summarize_and_archive) pre-fetch a tuned set of
  tools in parallel and inject results into the system prompt as
  structured JSON. The model still gets the full tool list bound, so
  it can call more tools if it wants — but it never has to start
  from scratch.
- `_build_calendar_context` / `_build_cognitive_context` /
  `build_agent_working_memory` (v1 prompt-injection helpers) are gone.
  The agent calls the equivalent MCP tools on demand (or finds the
  data pre-fetched on default actions).

Public API kept stable for callers in api_server:
- chat(user_input, thread_id, system_context=None, agent=None) — the
  `agent` arg is accepted but ignored (back-compat with coach/doctor
  dropdown frontend).
- analyze_run(...) and analyze_health(...) — back-compat shims that
  internally route to review_workout / review_health.
- get_history / consolidate_and_learn / generate_episodic_summary /
  summarize_thread — unchanged.
"""

from __future__ import annotations

import asyncio
import atexit
import datetime
import json
import os
import sqlite3
import threading
from datetime import date, timedelta
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.prebuilt import create_react_agent

# llm_provider is the ONLY module allowed to construct LangChain models.
from backend.llm_provider import (
    _get_llm,  # private to other modules; in-project use is fine
    call_llm,
    get_provider_model_name,
)


# ---------------------------------------------------------------------------
# System prompt — single persona, replaces v1's coach + doctor templates.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an elite running coach and sports physiologist for one user.
You speak in the user's preferred language (Chinese for free-form chat,
Markdown headings in mixed Chinese/English are fine for analysis).

You are NOT split into "coach" and "doctor" personas. Both running
biomechanics/training and physiological recovery/sleep/HRV are within
your scope — pick the right tools for each question.

## Streams (NEVER collapse them)

You have MCP tools that read the user's data. Reasoning across these
streams correctly is the single most important thing.

### objective (raw sensor data — and ONLY raw)

  • HR (bpm timeseries + per-lap avg)
  • pace, distance, elevation
  • HR drift (first vs last third), under elevation context

Garmin's **per-run** interpretive labels are filtered at the MCP data
layer — you won't see them in tool returns:
  • aerobicTrainingEffect / anaerobicTrainingEffect (scores)
  • activityTrainingLoad (per-activity load score)
  • trainingEffectLabel ("TEMPO", "VO2MAX", "RECOVERY", etc.)
  • aerobicTrainingEffectMessage (Garmin's English description)

If you're asked "what kind of run was this", answer from HR
distribution + pace + the user's own labels. Never use a Garmin-style
category name (Tempo / Base / Threshold / Anaerobic) for individual
runs — that's Garmin's vocabulary, not the user's.

Garmin's **long-term baselines** in `athlete_profile.fitness` are
NOT filtered and ARE useful — these are rolling-window estimates,
not per-run guesses. Use them as reference points:
  • `vo2max_running` — fitness ceiling proxy
  • `lactate_threshold_hr` / `lactate_threshold_pace` — anchor for
    threshold work ("today's tempo at HR 170 was just under LT 173")

### perceived — TWO layers, both authored by the user, both valid

**Medium-term mapping (`athlete_profile.fitness.medium_term_hr_effort_map`)**
The user's *current expected* HR ↔ effort mapping. Each band has a
`name` (e.g., "Steady / Constant") and an `rpe_label` (e.g., "Steady
Effort") naming what HR range they expect to feel like that effort.
Stable on the order of months. Re-tune slowly.

**Short-term per-run labels (`manual_meta`)**
What the user *actually labeled* a specific run as, after running it.
  • `category_stats[]` — per-segment summary: {category, distance_mi,
    pace, avg_hr}
  • `lap_categories[]` — one label per lap (parallel to laps array)
  • `notes` — free-form context

Both use the same RPE vocabulary as the zones (Steady Effort, Marathon,
etc.) — see the Vocabulary Trap section below.

### planned

Calendar-driven workout intent (Phase 2; null on every run for now).

### the coaching signal

The medium-term zones and short-term labels are BOTH the user's truth.
The job is to compare:

  short-term label  ⟷  raw HR/pace from this run  ⟷  medium-term zone

A persistent gap between short-term labels and the zone HR they map
to is the SIGNAL that the medium-term mapping needs updating. After
3-4 months of accumulating label-vs-HR data, you should proactively
suggest re-tuning the zones — that's the long-term feedback loop these
two layers are designed for. Don't suggest re-tuning on a single run.

## Vocabulary Trap (READ THIS BEFORE EVERY ANALYSIS)

The phrases "Hold Back Easy / Steady Effort / Increasing Effort /
Marathon / LT Effort / VO2Max" appear in BOTH:

  (a) `athlete_profile.fitness.medium_term_hr_effort_map[].rpe_label`
      — naming a HR band the user predefined (the medium-term
      mapping)
  (b) `manual_meta.category_stats[].category` and `lap_categories[]` —
      the user's per-segment label for what they intended this run's
      segments to be (the short-term per-run label)

These are NOT the same thing. Same words, different objects:
  - effort_map entry "Steady Effort" = an HR range (e.g., 145-162 bpm)
  - lap "Steady Effort"               = the user's intent for THIS lap

NEVER write "你将其定义为 'Hold Back Easy'" or "you classified this
as <X>" UNLESS that exact label appears in `manual_meta`. If the user
didn't label this run/lap with that word, don't put it in their mouth.
Cite the SOURCE explicitly: "profile 里 Steady Effort = 145-162 bpm"
vs "你给前 10 mi 贴了 Steady Effort 标签".

## Conversation session rules

This conversation is one session, bounded by the user clicking
"End & Save" when they're done. While it's active you have full
verbatim access to every message in this session. You do NOT see
content from prior closed sessions — those have been internalized
into memory tools (CME). Don't ask the user to repeat things from
prior sessions; use the tools.

**On the FIRST user message of a new session** (no prior AI message
in the conversation), call `get_pending_clarifications` exactly once.
If it returns any items, ask the user those questions BEFORE any
other coaching. These are explicit conflicts the agent owes the user
from prior sessions.

**Throughout the session**, when the user references a past
situation, an old goal, an injury / niggle / preference you'd
plausibly already know about, or whenever you'd want to reference
prior coaching — call `recall_topics(status="active")` or
`search_episodes(keywords=[...])`. Don't fabricate history.

When a default action (review_workout / make_plan / review_health /
follow_up_memory) has pre-fetched tool results into the system
prompt above your messages, USE that data — don't re-call those
tools. Call additional tools only for what's missing.

## Output

- Use Markdown with clear sections for analysis / recommendations.
- Reference specific numbers from tool outputs to ground your claims.
- If a tool returned null/empty, say so. Never fabricate.
- NEVER render internal identifiers in user-facing prose. Specifically,
  do not include any of these in the answer text:
    • `tpc_<hex>` (topic ids from recall_topics)
    • `epi_<hex>` (episode ids from search_episodes)
    • `pnd_<hex>` (pending-clarification ids)
    • activity_id numbers (use the run's date + name instead)
  These are tool-facing identifiers only — referring to topics or
  episodes by their natural name is sufficient and far more readable.
  Bad:  "右膝外侧下坡刺痛 (tpc_713f9d8e) 处于观察期"
  Good: "右膝外侧下坡刺痛处于观察期"
"""


# ---------------------------------------------------------------------------
# Default-action prompt fragments — appended after the JSON pre-fetch block.
# ---------------------------------------------------------------------------

_REVIEW_WORKOUT_INSTRUCTIONS = """### TASK: Review the workout above

Required reading order. Do NOT skip step 1 — going straight to HR
distribution before you've read what the user labeled is the single
biggest mistake a coach can make here.

**Step 1 — short-term perceived (the user's labels for THIS run)**
Read `manual_meta.category_stats`, `manual_meta.lap_categories`, and
`manual_meta.notes`. State plainly what the user labeled each segment
as. If `manual_meta` is empty, say so and continue with objective
only — don't invent labels.

**Step 2 — objective (raw sensor data)**
Pace, HR distribution per segment (use `category_stats[].avg_hr` if
present; otherwise compute from telemetry against the user's zones),
HR drift over the run with elevation context. Raw numbers only.

**Step 3 — medium-term mapping (where do those HRs land in the user's
zones)**
For each segment the user labeled, present a small vertical block
comparing the label, the actual avg_hr, and the matching profile
zone. Use this exact structure (NOT a markdown table — tables get
squashed on the phone-width chat bubble). One block per segment:

```
**前 10 mi · 你的标注: Steady Effort**
- 实际 avg HR: 159 bpm
- 档案区间: Steady Effort (145-162 bpm)
- 匹配度: 高度匹配 — HR 落在区间高位
```

Don't add a header row. Don't combine multiple segments into one
table. One block per segment, plain prose between if you want to
note anything special.

**Step 4 — interpretation**
- Where does the short-term label match the medium-term zone the HR
  fell in? That confirms the user's current mapping is calibrated for
  that effort.
- Where do they DISAGREE? Note it, but a single mismatch isn't enough
  to suggest re-tuning zones. Flag it for tracking, not for action.
- Is HR drift consistent with the user's labels? (e.g., a "Steady"
  segment that drifts heavily under flat terrain = fatigue signal,
  not a labeling error.)

**Step 5 — recovery + cycle context**
Was today's readiness aligned with the effort? Where in the cycle
phase / ACWR band does this run sit?

**Step 6 — recommendation**
What should the next 1-2 sessions look like given the above? Speak
in the user's RPE vocabulary — "next session: Hold Back Easy, max
HR 144".
"""

_MAKE_PLAN_INSTRUCTIONS = """### TASK: Propose the next 3-5 sessions

Use:
- The user's cycle phase (`current_block.phase`) and weeks remaining.
- ACWR band — push if sweet, hold if caution, taper if danger.
- Today's readiness score.
- Calendar availability — DO NOT schedule runs that overlap work
  blocks, PT, or other commitments. Note which time-of-day slots are
  free.
- Pending CME clarifications — resolve them before committing to a
  plan if they're material.

Output: a Markdown table of date / planned workout / target effort
zone (use the user's RPE labels) / target distance, plus a
one-paragraph rationale.
"""

_REVIEW_HEALTH_INSTRUCTIONS = """### TASK: Review the user's recovery state

**Line 1 must be a single declarative sentence** summarizing today's
recovery state in plain language. No heading marker, no bullet, no
greeting, no "根据数据" preamble. Just one sentence the user can read
off a glance card. Examples:
  • "今日恢复良好，绿灯日，可以正常训练。"
  • "HRV 偏低 + 睡眠不足，建议改为轻量恢复。"
  • "黄灯 — 各项指标偏离基线，今天降一档强度。"

Then a blank line, then the full breakdown:

1. Today's readiness score and what's driving it (which marker is
   off baseline).
2. ACWR + 28-day load — is the body absorbing the load or
   accumulating fatigue?
3. Active CME topics that mention injury / soreness — any progression?
4. Recommendation — green/yellow/red day, what TYPE of session is OK.
"""

_FOLLOW_UP_MEMORY_INSTRUCTIONS = """### TASK: Drive the conversation forward on open memory items

You have `recall_topics(active)` results pre-fetched. For each
non-resolved topic, ask the user a SPECIFIC follow-up question (not
"how is everything"). Format as a numbered list of questions. Do NOT
give advice yet — gather the user's answer first.

If `pending_clarifications` is non-empty, ask THOSE FIRST — they're
explicit conflicts the agent owes the user.
"""


# ---------------------------------------------------------------------------
# Pre-fetch plans — tool name + arg dict per action. Run in parallel
# with asyncio.gather, results JSON-injected into the system prompt.
# ---------------------------------------------------------------------------

def _prefetch_review_workout(
    activity_id: int, run_date: str | None
) -> list[tuple[str, dict]]:
    plan: list[tuple[str, dict]] = [
        ("get_athlete_profile", {}),
        ("get_run_detail", {"activity_id": activity_id}),
        ("get_run_telemetry", {"activity_id": activity_id, "downsample_sec": 30}),
    ]
    if run_date:
        plan.append(("get_readiness", {"date": run_date}))
    plan.append(("get_pending_clarifications", {}))
    return plan


def _prefetch_make_plan() -> list[tuple[str, dict]]:
    today = date.today().isoformat()
    horizon_end = (date.today() + timedelta(days=14)).isoformat()
    return [
        ("get_athlete_profile", {}),
        ("get_readiness", {}),
        ("get_training_load", {"window_days": 28}),
        ("list_blocks", {}),
        (
            "get_calendar_events",
            {"start": f"{today}T00:00:00", "end": f"{horizon_end}T23:59:59"},
        ),
        ("get_pending_clarifications", {}),
        # Last 14 days of runs for narrative context
        ("list_runs", {
            "start": (date.today() - timedelta(days=14)).isoformat(),
            "end": today,
        }),
    ]


def _prefetch_review_health() -> list[tuple[str, dict]]:
    return [
        ("get_athlete_profile", {}),
        ("get_readiness", {}),
        ("get_training_load", {"window_days": 28}),
        ("recall_topics", {"status": "active"}),
    ]


def _prefetch_follow_up_memory() -> list[tuple[str, dict]]:
    return [
        ("recall_topics", {"status": "active"}),
        ("get_pending_clarifications", {}),
    ]


def _started_at_from_thread_id(thread_id: str) -> str | None:
    """Coach session thread ids look like `coach_20260509T220103Z`.
    Pull out the timestamp portion as ISO-8601 so the frontend can
    sort/format without parsing thread_ids itself."""
    if not thread_id.startswith("coach_"):
        return None
    rest = thread_id[len("coach_"):]
    # Expect 8+T+6+Z layout (UTC-compact). Reformat to standard ISO.
    if len(rest) >= 16 and rest.endswith("Z"):
        try:
            y, mo, d = rest[0:4], rest[4:6], rest[6:8]
            h, mi, s = rest[9:11], rest[11:13], rest[13:15]
            return f"{y}-{mo}-{d}T{h}:{mi}:{s}Z"
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# AgenticCoach
# ---------------------------------------------------------------------------

class AgenticCoach:
    """Single-persona, MCP-tool-using AI coach.

    Lazy-spawns the personal-coach MCP server on first chat call and
    keeps it alive for the api-server's lifetime. atexit handler
    teardown.
    """

    def __init__(
        self,
        db_path: str = "data/chat_memory.db",
        user_profile: dict | None = None,
        memory_engine=None,
        skip_api_probe: bool = False,
    ):
        """
        Args:
            skip_api_probe: when True, `_ensure_agent` skips the
                `_require_api_reachable(api_base)` round-trip. Lets
                tests construct an AgenticCoach + exercise the
                non-agent methods (session_meta sidecar, history
                lookup, delete_session, etc.) without standing up a
                real api_server. Production paths leave this False.
        """
        self.db_path = db_path
        # `db_path` may be `:memory:` or a path with no parent (e.g. a
        # bare tmp filename in tests). Guard against passing "" to
        # os.makedirs which raises FileNotFoundError.
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._skip_api_probe = skip_api_probe

        # Semantic profile — kept for back-compat (some v1 callers
        # passed this in). The new prompt doesn't pre-inject it; the
        # agent calls get_athlete_profile() instead when needed.
        self.user_profile = user_profile or {}

        # Cognitive Memory Engine — still used for consolidate &
        # episodic summary helpers (those are post-conversation, NOT
        # LLM-callable tools).
        self.memory_engine = memory_engine

        # Conversation persistence — sync SqliteSaver for read-only
        # get_history calls (api_server uses it from a sync handler).
        # AsyncSqliteSaver (created lazily in _ensure_agent) drives the
        # actual agent loop, since LangGraph's prebuilt create_react_agent
        # invokes checkpointer methods on the event loop. Both savers
        # point at the same SQLite file; SQLite handles cross-process
        # concurrency fine for our single-user load.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.conn)
        self._aio_checkpointer: AsyncSqliteSaver | None = None
        self._aio_checkpointer_cm = None

        # Session metadata sidecar — closed_at, summary, counts.
        # Lives in the same chat_memory.db so a single backup covers
        # both messages and session lifecycle. Active sessions have no
        # row here; closed sessions have closed_at populated.
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_meta (
                thread_id      TEXT PRIMARY KEY,
                closed_at      TEXT,
                summary        TEXT,
                topics_added   INTEGER DEFAULT 0,
                episodes_added INTEGER DEFAULT 0
            )
            """
        )
        self.conn.commit()

        # MCP + agent are lazy-built on first chat (need an event loop).
        self._mcp_client = None
        self._mcp_tools: list[Any] | None = None
        self._mcp_tools_by_name: dict[str, Any] = {}
        self._agent = None
        self._init_lock: asyncio.Lock | None = None

        # Background event loop. AsyncSqliteSaver + MCP client + the
        # LangGraph agent all bind to the loop they're created on; if
        # we let each request open its own loop (asyncio.run from a
        # threadpool worker) the second request gets "no active
        # connection" because aiosqlite's connection is dead. So we
        # own one daemon-thread loop and submit every coroutine to it
        # via run_coroutine_threadsafe — works whether the caller is
        # sync (tests, ad-hoc scripts) or async (FastAPI handlers can
        # also submit). The loop survives until process exit.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            name="agentic-coach-loop",
            daemon=True,
        )
        self._loop_thread.start()

        # Track last provider for attribution display.
        self._last_provider: str | None = None

        atexit.register(self._cleanup_sync)

    # -- API reachability probe --------------------------------------

    @staticmethod
    async def _require_api_reachable(api_base: str) -> None:
        """Ping api_server's /api/runs endpoint once; raise a clear
        error if unreachable. Required because every MCP tool resolves
        through HTTP to api_server — non-api callers (dashboard, tests)
        used to read DataProcessor in-process, but the v2 design moved
        all data access through HTTP to keep a single source of truth
        and avoid two live DataProcessor instances racing on the same
        JSON files. So api_server must be running."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.get(f"{api_base}/api/runs", params={
                    "start": "2024-01-01", "end": "2024-01-02",
                })
                # Any 2xx/4xx means the server is up. 5xx + connection
                # errors are the failure modes we care about.
                if r.status_code >= 500:
                    raise RuntimeError(
                        f"api_server reachable at {api_base} but returned "
                        f"{r.status_code}"
                    )
        except Exception as e:
            raise RuntimeError(
                "AgenticCoach now requires api_server to be running — every "
                "MCP tool wraps a FastAPI endpoint. Start it with:\n"
                "    uv run uvicorn backend.api_server:app --port 8765\n"
                "or override PERSONAL_COACH_API_BASE if it's on a different "
                f"host/port. Tried: {api_base} ({e!r})"
            ) from e

    # -- Loop submit helper ------------------------------------------

    def _submit(self, coro):
        """Run a coroutine on the agent's background loop and block
        until it completes. Safe to call from sync (tests, ad-hoc
        scripts) or FastAPI handlers (which run on threadpool workers)."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # -- MCP / agent init --------------------------------------------

    async def _ensure_agent(self) -> None:
        """Lazy: spawn the MCP subprocess + bind tools + wrap in
        create_react_agent. Holds a lock so concurrent first-calls
        don't race. Runs on self._loop (called via _submit)."""
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        if self._agent is not None:
            return
        async with self._init_lock:
            if self._agent is not None:
                return

            from langchain_mcp_adapters.client import MultiServerMCPClient

            api_base = os.environ.get(
                "PERSONAL_COACH_API_BASE", "http://127.0.0.1:8765"
            )

            # Hard prereq: every MCP tool wraps an api_server endpoint.
            # If the FastAPI server isn't reachable, every tool call
            # will fail mid-conversation with a ToolException — really
            # confusing for ad-hoc-script callers. Probe once up front
            # and bail with a message that points the user at the fix.
            #
            # Tests opt out via skip_api_probe=True — they typically
            # mock the agent's chat/action methods entirely and never
            # actually hit the MCP subprocess, so probing api_server
            # would be a pointless dependency.
            if not self._skip_api_probe:
                await self._require_api_reachable(api_base)
            self._mcp_client = MultiServerMCPClient({
                "personal-coach": {
                    "command": "uv",
                    "args": ["run", "python", "-m", "backend.personal_coach_mcp"],
                    "transport": "stdio",
                    "env": {
                        **os.environ,
                        "PERSONAL_COACH_API_BASE": api_base,
                    },
                }
            })
            self._mcp_tools = await self._mcp_client.get_tools()
            self._mcp_tools_by_name = {t.name: t for t in self._mcp_tools}

            # AsyncSqliteSaver is exposed as an async context manager.
            # Hold the entered manager so it survives across requests;
            # _cleanup_sync closes it at process exit.
            self._aio_checkpointer_cm = AsyncSqliteSaver.from_conn_string(
                self.db_path
            )
            self._aio_checkpointer = await self._aio_checkpointer_cm.__aenter__()

            # Use Gemini 3.1 Flash Lite as primary chat model for agent flow.
            # Free-tier limits (May 2026): 15 RPM / 250k TPM / 500 RPD —
            # the 250k TPM in particular is the headroom we needed; Groq
            # Llama 3.3 70B free tier capped at 12k TPM and review_workout's
            # ~14k first-turn prompt overran. The call_llm fallback chain
            # still applies to non-tool flows (consolidation, summaries,
            # episodic summaries) — those are unchanged.
            llm = _get_llm("creative", "gemini")
            self._last_provider = "gemini"

            self._agent = create_react_agent(
                model=llm,
                tools=self._mcp_tools,
                checkpointer=self._aio_checkpointer,
                prompt=_SYSTEM_PROMPT,
            )

    def _cleanup_sync(self) -> None:
        """Best-effort teardown at process exit: MCP subprocess +
        AsyncSqliteSaver context. Runs on self._loop, then stops it."""
        async def _close():
            cm = self._aio_checkpointer_cm
            if cm is not None:
                try:
                    await cm.__aexit__(None, None, None)
                except Exception:
                    pass
            client = self._mcp_client
            if client is not None:
                try:
                    await client.aclose()
                except Exception:
                    pass

        try:
            future = asyncio.run_coroutine_threadsafe(_close(), self._loop)
            future.result(timeout=5)
        except Exception:
            pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass

    # -- Pre-fetch helper --------------------------------------------

    async def _prefetch(
        self, plan: list[tuple[str, dict]]
    ) -> dict[str, Any]:
        """Run a list of (tool_name, args) calls in parallel via the
        MCP session. Returns a dict keyed by tool_name. Errors per
        call are kept as `{"error": "..."}` so the LLM can see what
        failed."""
        await self._ensure_agent()

        async def _one(name: str, args: dict) -> tuple[str, Any]:
            tool = self._mcp_tools_by_name.get(name)
            if tool is None:
                return name, {"error": f"tool {name!r} not found"}
            try:
                result = await tool.ainvoke(args)
                # Tools return JSON strings — parse so the result block
                # is structured rather than escaped.
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except Exception:
                        pass
                return name, result
            except Exception as e:
                return name, {"error": str(e)}

        pairs = await asyncio.gather(*[_one(n, a) for n, a in plan])
        return dict(pairs)

    # -- Back-compat sync chat ---------------------------------------

    def chat(
        self,
        user_input: str,
        thread_id: str,
        system_context: str | None = None,
        agent: str | None = None,  # ignored (legacy coach/doctor flag)
    ) -> str:
        """Free-form chat. The `agent` arg is accepted for back-compat
        but ignored — there's only one persona now."""
        return self._submit(
            self._run_turn(
                user_input=user_input,
                thread_id=thread_id,
                extra_system_context=system_context,
            )
        )

    # -- Streaming chat (SSE source for /api/ai/chat/stream) ---------

    @staticmethod
    def _chunk_text(chunk) -> str:
        """Extract plain text from a streaming chat-model chunk.
        Gemini-via-LangChain sometimes emits content as a list of
        text blocks; flatten to plain str. Empty chunks (e.g. metadata
        or tool-call deltas with no visible text) return ""."""
        c = getattr(chunk, "content", None)
        if isinstance(c, list):
            return "".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and "text" in b
            )
        return str(c) if c else ""

    async def chat_stream(
        self,
        user_input: str,
        thread_id: str,
        system_context: str | None = None,
    ):
        """Streaming version of chat. Async generator yielding
        progress events:

          {"type": "token",     "content": "..."}    # LLM text chunk
          {"type": "tool_call", "name": "..."}      # tool invocation
          {"type": "done"}                           # stream finished cleanly
          {"type": "error",     "message": "..."}   # turn failed

        Powers `/api/ai/chat/stream` (SSE). The synchronous `chat`
        method above is kept for any non-UI callers (tests, future
        batch jobs).

        Internally drives `self._agent.astream_events(..., version="v2")`
        and filters to the events users care about. The full message
        state (including tool calls) still lands in the checkpointer
        at end — frontend should invalidate `/api/ai/history` on
        `done` to pick up the canonical history with timestamps.
        """
        await self._ensure_agent()
        config = {"configurable": {"thread_id": thread_id}}
        messages: list[BaseMessage] = []
        if system_context:
            messages.append(SystemMessage(content=system_context))
        messages.append(HumanMessage(content=user_input))

        try:
            async for ev in self._agent.astream_events(
                {"messages": messages}, config, version="v2"
            ):
                et = ev.get("event")
                if et == "on_chat_model_stream":
                    text = self._chunk_text(ev["data"].get("chunk"))
                    if text:
                        yield {"type": "token", "content": text}
                elif et == "on_tool_start":
                    yield {"type": "tool_call", "name": ev.get("name", "unknown")}

            # Trailing "[Generated by X]" footer — matches what the
            # sync _run_turn appends, so the persisted message in the
            # checkpointer and the streamed assembly stay consistent.
            provider = self._last_provider or "gemini"
            try:
                model_name = get_provider_model_name(provider)
            except Exception:
                model_name = provider
            yield {"type": "token", "content": f"\n\n[Generated by {model_name}]"}
            yield {"type": "done"}
        except Exception as e:
            yield {"type": "error", "message": str(e)}

    # -- Default actions (sync wrappers around _action_turn) ---------

    def review_workout(
        self,
        activity_id: int,
        thread_id: str,
        run_date: str | None = None,
        user_message: str | None = None,
    ) -> str:
        """Append a workout-review action to the given session thread.
        Pre-fetches profile + run detail + telemetry + readiness +
        pending in parallel. See docs/coach_chat_design.md."""
        return self._submit(self._action_turn(
            plan=_prefetch_review_workout(activity_id, run_date),
            instructions=_REVIEW_WORKOUT_INSTRUCTIONS,
            user_input=user_message or (
                f"请分析我 activity_id={activity_id} 这次训练。"
            ),
            thread_id=thread_id,
        ))

    def make_plan(
        self,
        thread_id: str,
        user_message: str | None = None,
    ) -> str:
        return self._submit(self._action_turn(
            plan=_prefetch_make_plan(),
            instructions=_MAKE_PLAN_INSTRUCTIONS,
            user_input=user_message or "请帮我安排接下来 3-5 次训练。",
            thread_id=thread_id,
        ))

    def review_health(
        self,
        thread_id: str,
        user_message: str | None = None,
    ) -> str:
        return self._submit(self._action_turn(
            plan=_prefetch_review_health(),
            instructions=_REVIEW_HEALTH_INSTRUCTIONS,
            user_input=user_message or "请评估我今天的恢复状态。",
            thread_id=thread_id,
        ))

    def follow_up_memory(
        self,
        thread_id: str,
        user_message: str | None = None,
    ) -> str:
        return self._submit(self._action_turn(
            plan=_prefetch_follow_up_memory(),
            instructions=_FOLLOW_UP_MEMORY_INSTRUCTIONS,
            user_input=user_message or "我们之前聊过哪些没结束的话题？",
            thread_id=thread_id,
        ))

    def summarize_and_archive(self, thread_id: str) -> dict[str, Any]:
        """Close a session: summarize, run CME consolidation, record
        session_meta so the sessions list shows it as closed.

        Idempotent: if the thread is already archived, returns the
        existing summary without re-running consolidation. Empty
        sessions (≤2 messages) get a no-op archive that just clears
        client state — no summary, no CME write."""
        # Idempotency: don't double-archive.
        existing = self._get_session_meta(thread_id)
        if existing and existing.get("closed_at"):
            return {
                "thread_id": thread_id,
                "summary": existing.get("summary"),
                "consolidation": {
                    "topics_added": existing.get("topics_added", 0),
                    "episodes_added": existing.get("episodes_added", 0),
                    "already_archived": True,
                },
                "closed_at": existing.get("closed_at"),
            }

        chat_list = self._chat_list_for_thread(thread_id)
        if len(chat_list) < 2:
            # Empty / 1-turn session — no point summarizing or
            # consolidating. Just stamp it closed so it doesn't
            # show up as active forever.
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._set_session_meta(
                thread_id, closed_at=now_iso, summary=None,
                topics_added=0, episodes_added=0,
            )
            return {
                "thread_id": thread_id,
                "summary": None,
                "consolidation": {"empty": True},
                "closed_at": now_iso,
            }

        summary = self.summarize_thread(thread_id)
        consolidation: dict[str, Any] | None = None
        topics_added = 0
        episodes_added = 0
        if self.memory_engine:
            try:
                consolidation = self.memory_engine.consolidate_memory_background(
                    thread_id, chat_list
                )
                # consolidate_memory_background may return None or a dict;
                # the dict (when present) reports counts under various
                # keys depending on CME version. Pull defensively.
                if isinstance(consolidation, dict):
                    topics_added = (
                        len(consolidation.get("new_topics") or [])
                        + len(consolidation.get("topics_created") or [])
                    )
                    episodes_added = (
                        len(consolidation.get("new_episodes") or [])
                        + len(consolidation.get("episodes_created") or [])
                    )
            except Exception as e:
                consolidation = {"error": str(e)}

        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._set_session_meta(
            thread_id, closed_at=now_iso, summary=summary,
            topics_added=topics_added, episodes_added=episodes_added,
        )

        return {
            "thread_id": thread_id,
            "summary": summary,
            "consolidation": consolidation,
            "topics_added": topics_added,
            "episodes_added": episodes_added,
            "closed_at": now_iso,
        }

    # -- Core turn runners (async) -----------------------------------

    async def _action_turn(
        self,
        plan: list[tuple[str, dict]],
        instructions: str,
        user_input: str,
        thread_id: str,
    ) -> str:
        await self._ensure_agent()
        prefetched = await self._prefetch(plan)
        prefetched_block = (
            "### PRE-FETCHED TOOL RESULTS (already gathered, don't re-call):\n"
            f"```json\n{json.dumps(prefetched, ensure_ascii=False, indent=2)}\n```\n"
        )
        return await self._run_turn(
            user_input=user_input,
            thread_id=thread_id,
            extra_system_context=prefetched_block + "\n" + instructions,
        )

    async def _run_turn(
        self,
        user_input: str,
        thread_id: str,
        extra_system_context: str | None = None,
    ) -> str:
        """One conversation turn through the LangGraph agent. The
        prebuilt agent handles its own tool-call loop natively — we
        just feed messages in and read the final AI message out."""
        await self._ensure_agent()
        config = {"configurable": {"thread_id": thread_id}}

        messages: list[BaseMessage] = []
        if extra_system_context:
            messages.append(SystemMessage(content=extra_system_context))
        messages.append(HumanMessage(content=user_input))

        state = await self._agent.ainvoke({"messages": messages}, config)
        final = state["messages"][-1]
        content = final.content if isinstance(final, AIMessage) else str(final)
        if isinstance(content, list):
            content = "".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and "text" in b
            )
        content = str(content)

        provider = self._last_provider or "gemini"
        try:
            model_name = get_provider_model_name(provider)
        except Exception:
            model_name = provider
        return content + f"\n\n[Generated by {model_name}]"

    # -- Session metadata --------------------------------------------

    def _get_session_meta(self, thread_id: str) -> dict | None:
        cur = self.conn.execute(
            "SELECT thread_id, closed_at, summary, topics_added, episodes_added "
            "FROM session_meta WHERE thread_id = ?",
            (thread_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "thread_id": row[0],
            "closed_at": row[1],
            "summary": row[2],
            "topics_added": row[3] or 0,
            "episodes_added": row[4] or 0,
        }

    def _set_session_meta(
        self,
        thread_id: str,
        *,
        closed_at: str | None,
        summary: str | None,
        topics_added: int,
        episodes_added: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO session_meta(thread_id, closed_at, summary, topics_added, episodes_added)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                closed_at = excluded.closed_at,
                summary = excluded.summary,
                topics_added = excluded.topics_added,
                episodes_added = excluded.episodes_added
            """,
            (thread_id, closed_at, summary, topics_added, episodes_added),
        )
        self.conn.commit()

    def list_sessions(
        self, limit: int = 10, before: str | None = None
    ) -> list[dict]:
        """List Coach sessions in reverse-chronological order (newest
        first), paginated.

        Walks LangGraph's checkpoint table for distinct thread_ids
        matching `coach_*`, joins with session_meta for closed_at /
        summary / counts. `started_at` derived from the thread_id
        timestamp suffix (cheap, sortable). `message_count` derived
        from the latest checkpoint of that thread.

        `before` accepts a thread_id; only thread_ids
        lexicographically less than it are returned (since thread_ids
        are timestamp-prefixed, lex order = chronological)."""
        # Pull distinct coach_* thread_ids from the checkpoints table.
        # AsyncSqliteSaver creates the table; if no chat has happened
        # yet, the table won't exist — return [].
        try:
            sql = """
                SELECT DISTINCT thread_id FROM checkpoints
                WHERE thread_id LIKE 'coach_%'
            """
            params: list = []
            if before:
                sql += " AND thread_id < ?"
                params.append(before)
            sql += " ORDER BY thread_id DESC LIMIT ?"
            params.append(limit)
            cur = self.conn.execute(sql, params)
            thread_ids = [r[0] for r in cur.fetchall()]
        except sqlite3.OperationalError:
            return []

        out: list[dict] = []
        for tid in thread_ids:
            meta = self._get_session_meta(tid) or {}
            msgs = self.get_history(tid)
            # Strip system messages from count — those are
            # internal/pre-fetch noise from the user's POV.
            user_or_ai = sum(
                1 for m in msgs if m.type in ("human", "ai")
            )
            out.append({
                "thread_id": tid,
                "started_at": _started_at_from_thread_id(tid),
                "closed_at": meta.get("closed_at"),
                "summary": meta.get("summary"),
                "topics_added": meta.get("topics_added", 0),
                "episodes_added": meta.get("episodes_added", 0),
                "message_count": user_or_ai,
            })
        return out

    def delete_session(self, thread_id: str) -> dict:
        """Wipe a coach session from chat_memory.db.

        Removes the LangGraph checkpoints + writes for `thread_id`
        plus the session_meta sidecar row. The CME side (topics /
        episodes that were consolidated *out* of this session) is
        deliberately NOT touched — those rows are commingled with
        memories from other sessions and we can't safely separate
        them. So a deleted thread loses its verbatim history but its
        long-term lessons remain in the agent's memory tools.

        Guards:
          - thread_id must look like `coach_<timestamp>Z` so a typo
            or malicious caller can't blow away non-coach checkpoint
            data.
          - Returns row-counts so the caller can tell "actually
            deleted" from "thread didn't exist".
        """
        if not (thread_id.startswith("coach_") and thread_id.endswith("Z")):
            raise ValueError(
                f"refusing to delete non-coach thread_id: {thread_id!r}"
            )
        try:
            with self.conn:  # transaction
                ck = self.conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
                ).rowcount
                wr = self.conn.execute(
                    "DELETE FROM writes WHERE thread_id = ?", (thread_id,)
                ).rowcount
                sm = self.conn.execute(
                    "DELETE FROM session_meta WHERE thread_id = ?", (thread_id,)
                ).rowcount
            return {
                "thread_id": thread_id,
                "checkpoints_deleted": ck,
                "writes_deleted": wr,
                "session_meta_deleted": sm,
            }
        except sqlite3.OperationalError as e:
            # Tables not created yet — nothing to delete, treat as no-op.
            return {
                "thread_id": thread_id,
                "checkpoints_deleted": 0,
                "writes_deleted": 0,
                "session_meta_deleted": 0,
                "note": f"sqlite tables missing: {e}",
            }

    # -- Conversation history helpers --------------------------------

    def get_history(self, thread_id: str) -> list[BaseMessage]:
        config = {"configurable": {"thread_id": thread_id}}
        try:
            tup = self.checkpointer.get_tuple(config)
            if tup is None:
                return []
            channel = tup.checkpoint.get("channel_values", {})
            return channel.get("messages", []) or []
        except Exception:
            return []

    @staticmethod
    def _message_content_text(msg: BaseMessage) -> str:
        """Normalize LangChain message content into a plain str.
        Some providers return content as a list of {type:'text', text:...}
        blocks; flatten those. Matches the same shape /api/ai/history
        and _chat_list_for_thread emit."""
        content = msg.content
        if isinstance(content, list):
            return "".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and "text" in b
            )
        return str(content)

    def get_history_with_ts(self, thread_id: str) -> list[dict]:
        """Return history with per-message first-seen timestamps.

        Walks `checkpointer.list()` chronologically (oldest first) and
        records the ts of the first checkpoint where each message
        position appeared. Used by /api/ai/history to power the UI's
        day-boundary dividers in long-running sessions that span
        multiple calendar days.

        Keyed by **list position**, not (type, content) — because users
        ask the same question on different days (e.g. "请评估我今天的恢复
        状态" each morning), so content-based keying would collapse
        them to the first day's ts. The messages list is append-only
        in this app (agent never trims), so position is stable across
        checkpoints.

        Missing ts (e.g. legacy checkpoint without `ts` field) falls
        back to None; the UI treats null as "no day anchor", preserving
        current behavior.

        Returns [{role, content, ts}] in chronological order. Empty
        list on any failure (matches `get_history` swallow-and-empty
        contract)."""
        config = {"configurable": {"thread_id": thread_id}}
        try:
            tuples = list(self.checkpointer.list(config))
        except Exception:
            return []
        if not tuples:
            return []
        # checkpointer.list returns newest-first; reverse for chronology
        tuples.reverse()

        ts_by_index: dict[int, str | None] = {}
        max_seen = -1
        for tup in tuples:
            ckpt = tup.checkpoint or {}
            ts = ckpt.get("ts")
            channel = ckpt.get("channel_values", {}) or {}
            msgs = channel.get("messages", []) or []
            for i in range(max_seen + 1, len(msgs)):
                ts_by_index[i] = ts
            if len(msgs) - 1 > max_seen:
                max_seen = len(msgs) - 1

        # Source of truth for the final message list is the newest
        # checkpoint — ensures we drop any messages that were rolled
        # back / superseded.
        last_msgs = (
            (tuples[-1].checkpoint or {}).get("channel_values", {}) or {}
        ).get("messages", []) or []
        out: list[dict] = []
        for i, m in enumerate(last_msgs):
            out.append({
                "role": m.type,
                "content": self._message_content_text(m),
                "ts": ts_by_index.get(i),
            })
        return out

    def _chat_list_for_thread(self, thread_id: str) -> list[dict]:
        """Adapter for memory_engine.consolidate_memory_background
        which wants [{role, content}] dicts."""
        history = self.get_history(thread_id)
        out = []
        for msg in history:
            if msg.type not in ("human", "ai"):
                continue
            content = msg.content
            if isinstance(content, list):
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and "text" in b
                )
            out.append({"role": msg.type, "content": str(content)})
        return out

    def summarize_thread(self, thread_id: str) -> str | None:
        """Compress a thread to 1-2 sentences. Used by
        summarize_and_archive and the existing UI flow.

        Routed through groq-first fallback so it doesn't compete with
        the agent's gemini RPM budget — this fires on every End & Save
        and would otherwise eat 1 of the 15 free RPM that the user-
        facing chat/action loop needs."""
        history = self.get_history(thread_id)
        if len(history) <= 3:
            return None
        chat_text = "\n".join(
            f"{m.type}: {m.content}"
            for m in history
            if m.type in ("human", "ai")
        )
        prompt = (
            "请将以下教练与运动员的对话，压缩成1-2句话的核心结论或建议。\n"
            "重点提取：运动员的痛点/感受，以及教练给出的具体对策。"
            "使用第三人称陈述句。\n\n"
            f"对话记录：\n{chat_text}"
        )
        msg, _ = call_llm(
            [HumanMessage(content=prompt)],
            role="precise",
            fallback_chain=["groq", "gemini"],
        )
        return str(msg.content).strip()

    # -- Back-compat shims -------------------------------------------

    def follow_up_chat(
        self, user_input: str, thread_id: str, agent: str | None = None
    ) -> str:
        # Identical to chat() now that there's no agent split.
        return self.chat(user_input=user_input, thread_id=thread_id)

    def analyze_run(
        self,
        working_memory_dict: dict,
        thread_id: str,
        telemetry_df=None,
        historical_memories=None,
    ) -> str:
        """Back-compat wrapper. v1 callers built a working_memory dict
        and passed it; the new flow ignores that and pre-fetches from
        MCP tools instead. We extract activity_id + date from the dict
        to drive review_workout."""
        ws = working_memory_dict.get("workout_summary") or {}
        activity_id = (
            working_memory_dict.get("activity_id")
            or ws.get("activity_id")
        )
        run_date = working_memory_dict.get("date")
        if not activity_id:
            return self.chat(
                user_input="请分析这次训练。",
                thread_id=thread_id,
                system_context=(
                    "Legacy working memory dict (no activity_id):\n"
                    "```json\n"
                    f"{json.dumps(working_memory_dict, ensure_ascii=False, indent=2)}\n"
                    "```"
                ),
            )
        return self.review_workout(
            activity_id=int(activity_id),
            thread_id=thread_id,
            run_date=run_date,
        )

    def analyze_health(
        self, history_df, yesterday_raw, thread_id: str
    ) -> str:
        """Back-compat wrapper. v1 took dataframes; v2 just calls
        review_health which pulls the same data via MCP tools."""
        return self.review_health(thread_id=thread_id)

    def consolidate_and_learn(self, thread_id: str) -> None:
        """Trigger CME background consolidation for a completed
        conversation."""
        if not self.memory_engine:
            return
        chat_list = self._chat_list_for_thread(thread_id)
        if len(chat_list) < 2:
            return
        try:
            self.memory_engine.consolidate_memory_background(
                thread_id, chat_list
            )
        except Exception as e:
            print(f"[CME] consolidation error: {e}")

    def generate_episodic_summary(
        self, working_memory_dict: dict, telemetry_df=None
    ) -> dict:
        """Compress a workout into a 50-75 word memory capsule with
        tags. Used by the activity-tab Save flow to write to episodic
        memory. Unchanged from v1 logic.

        Routed through groq-first fallback so a Garmin-sync batch (which
        fires this once per imported run) doesn't burn the gemini RPM
        budget shared with user-facing chat/action."""
        run_name = (
            (working_memory_dict.get("workout_summary") or {}).get("name")
            or "Unnamed Workout"
        )
        wm_str = json.dumps(working_memory_dict, ensure_ascii=False, indent=2)
        prompt = (
            "You are an AI Memory Summarizer. Look at this run and "
            "compress the core physiological takeaways into a dense "
            "50-75 word summary. Focus on facts: Distance, Pace, HR "
            "Drift, and how Daily Readiness (like sleep) affected it. "
            "Also, assign 2-4 broad categorization tags (e.g., "
            "'Long Run', 'Fatigue', 'VO2Max', 'Hot Weather').\n\n"
            f"Context:\n```json\n{wm_str}\n```\n\n"
            "Output EXACTLY in this JSON format, nothing else:\n"
            '{"tags": ["Tag1", "Tag2"], "summary_text": "Your dense summary here."}'
        )
        msg, _ = call_llm(
            [
                SystemMessage(content="You return strictly JSON."),
                HumanMessage(content=prompt),
            ],
            role="structured",
            fallback_chain=["groq", "gemini"],
        )
        try:
            content = (
                str(msg.content)
                .replace("```json", "")
                .replace("```", "")
                .strip()
            )
            return json.loads(content)
        except Exception as e:
            print(f"Error generating episodic memory: {e}")
            return {
                "tags": ["Analysis"],
                "summary_text": f"Completed {run_name}.",
            }
