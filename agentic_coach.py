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
from llm_provider import (
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

## Three streams (NEVER collapse them)

You have MCP tools that read the user's data. Reasoning across these
three streams correctly is the single most important thing:

  • objective — Garmin sensor measurements (HR, pace, training effect,
    HR zones against the user's RPE-named bands). Source of truth for
    what physically happened.
  • perceived — the user's RPE labels in `manual_meta`
    (category_stats, lap_categories) and free-form notes. The user's
    subjective view, recorded AFTER the run.
  • planned — Calendar-driven workout intent (Phase 2; for now this
    will be null on every run).

The MOST USEFUL coaching signal is usually the MISMATCH between
streams. E.g., Garmin says HIGHLY_IMPACTING_TEMPO + user labels
"Steady Effort" + HR didn't drift = positive fitness adaptation.
Garmin says easy run + user notes "felt awful" = recovery deficit.
Always look at all three before drawing a conclusion.

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
"""


# ---------------------------------------------------------------------------
# Default-action prompt fragments — appended after the JSON pre-fetch block.
# ---------------------------------------------------------------------------

_REVIEW_WORKOUT_INSTRUCTIONS = """### TASK: Review the workout above

Compare the three streams. Cover at minimum:
1. Objective summary — distance, time, pace, HR distribution against
   the user's RPE-named zones, training-effect, HR drift (with
   elevation context from `drift.first_third` / `drift.last_third`).
2. Perceived vs objective — does the user's category_breakdown +
   notes line up with what Garmin saw, or is there an interesting gap?
3. Recovery context — was today's readiness aligned with the effort?
4. Recommendation — what should the next 1-2 sessions look like given
   ACWR / cycle phase / recent runs?
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

Cover:
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
    ):
        self.db_path = db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

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
        # sync (streamlit, tests) or async (FastAPI handlers can also
        # submit). The loop survives until process exit.
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
                "    uv run uvicorn api_server:app --port 8765\n"
                "or override PERSONAL_COACH_API_BASE if it's on a different "
                f"host/port. Tried: {api_base} ({e!r})"
            ) from e

    # -- Loop submit helper ------------------------------------------

    def _submit(self, coro):
        """Run a coroutine on the agent's background loop and block
        until it completes. Safe to call from sync (streamlit) or sync
        FastAPI handlers (which run on threadpool workers)."""
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
            # confusing for non-api callers (Streamlit dashboard, ad-hoc
            # scripts). Probe once up front and bail with a message
            # that points the user at the fix.
            await self._require_api_reachable(api_base)
            self._mcp_client = MultiServerMCPClient({
                "personal-coach": {
                    "command": "uv",
                    "args": ["run", "python", "-m", "personal_coach_mcp"],
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

            # Use Gemini as primary chat model for agent flow. The
            # call_llm fallback chain still applies to non-tool flows
            # (consolidation, summaries, episodic summaries).
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
        summarize_and_archive and the existing UI flow."""
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
        msg, _ = call_llm([HumanMessage(content=prompt)], role="precise")
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
        memory. Unchanged from v1 logic."""
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
