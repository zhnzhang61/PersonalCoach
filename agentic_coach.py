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

You have MCP tools that read the user's data. Three streams to keep
strictly distinguished in your reasoning:

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

When the user clicks a default action (review_workout, make_plan,
etc.), the action wrapper has already pre-fetched the most relevant
tools in parallel; you'll see their results as a JSON block in this
system prompt. You can call more tools if you need them — but don't
re-call ones already pre-fetched.

When you respond:
- Use Markdown with clear sections for analysis / recommendations.
- Reference specific numbers from tool outputs to ground your claims.
- If the user has unresolved CME clarifications, ask them BEFORE
  giving advice.
- Never fabricate. If a tool returned null/empty, say so.
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
        thread_id: str | None = None,
        run_date: str | None = None,
        user_message: str | None = None,
    ) -> str:
        thread_id = thread_id or f"review_workout_{activity_id}"
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
        thread_id: str | None = None,
        user_message: str | None = None,
    ) -> str:
        thread_id = thread_id or f"make_plan_{date.today().isoformat()}"
        return self._submit(self._action_turn(
            plan=_prefetch_make_plan(),
            instructions=_MAKE_PLAN_INSTRUCTIONS,
            user_input=user_message or "请帮我安排接下来 3-5 次训练。",
            thread_id=thread_id,
        ))

    def review_health(
        self,
        thread_id: str | None = None,
        user_message: str | None = None,
    ) -> str:
        thread_id = thread_id or f"review_health_{date.today().isoformat()}"
        return self._submit(self._action_turn(
            plan=_prefetch_review_health(),
            instructions=_REVIEW_HEALTH_INSTRUCTIONS,
            user_input=user_message or "请评估我今天的恢复状态。",
            thread_id=thread_id,
        ))

    def follow_up_memory(
        self,
        thread_id: str | None = None,
        user_message: str | None = None,
    ) -> str:
        thread_id = thread_id or f"follow_up_memory_{date.today().isoformat()}"
        return self._submit(self._action_turn(
            plan=_prefetch_follow_up_memory(),
            instructions=_FOLLOW_UP_MEMORY_INSTRUCTIONS,
            user_input=user_message or "我们之前聊过哪些没结束的话题？",
            thread_id=thread_id,
        ))

    def summarize_and_archive(self, thread_id: str) -> dict[str, Any]:
        """Post-conversation: ask the LLM to summarize the thread,
        then run CME's consolidation. Returns the summary + whatever
        consolidation reported (new topics / episodes / decisions)."""
        summary = self.summarize_thread(thread_id)
        consolidation: dict[str, Any] | None = None
        if self.memory_engine:
            try:
                consolidation = self.memory_engine.consolidate_memory_background(
                    thread_id, self._chat_list_for_thread(thread_id)
                )
            except Exception as e:
                consolidation = {"error": str(e)}
        return {
            "thread_id": thread_id,
            "summary": summary,
            "consolidation": consolidation,
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
