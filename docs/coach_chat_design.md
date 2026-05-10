# Coach Chat — Session-based Design

**Status**: design doc, not yet implemented. Will drive PR-2 (chat UI on web).

## Mental model

A conversation between the user and the AI coach mirrors a conversation
between an athlete and a human coach. Sessions are **topic-bounded**:

> 某一天，我跟教练就一个问题展开讨论，有来有回好几轮，引经据典查数据，
> 最后得出一个结论，对话结束。过了一阵开启新一轮对话，上一轮的内容已被
> 双方内化掉了。

Three implications:

1. **The user — not the AI — decides when a session ends.** A human
   coach doesn't intuit boundaries either; the athlete chooses to wrap
   it up.
2. **Within a session, the AI sees verbatim history.** No trimming,
   no rolling summaries. A focused 5-15-turn session is small enough
   that "everything" is the right amount of context.
3. **Across sessions, the AI sees nothing direct.** When the user
   ends a session, the AI internalizes that conversation into CME
   (topics, episodes, profile updates). Future sessions retrieve that
   internalized form **on demand via tools** — the way a real coach
   says "by the way, last time you mentioned..." mid-conversation.

This kills the "rolling summary" architecture I'd half-designed. Drop
the trim-at-N logic, drop the running_summary state schema. The
session boundary IS the trim point.

---

## Data model

### Session = thread_id

Every session is one thread_id. Two states:

- **Active**: the session the user is currently appending to. There
  is at most one active session at a time, tracked client-side in
  `localStorage.current_session_id`.
- **Closed**: a session the user has ended via "End & Save". Closed
  sessions are immutable — never appended to, never replayed into the
  AI's context. They live in the checkpointer SQLite forever (small
  cost, big value for scrollback).

Thread IDs follow the pattern `coach_<utc_iso8601>` (e.g.
`coach_20260509T220103Z`). Human-readable in DB browsing,
deterministic-sortable.

### Storage

Two stores:

1. **Checkpointer SQLite** (already exists, owned by AsyncSqliteSaver):
   stores all messages of every session, keyed by thread_id. We never
   delete from this. Used as the source of truth for both AI context
   (active session only) and UI scrollback (active + closed sessions).

2. **CME** (already exists): topics, episodes, profile, pending
   clarifications. The "internalized" form. Written to during
   `summarize_and_archive`. Read at-will by the agent via tools.

No new tables. The session list is derivable from
`checkpointer.list(...)` filtered to thread_ids matching `coach_*`.
We can add a small index later if listing gets slow.

### Session index (derived, not stored)

The frontend needs an ordered list of sessions to render scrollback.
We expose:

```
GET /api/ai/sessions
  → [{thread_id, started_at, closed_at?, summary?, message_count}, ...]
```

Computed on demand from checkpointer. Cheap for the order of magnitude
we'll have (probably 1-3 sessions/day → hundreds/year).

`closed_at` is set by `summarize_and_archive` (we'll need to record
it — see below).

---

## Per-turn LLM context (within an active session)

```
1. SystemMessage  — fixed coach persona prompt
2. <full verbatim message history of THIS session>
3. HumanMessage   — new user input
```

That's it.

**No auto-CME injection.** No `retrieve_working_context` block in the
system prompt. Memory recall is a tool call the model decides to make,
not free context every turn. This is real ReAct: the agent reasons,
realizes it needs prior context, calls `recall_topics` /
`search_episodes` / `get_pending_clarifications`, gets the answer,
continues.

The system prompt instructs the agent on **when** to reach for memory:

> Before answering the FIRST user message of a session, call
> `get_pending_clarifications` to surface any unresolved questions
> the previous session(s) left for the user. If non-empty, ask those
> questions before any other coaching.
>
> Throughout the session, when the user references something from a
> prior conversation, or when you need to know whether you've covered
> a topic before, call `recall_topics` (filter by status='active') or
> `search_episodes` (with keywords from the user's message) before
> answering.

The "first user message of session" check is detectable from the
message list — no AI messages yet. The agent self-routes.

### Why this is correct (and why my earlier auto-inject design was wrong)

Auto-injecting `retrieve_working_context` every turn:
- Pollutes the prompt with topics that may be irrelevant to the
  current question
- Spends tokens on memory the agent doesn't need
- Trains the agent to expect always-present memory, then it stops
  reasoning about whether memory is needed
- Hides which retrievals were actually useful (no tool-call audit
  trail)

ReAct-driven recall:
- Agent only pays for memory when it decides to
- Tool-call log shows exactly when memory mattered
- The same MCP tool stack works for the iPhone chat, an external
  Claude Desktop client, or any future agent — single source of truth

---

## End & Save flow

User clicks **[End & Save]**:

```
1. POST /api/ai/action/summarize_and_archive
     body: { thread_id: <current_session_id> }
   server-side:
     a) summarize_thread(thread_id) → 1-2-sentence summary text
     b) memory_engine.consolidate_memory_background(thread_id, msgs)
        → extracts new_topics / topic_updates / new_episodes / conflicts
        → writes to CME tables
     c) records the close: a small "session_meta" entry (closed_at,
        summary) attached to thread_id (we'll either add a tiny SQLite
        side-table or stuff a sentinel system message at the end of
        the thread). TBD in implementation.
   response: {
     thread_id, summary, consolidation: { new_topics: [...], ... }
   }
2. UI:
   a) renders a divider in the chat view labeled
      "Archived <date> — <summary>", with the new topic / episode counts
      below it as small tags.
   b) clears localStorage.current_session_id.
   c) the chat view does NOT clear — the just-closed session messages
      stay visible above the divider, scrollable. The user wanted to
      keep being able to scroll back through a closed session in the
      same view.
   d) the input field stays empty; action buttons stay visible.
3. Next user message:
   - Generate new thread_id `coach_<new_utc>`, persist to localStorage,
     POST /api/ai/chat with that thread_id. Message and AI response
     render BELOW the most recent divider.
```

**Important**: closed sessions remain in the UI scrollback as
**rendered HTML**, but the closed messages are NEVER part of the
new session's checkpointer state. The agent has no way to see them.
If the user references something from a closed session, the agent
goes through CME (which now has internalized topics/episodes from
that session).

---

## UI rendering

### Default view of Coach tab

```
┌─────────────────────────────────────────────┐
│ Coach                                       │
│                                             │
│  [Review Workout][Plan][Health][Memory]     │  ← action pills row
│                                  [End&Save] │  ← top-right, separated
│                                             │
│  ─── archived May 8 — "讨论本周长跑pacing,  │
│       提议周日 12mi @ Steady" ─────────     │
│                                             │
│  user: 周日想试试 Steady 12mi               │
│  ai: 看你这周 ACWR 0.94...                  │
│  ...                                        │
│                                             │
│  ─── archived May 9 — "膝盖外侧紧张, 决定    │
│       减量 + 加冰敷, 下周复查" ──────────   │
│                                             │
│  user: 跑完左膝外侧又紧张了                 │
│  ai: ...                                    │
│  ...                                        │
│                                             │
│  ─── current session (active) ────────       │
│                                             │
│  user: 今天感觉怎么样？                     │
│  ai: 你 RHR 52 比基线高 2.6%...             │
│                                             │
│  ┌─────────────────────────────┬─────┐      │
│  │ ask anything...             │  →  │      │
│  └─────────────────────────────┴─────┘      │
└─────────────────────────────────────────────┘
```

- Action pills always visible (per user spec).
- **End & Save** lives in top-right corner, visually separated from
  the 4 utility action pills since it's the only action that mutates
  the session lifecycle.
- Dashed divider with date + summary + (small) topic/episode counts
  marks each session boundary.
- The most recent dashed divider with label "current session
  (active)" precedes the live thread.
- Sticky input above bottom nav.

### Pagination

PR-2 baseline: load **the most recent 3 sessions** on first render
of the Coach tab. That's typically 1 active + 2 most recent closed.
A "Load earlier" button at the top of the scrollback fetches the
next batch.

If a single session has hundreds of messages, no in-session pagination
in PR-2 — render them all. We'll revisit if it becomes a perf issue.

### Dividers vs raw separators

Each closed-session divider is its own component, not just a `<hr>`:

```tsx
<SessionDivider
  status="archived"
  closedAt="2026-05-09"
  summary="膝盖外侧紧张, 决定减量 + 加冰敷, 下周复查"
  topicsAdded={1}
  episodesAdded={2}
/>
```

The active-session divider is similar but with `status="active"` and
no summary.

---

## API surface

### Existing (kept as-is)

- `POST /api/ai/chat { thread_id, message }` — append to a session.
- `POST /api/ai/action/{name} { thread_id, ... }` — run an action,
  appending its pre-fetch + first message to the given thread_id.
- `GET /api/ai/history/{thread_id}` — already exists; returns full
  message list for one session. Frontend uses this when rendering one
  session's content.

### New for PR-2

- `GET /api/ai/sessions?limit=3&before=<thread_id>` — list sessions
  in reverse-chronological order, paginated. Response per item:
  `{ thread_id, started_at, closed_at?, summary?, message_count }`.
  Computed by walking the checkpointer.

### Behavior changes

- Default actions (review_workout / make_plan / review_health /
  follow_up_memory): in PR-1 they auto-generated a thread_id like
  `review_workout_<id>`. **Change**: now they require thread_id from
  the body (the active session). If body.thread_id is null, the
  endpoint returns an error or starts a fresh session and returns its
  id; client-side the frontend ensures it always passes the current
  session's id.
- `summarize_and_archive`: existing endpoint stays. Internally also
  records `closed_at` + `summary` for `/api/ai/sessions` to return.

---

## Default actions × session model

All 4 utility actions append to the **current active session**. They
generate a synthetic first user message + pre-fetch the relevant MCP
tools in parallel, inject pre-fetch as `extra_system_context`, then
append a `HumanMessage` with the action's prompt — exactly like
PR-1's `_action_turn`. The only change: thread_id comes from the
request body, not auto-generated.

User can mix actions in one session:
1. Click [Review Workout] → AI analyzes 5/2's 10mi
2. Click [Plan] → AI proposes 3-5 sessions
3. Free-text: "我周三能跑 LT 吗?"
4. AI reasons across the workout review + the plan + the question
5. Click [End & Save] → all of that gets archived as one session

The AI gets a coherent context of "everything we discussed today",
not 4 disconnected analyses.

### Pre-fetched data persistence in thread

PR-1 stuffs pre-fetched JSON as a `SystemMessage` in
`_action_turn`. With sessions: that SystemMessage persists for the
lifetime of the active session. Two consequences:

- **Good**: subsequent turns within the session can reference the
  earlier pre-fetched data without the agent re-calling tools.
- **Bad-ish**: the SystemMessage stays in the prompt for every later
  turn, eating tokens.

For typical sessions this is fine (max ~25-50KB pre-fetched data
across mixed actions). If long sessions cause real bloat we'll switch
to ToolMessage-based pre-fetch or unset the SystemMessage after a
turn or two — defer until measured.

---

## System prompt updates

PR-1's prompt assumed the agent might run "as a default action with
pre-fetched JSON in system context". With session model, that's still
true, but we need additional language about memory recall:

```
You are a single-persona coach (running + recovery). Use MCP tools
to read the user's data on demand.

THREE STREAMS — never collapse them:
  • objective (Garmin sensors)
  • perceived (manual_meta RPE labels)
  • planned (calendar; null in Phase 1)

CONVERSATION SESSION RULES:
- This conversation is one session. The user controls when it ends
  (via "End & Save"). You do not see content from prior closed
  sessions directly.
- On the FIRST user message of a session (no prior AI messages),
  call `get_pending_clarifications` once. If it returns any items,
  ask the user those questions BEFORE any other coaching.
- When the user mentions a past situation, an old goal, an injury
  you'd plausibly already know about, or whenever you'd want to
  reference prior coaching, call `recall_topics(active)` or
  `search_episodes(keywords)` to load the relevant memory. Do not
  make up history.

WHEN A DEFAULT ACTION HAS PRE-FETCHED TOOL RESULTS into the system
context, don't re-call those tools — use the data that's already
there. Call additional tools only for what's missing.
```

---

## Edge cases

**Empty new session, user lands on Coach tab fresh:**
- localStorage has no current_session_id (or all sessions closed).
- UI shows action pills + empty input + the most recent ~3 closed
  sessions in scrollback (with their dividers).
- User typing or clicking an action triggers session creation
  (generate thread_id, persist).

**User refreshes mid-session:**
- localStorage still has current_session_id.
- UI loads `/api/ai/history/<current_session_id>` + last 2-3 closed.
- Scrollback continues exactly as it was.

**User opens app days after last session, never explicitly ended:**
- Per user choice (Q1 = A): the most recent unclosed session is
  resumed automatically. They scroll up, see the prior conversation,
  and either continue OR click [End & Save] to archive it before
  starting fresh.

**User clicks [End & Save] on an empty session:**
- No-op safeguard server-side: if `len(messages) <= 2`, skip the
  consolidation, just clear localStorage and don't write a divider.

**`summarize_and_archive` fails partway through (LLM 500, etc.):**
- Returned error to UI; toast shows "Archive failed: <reason>";
  localStorage is NOT cleared; user can retry.
- Idempotency: hitting summarize_and_archive twice on the same
  thread should NOT double-write topics/episodes. CME's
  consolidation already has duplicate-detection (embedding match),
  so this is mostly safe; we should additionally short-circuit if
  the thread already has a closed_at stamp.

**Tool error inside agent loop:**
- LangGraph already catches tool errors; agent gets
  `ToolException(...)` content and decides what to do. If repeated
  failures, agent gives up and replies with what it knows.

---

## Out of scope (future PRs)

- **Detailed history view**: searching past sessions, filtering by
  date / topic / etc. PR-3 or later.
- **Streaming responses**: SSE / token-by-token rendering. Defer.
- **Cross-session resume**: "continue the discussion from May 8" —
  would require selectively replaying a closed session. Out of scope;
  for now CME retrieval covers it.
- **Removing build_agent_working_memory**: deprecated in
  mcp_tools_design.md; remove when no caller remains. Not blocking.
- **Removing `coach`/`doctor` references in api inputs**: PR-1 left
  the `agent` field accepted-but-ignored on `ChatInput`. Phase out
  in a separate cleanup PR.

---

## Implementation checklist (for PR-2)

When user OK's this design, the coding work is:

### Backend

1. `agentic_coach.py`:
   - Drop the auto-generated thread_id default in
     `review_workout` / `make_plan` / `review_health` /
     `follow_up_memory`. They REQUIRE `thread_id` now.
   - System prompt updated with session rules + memory recall
     instructions.
   - `summarize_and_archive` records `closed_at` + `summary`. Tiny
     side-table or sentinel system message — pick during impl.
   - Idempotency check: if thread already has closed_at, skip.

2. `api_server.py`:
   - `POST /api/ai/action/{name}`: require `thread_id` in body. If
     missing, generate one and return it (and let client persist).
   - New `GET /api/ai/sessions?limit=N&before=<thread_id>`.

3. No CME-injection in pre_model_hook. (We're not adding one — agent
   uses tools only.)

### Frontend (web/)

1. New page `/coach` + `<CoachTab>` component.
2. New nav item with `MessageCircle` (or similar) icon, between
   Training and Setup. Bottom nav becomes 5 items.
3. Components:
   - `<CoachThread>` — orchestrates rendering active + closed sessions.
   - `<SessionDivider>` — dashed line + label + summary + tags.
   - `<MessageBubble>` — user vs ai styling.
   - `<ActionPills>` — 4 utility pills + End & Save button.
   - `<ChatInput>` — sticky bottom input.
4. `localStorage` integration for `current_session_id`.
5. API client helpers for `/api/ai/chat`, `/api/ai/action/{name}`,
   `/api/ai/history/{thread_id}`, `/api/ai/sessions`.
6. Spinner UI during action / chat in-flight.
7. Toast component for archive results.

### Tests / verification

- Smoke: send message in fresh session, verify thread_id created
  and appended.
- Smoke: click [End & Save] on a 3-message session, verify divider
  renders, localStorage cleared, summary toast shows.
- Smoke: refresh after [End & Save], verify scrollback shows the
  closed session above empty active state.
- Smoke: agent calls `get_pending_clarifications` on first message
  of a fresh session (verify in api log).
- Smoke: agent calls `search_episodes` when the user references a
  past situation (verify the tool log contains the call).
- Token test: a 10-turn session's 11th turn shows reasonable token
  usage (no auto-CME injection).
