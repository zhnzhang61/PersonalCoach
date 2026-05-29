# PersonalCoach — Project Guide

**English** · [中文](PROJECT_GUIDE.zh.md)

Single source of truth for how this project is built and what's left to
do. The **only** doc — supersedes the older scattered set (architecture,
coach_brain_design, coach_chat_design, mcp_tools_design, IMPROVEMENTS,
CI, langsmith-setup, PROMPT_CHANGELOG) which were point-in-time notes
that drifted as the code moved. Reflects the **current** state
(2026-05-28).

> Prompt changelog is now [§3.4.3](#343-prompt-versioning); LangSmith
> setup is [§3.4.4](#344-observability--traces--langsmith); Garmin
> token setup (the 429 workaround that used to live in the README) is
> [§3.2](#32-authentication).

---

## Index

- [1. Overview](#1-overview) — what the app is, the big picture diagram
- [2. Frontend](#2-frontend) — Next.js web app, 5 tabs
- [3. Backend](#3-backend)
  - [3.1 Data processor](#31-data-processor) — the data layer
  - [3.2 Authentication](#32-authentication) — Garmin + Google OAuth
  - [3.3 MCP tools](#33-mcp-tools) — what the agent can call
  - [3.4 AI / Coach](#34-ai--coach)
    - [3.4.1 Coach brain](#341-coach-brain--memory-models-input-streams) — memory, models, the 4 input streams **(long)**
    - [3.4.2 Coach chat](#342-coach-chat--session-design) — session-bounded chat
    - [3.4.3 Prompt versioning](#343-prompt-versioning)
    - [3.4.4 Observability](#344-observability--traces--langsmith) — JSONL traces + LangSmith
    - [3.4.5 Planned — profile + cycle config capture](#345-planned--athlete-profile-a--cycle-config-b-capture) — A/B intake into the CME
- [4. Engineering debt](#4-engineering-debt) — CI, tests, tracing, repo reorg, open gaps **(longest)**
- [5. Appendix](#5-appendix) — storage tour, provider routing, doc history

---

## 1. Overview

Single-user, iPhone-first AI running coach. One human (the owner)
interacts with one always-on coach agent that reasons over their
Garmin sensor data, recovery metrics, planned calendar, subjective
check-ins, and an accumulated long-term memory of past topics,
episodes, and statistical models of how the user's body behaves.

The project is two halves:

- **Frontend** — `web/`, a Next.js app, 5 tabs.
- **Backend** — `backend/`, a FastAPI process plus an MCP subprocess.
  Decomposes into: data processor, authentication, MCP tools, and the
  AI/coach layer (memory + models + prompt + LangChain + LangSmith).

```mermaid
graph TB
    classDef ext fill:#fff3e0,stroke:#e65100,color:#000
    classDef db fill:#e8f5e9,stroke:#1b5e20,color:#000
    classDef proc fill:#e3f2fd,stroke:#0d47a1,color:#000
    classDef frontend fill:#fce4ec,stroke:#880e4f,color:#000

    subgraph Browser["📱 Browser (iPhone / desktop)"]
        WebApp["<b>web/</b> — Next.js 16<br/>5 tabs: Health · Activity ·<br/>Training · Coach · Setup"]:::frontend
    end

    subgraph APIProc["🖥️ api_server.py — :8765 (FastAPI)"]
        API["<b>api_server.py</b><br/>~85 HTTP endpoints<br/>+ Garmin/Google OAuth"]:::proc
        AC["<b>agentic_coach.py</b><br/>LangGraph create_react_agent<br/>SSE streaming · session threads"]:::proc
        CME["<b>cognitive_memory_engine.py</b><br/>topics · episodes · models ·<br/>decisions · pending"]:::proc
        DP["<b>data_processor.py</b><br/>RunActivity / ManualActivity<br/>.fit → derived JSON"]:::proc
        LP["<b>llm_provider.py</b><br/>provider routing + fallback"]:::proc
        SEED["<b>seed_models.py</b><br/>5 stat-derived model refits"]:::proc
        TRACE["<b>trace_logger.py</b> + <b>langsmith_setup.py</b>"]:::proc
    end

    subgraph MCPProc["🔧 MCP subprocess (stdio)"]
        MCP["<b>personal_coach_mcp.py</b><br/>~28 tools wrapping api_server"]:::proc
    end

    Gemini["☁️ Gemini API<br/>3.1-flash-lite + embedding-2"]:::ext
    Groq["☁️ Groq API<br/>llama-3.3-70b"]:::ext
    Garmin["☁️ Garmin Connect"]:::ext
    GCal["☁️ Google Calendar (R/W)"]:::ext
    LS["☁️ LangSmith (opt-in)"]:::ext

    ChatDB[("📂 chat_memory.db<br/>checkpoints · session_meta")]:::db
    CMEDB[("📂 cognition.db<br/>topics · episodes · models ·<br/>topic_decisions · pending")]:::db
    FS[("📂 data/<br/>garmin dumps · derived/ ·<br/>manual_inputs/ · traces/")]:::db

    WebApp -- "/api/* · /oauth/* proxy" --> API
    API --> AC & CME & DP & SEED
    AC -- "create_react_agent (SSE)" --> Gemini
    AC -- "tool calls" --> MCP
    AC -- "checkpoints" --> ChatDB
    AC -. "auto-trace when wired" .-> LS
    LP --> Gemini & Groq
    CME --> CMEDB & LP
    DP --> FS
    SEED --> CMEDB
    MCP -. "HTTP back to FastAPI" .-> API
    API -. "spawn -m backend.garmin_sync" .-> Garmin
    API --> GCal
```

**Status:** Phase 0 → Phase 3 of the coach-brain roadmap complete. All
four agent input streams live; 5 stat-derived models in the store;
711 backend tests passing. See [§3.4.1](#341-coach-brain--memory-models-input-streams)
for the roadmap detail and [§4](#4-engineering-debt) for what's left.

---

## 2. Frontend

`web/` — Next.js 16 (note: this is a newer Next.js with breaking
changes from older versions; read `node_modules/next/dist/docs/`
before touching routing/server conventions). React Query for data
fetching. Tailwind. iPhone-first layout.

**Five tabs:**

| Tab | Route | What's there |
|---|---|---|
| Health | `/` | Today's check-in card, context-events card, readiness, recovery/sleep charts |
| Activity | `/activity` | Run list + per-run detail (`/activity/[id]`) with map / telemetry / laps + "Ask AI about this run" |
| Training | `/training` | Cycle overview, monthly chart, plan calendar, upcoming planned workouts (editable) |
| Coach | `/coach` | Session-based chat thread (streaming), action pills, day dividers |
| Setup | `/setup` | Garmin / Google sign-in, sync controls |

**Key client modules (`web/lib/`):**
- `api.ts` — `apiGet/Post/Put/Delete` + `streamSSE` (the SSE frame parser for streaming chat)
- `hooks/use-coach-session.ts` — localStorage-backed current `thread_id`
- `coach-errors.ts` — classify provider rate-limit / proxy timeouts → friendly Chinese messages + retry hints
- `todays-read.ts` — per-day cache for the "Today's Read" sentence
- `format.ts` — date/pace/distance formatters
- `types.ts` — all TypeScript interfaces (mirrors backend response shapes)

**Per-card invariant (hard-won — caught in 3 separate reviews):** every
React Query `useMutation` must render its `isError`; every `useQuery`
must branch `isError` distinctly from the empty state; modals owning a
mutation need an `isMounted` guard. See
`feedback_no_silent_mutation_errors.md` in memory.

---

## 3. Backend

`backend/` Python package. FastAPI process (`api_server.py`) is the
single source of truth — everything else that needs data calls HTTP
to it rather than constructing a `DataProcessor` directly (avoids two
live instances racing on the same JSON files).

### 3.1 Data processor

**`data_processor.py`** — the pure data layer. No LLM, no HTTP; reads
`data/*.fit` + Garmin JSON dumps + manual inputs, normalizes into typed
shapes.

- `RunActivity` / `ManualActivity` dataclasses — typed views over raw
  Garmin/manual records (pace, HR, stride, elevation, surface bucket).
- `get_health_stats()` — the daily health ledger (sleep_hours, rhr,
  hrv, stress, run_miles) — feeds HRV/sleep/volume models.
- `compute_route_profile(activity_id)` — grade-band distribution +
  climb/loss from telemetry (P5).
- CRUD for: check-ins, planned workouts, training blocks, manual
  activities, user HR zones.
- **Rule:** all shaping/aggregation lives here; the dashboard/UI only
  calls functions and renders. Data is shaped for BOTH UI and AI —
  numeric + pre-formatted fields side by side, self-describing units.

### 3.2 Authentication

Two external auth flows; **the app never handles passwords** — the user
signs in directly, the app stores the resulting tokens.

- **Garmin** — `backend/garmin_ticket_login.py` exchanges a manually-
  obtained Service Ticket for a long-lived garth OAuth2 token (the
  "429 workaround", see setup below). `backend/garmin_sync.py` then
  uses it to pull activities + daily health, spawned via
  `python -m backend.garmin_sync` from `POST /api/sync/garmin`.
- **Google Calendar** — `google_calendar.py`. OAuth flow at
  `/oauth/google/start` → `/callback`. Scope is `calendar.events`
  (read+write; we write AI-planned workouts to the user's calendar).
  Incremental-grant via `include_granted_scopes=true`. Token in
  `data/oauth/google_token.json`.
  - **Gotcha (cost us a debugging session):** don't list both
    `calendar.readonly` AND `calendar.events` — Google consolidates
    them and returns just `events`, which trips
    `oauthlib`'s strict scope check → callback errors → silent
    "not connected". List `calendar.events` only.

##### Garmin token setup (the 429 workaround)

Garmin now fronts login with strict Cloudflare anti-bot. Automated
browser login (Playwright etc.) reliably trips `HTTP 429 Too Many
Requests` + infinite-CAPTCHA loops. The workaround: grab a one-time
**Service Ticket** manually in a browser, then exchange it immediately
with the project script for a durable garth `OAuth2` token.

> **Never** commit a Service Ticket, password, or token to git /
> screenshots. They're short-lived secrets.

**Step 1 — grab the Service Ticket (`ST-…`)** (one-time, expires in
under a minute — run step 2 immediately after):
1. Open a fresh **incognito** browser window. F12 → **Network** tab →
   check **Preserve log**.
2. Visit the mobile SSO login URL:
   ```
   https://sso.garmin.com/mobile/sso/en_US/sign-in?clientId=GCM_ANDROID_DARK&service=https://mobile.integration.garmin.com/gcm/android
   ```
3. Log in normally (clear any CAPTCHA by hand). After success the page
   redirects to a "site can't be reached" — **that's expected**.
4. Copy the **whole redirect URL** from the address bar, or just the
   `ticket=ST-…-sso` part.

**Step 2 — exchange + write garth** (from repo root):
```bash
# A: pass the redirect URL or ST string directly (fastest)
uv run python -m backend.garmin_ticket_login --url "https://...ticket=ST-..."
# or
uv run python -m backend.garmin_ticket_login --ticket "ST-....-sso"

# B: no args — paste the redirect URL when prompted
uv run python -m backend.garmin_ticket_login

# C: auto-open the login page, then paste the address-bar URL
uv run python -m backend.garmin_ticket_login --open-browser
```
The script exchanges the ST → a long-lived session
(`~/.local/share/pirate-garmin/native-oauth2.json`, override with
`--app-dir`) and writes the DI token to `~/.garth/oauth2_token.json`.

Useful flags:
- `--compat` — also write `oauth1_token.json` + `domain_profile.json`
  stubs for older `garminconnect` that still checks OAuth1.
- `--run-sync` — run `python -m backend.garmin_sync` on success.

```bash
# exchange + compat stubs + pull data in one go
uv run python -m backend.garmin_ticket_login --url "$PASTED_URL" --compat --run-sync
```

**Already have a `native-oauth2.json`?** Just migrate it into garth:
```bash
uv run python -m scripts.migrate_garmin_token
```
(`scripts/migrate_garmin_token.py` shares the migration logic with
`backend/garmin_ticket_login.py`.)

**Recovery:** don't hand-edit `.venv` to hard-code tickets — `uv sync`
overwrites it. If you ever did, restore upstream behavior with
`uv sync --reinstall-package pirate-garmin`.

### 3.3 MCP tools

**`personal_coach_mcp.py`** — MCP server (`@mcp.tool()` decorators),
spawned as a stdio subprocess by `agentic_coach._ensure_agent`. Every
tool is a thin HTTP wrapper around `api_server` (keeps one
`DataProcessor`, avoids races). ~28 tools, grouped:

| Group | Tools |
|---|---|
| Profile / readiness | `get_athlete_profile`, `get_readiness`, `get_training_load`, `get_recent_checkins` |
| Runs | `list_runs`, `get_run_detail`, `get_run_telemetry`, `get_run_weather`, `get_run_route_profile`, `get_plan_actual_deviation` |
| Training cycle | `list_blocks`, `get_cycle_stats`, `get_monthly_stats` |
| Calendar / planned | `get_calendar_events`, `get_workout_plan`, `get_planned_workouts`, `propose_workout_plan` |
| External context | `get_external_events` |
| Manual activities | `list_manual_activities`, `get_manual_activity` |
| Memory (CME) | `recall_topics`, `search_episodes`, `get_pending_clarifications`, `get_model`, `list_models`, `propose_model_from_topic`, `list_pending_decisions`, `resolve_decision` |

**Design principle (from IMPROVEMENTS §2, now enforced):** the MCP
layer does *projection*, not raw passthrough. Garmin's interpretive
labels (`trainingEffectLabel`, `vO2MaxValue`, …) are filtered at this
boundary, not by prompt rules. The agent sees self-describing keys
(`medium_term_hr_effort_map`, not raw `hr_zones`).

### 3.4 AI / Coach

The intelligence layer. `agentic_coach.py` owns the agent
(`create_react_agent` from LangGraph, Gemini 3.1-flash-lite pinned for
tool-calling, SSE streaming). `llm_provider.py` is the ONLY module
allowed to call LLMs (provider routing + fallback chains:
gemini → groq → local).

#### 3.4.1 Coach brain — memory, models, input streams

*(This is the long section — it's the coach-brain roadmap, the main
build effort of Phases 1–3.)*

##### The four input streams (never collapse them)

The coaching signal is the **mismatch** between streams. Never assume
two streams agree even when their vocabulary matches.

| Stream | Source | What it is |
|---|---|---|
| **objective** | Garmin sensors via `data_processor` (+ weather + route/terrain) | Raw measurements: HR, pace, distance, drift, grade. Garmin's interpretive labels are noise, filtered at the MCP layer. |
| **perceived** | `daily_checkins.json` (sleep/soreness/mood/motivation 0-5) + `manual_meta` RPE labels per run + medium-term HR↔effort map | What the user *feels* / *reports*. Post-hoc RPE is NOT planned intent. |
| **planned** | Google-Cal-synced workouts (`planned_workouts.json` + `cal_event_id`) + plan-vs-actual deviation | What was *supposed* to happen. |
| **external** | `travel` / `illness` / `life_stress` episodes (CME) | Context the sensors can't see — why a number means something or is a known degraded-data day. |

##### Cognitive Memory Engine (CME) — `cognitive_memory_engine.py`

Long-term memory in `cognition.db`. Six tables:

- **topics** — state machine (Open / Testing / Resolved / Conflicting)
  + `working_conclusion` + `open_question` + `related_models`.
- **episodes** — 5W1H + `lesson_learned` + event timestamp. Includes
  the external-context types (`travel`/`illness`/`life_stress`,
  `daily_checkin`).
- **models** — parameterized observations about the user (the pattern
  store; see below). Parallel to episodes.
- **topic_episode_links** — junction (canonical link source).
- **pending_clarifications** — the agent's question queue.
- **topic_decisions** — audit log of LLM proposals (new_model /
  merge / conflict).

`consolidate_memory_background` is the LLM call that, on session close,
extracts `{new_topics, topic_updates, new_episodes, conflicts}` from a
closed chat thread and upserts them (embedding-matched against existing
topics).

##### The model (pattern) store

Models characterize how the user's body behaves — what makes a coach
feel like a coach vs a spreadsheet. Two derivation paths:

- **stat-derived** (`seed_models.py`) — computed from raw data, refit
  on demand via `POST /api/memory/models/refit/{key}` (registry-driven;
  a future nightly cron will iterate `REFIT_REGISTRY`). 5 shipped:
  - `recovery.hrv_14d_baseline` (mean_std) — rolling 14-day HRV.
  - `aerobic.decoupling_baseline` (mean_std) — pace/HR drift on easy
    runs. Negative = HR holds steady / drops; healthy.
  - `cadence.baseline` (mean_std) — steady-state easy-run cadence.
  - `sleep.debt_14d` (mean_std) — 14-day sleep + total debt vs 8h
    target + nights below target.
  - `cycle.weekly_volume_diff` (linear_trend) — slope of weekly
    mileage over 6 completed weeks (current week dropped to avoid a
    partial-week downward bias).
- **llm-derived** (P2 pipeline) — `propose_model_from_topic` asks an
  LLM "is this topic parametrically generalizable?", parks a
  `kind='new_model'` decision; the user confirms in chat (no separate
  UI page); `resolve_decision` creates the model + links it to the
  topic.

The agent reads models via `get_model` / `list_models`. Real chat
testing (2026-05-28) confirmed the agent autonomously consults all 5
baselines and quotes exact numbers — no prompt nudge needed.

##### Roadmap status (Phases 0–3, all done)

| Phase / PR | What |
|---|---|
| Phase 0 (felt-pain) | multi-day timeline fix, tracing scaffold, SSE streaming |
| Phase 1 | foundation before the model pipeline |
| P1 | `models` table scaffold + CRUD + seed HRV baseline |
| P2 | episode → model LLM proposal pipeline (chat-driven confirm) |
| P3 | daily check-in widget (perceived stream) |
| P4a | planned workouts → Google Cal write loop (silent reminders) |
| P4b | planned-workout edit UI + plan-vs-actual deviation tool |
| P5 | external context (route-profile tool + travel/illness/life-stress) |
| P6 batch 1 | aerobic decoupling + cadence baselines |
| P6 batch 2 | sleep debt + weekly volume trend baselines |
| E (Phase 3) | LangSmith tracing wiring + observability endpoint |

#### 3.4.2 Coach chat — session design

*(Implemented. This was originally the `coach_chat_design.md` design;
it's built now.)*

A conversation mirrors an athlete ↔ human-coach exchange. Sessions are
**topic-bounded**:

1. **The user — not the AI — ends a session.** End & Save triggers
   summarize + `consolidate_memory_background`.
2. **Within a session the AI sees verbatim history.** No rolling
   summary; a focused 5–15-turn session is small enough that
   "everything" is the right context. The session boundary IS the trim
   point.
3. **Across sessions the AI sees nothing direct** — it retrieves the
   internalized form (topics/episodes/models) on demand via tools.

Built surface: streaming chat (`/api/ai/chat/stream`, SSE), 5 actions
(`review_workout`, `review_health`, `make_plan`, `follow_up_memory`,
`summarize_and_archive` — note `follow_up_memory`'s UI label is
"Memory" with a brain icon), session list + delete, multi-day
DayDivider in the thread, per-message timestamps. Pre-fetch plans
hydrate action turns with parallel MCP calls injected as system
context.

#### 3.4.3 Prompt versioning

`PROMPT_VERSION` constant in `agentic_coach.py` (currently **v8**).
The system prompt is built per-turn by `_build_prompt(state)` — it
prepends today's date (tz-aware, honors `PERSONAL_COACH_TZ`) in front
of the static persona so the agent never schedules workouts in the
past. The trace `prompt_hash` is computed against
`f"{_HEADER_TEMPLATE.format(sentinel_date)}\n\n{_SYSTEM_PROMPT}"` — so
the daily-changing date doesn't churn the hash, but any wrapper or
persona edit does. Version + hash land in every trace row, so "which
prompt produced this turn?" is answerable without guessing.

**Contract — how to bump:** any edit to LLM-visible system text
(`_SYSTEM_PROMPT` OR the `_HEADER_TEMPLATE` wrapper) must, in the SAME
commit: (1) bump `PROMPT_VERSION`, (2) add a row to the changelog
below. Reviewers reject a prompt edit that doesn't bump the version —
otherwise traces stamp the old version onto a prompt that's actually
different.

**Reading traces by version:**
```bash
# all turns on v8 today
jq -c 'select(.prompt_version == "v8")' data/traces/$(date +%F).jsonl
# drift check — version label vs actual content hash
jq -c 'select(.prompt_version == "v8" and .prompt_hash != "<current>")' \
  data/traces/$(date +%F).jsonl
```
The canonical hash is logged at AgenticCoach init (grep startup output).

##### Prompt changelog

| Version | Date | What changed | PR |
|---|---|---|---|
| **v8** | 2026-05-27 | Per-turn date-header wrapper (`_HEADER_TEMPLATE`) in front of `_SYSTEM_PROMPT`. Pins "Today is YYYY-MM-DD (Weekday)" + relative-time directive in English + Chinese (`今天 / 明天 / 后天 / 这周`) + "never schedule in the past". Today via `datetime.now(_user_tz()).date()` (honors `PERSONAL_COACH_TZ`, falls back to process-local). Hash now covers wrapper + persona with a sentinel date. Fixed a real bug: agent wrote a "今天 easy run" to 2026-05-14 with no date anchor. | [#84](https://github.com/zhnzhang61/PersonalCoach/pull/84) |
| v7 | 2026-05-13 | Codex P2: explicit list of which Garmin per-run interpretive labels are filtered at the MCP boundary (`aerobicTrainingEffect`, `anaerobicTrainingEffect`, `activityTrainingLoad`, `trainingEffectLabel`, `aerobicTrainingEffectMessage`) AND which long-term baselines are NOT (`vo2max_running`, `lactate_threshold_hr`, `lactate_threshold_pace`). Replaced v6's vague "you won't see them". | [#68](https://github.com/zhnzhang61/PersonalCoach/pull/68) |
| v6 | 2026-05-13 | Removed the "SILENTLY IGNORE…" block + forbidden-field bullet list — those fields are now filtered at the MCP data layer (see §4.2), so prompt rules aren't load-bearing. Renamed `hr_zones` → `medium_term_hr_effort_map` in the prompt to match the projected key. | [#68](https://github.com/zhnzhang61/PersonalCoach/pull/68) |
| ≤ v5 | pre-2026-05-13 | History before structured tracing. ~6 iterations existed (session-based Coach, Gemini 3.1 Flash Lite swap, archive divider) but exact diffs weren't tracked. For a trace row with `prompt_version ≤ v5`, the content hash is the source of truth. | — |

#### 3.4.4 Observability — traces + LangSmith

Two layers:

- **Local JSONL** (`trace_logger.py`) — one row per turn to
  `data/traces/YYYY-MM-DD.jsonl`: turn_id, prompt_version, prompt_hash,
  user_input, final_answer, duration_ms, error. Source-of-truth audit
  log, never leaves the machine. Tracing never raises into the caller.
  Does NOT capture per-tool calls or token counts — that's the gap
  LangSmith fills.
- **LangSmith** (`langsmith_setup.py`, opt-in) — when env vars are
  set, langchain auto-instruments the full tool-call + LLM tree
  (per-tool inputs/outputs, token counts, latency, cross-prompt-version
  diffs). `GET /api/admin/observability` reports status (never echoes
  the key).

##### LangSmith setup

Opt-in. Free tier is 5,000 traces/month — single-user dev does maybe
50–200/day, comfortably under.

1. **Sign up** at <https://smith.langchain.com> → Settings → API Keys →
   Create. Key starts with `lsv2_` / `ls__`. Treat as a secret.
2. **Set env vars** (shell rc or `.envrc`):
   ```bash
   export LANGSMITH_TRACING=true              # must be lowercase "true"
   export LANGSMITH_API_KEY=lsv2_pt_...        # from step 1
   export LANGSMITH_PROJECT=personalcoach      # optional, organizes traces
   ```
   - **`LANGSMITH_TRACING` must be the literal lowercase `"true"`.**
     langsmith does a strict `var_result == "true"` check; `1` / `yes`
     / `on` / `True` are all REJECTED. The startup log line flags this
     misconfiguration explicitly.
   - **Legacy `LANGCHAIN_*` names also work** (`LANGCHAIN_TRACING_V2` /
     `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT`) — langsmith reads both
     namespaces. The `*_source` fields in the status payload tell you
     which one was actually picked up.
3. **Restart the backend.** One startup line tells you the state:
   ```
   LangSmith tracing: ON (project=personalcoach, source=LANGSMITH_TRACING, endpoint=https://api.smith.langchain.com)
   ```
   Four states:

   | Startup line | Meaning |
   |---|---|
   | `OFF (no LANGSMITH_TRACING / LANGCHAIN_TRACING set)` | env var didn't reach the process (forgot to restart shell) |
   | `MISCONFIGURED — flag is 'X' but langsmith requires lowercase 'true'` | typo (`=1`, `=True`) — fix to lowercase `true` |
   | `MISCONFIGURED — tracing flag is set but no API key found` | flag right, no key in either namespace — spans 401 silently |
   | `ON (...)` | flowing |

4. **Verify:**
   ```bash
   curl -s http://localhost:8765/api/admin/observability | python3 -m json.tool
   # → {tracing_enabled: true, tracing_flag_source: "LANGSMITH_TRACING",
   #    api_key_set: true, project: "personalcoach", ...}
   ```
   `api_key_set: true` confirms the key was found (value never echoed).
   Then send a `/coach` message and refresh the LangSmith project — the
   full run (prompt → tool calls → answer) shows within ~10s.

5. **Turn off:** `unset LANGSMITH_TRACING` (or `=false`) + restart. JSONL
   keeps working; you just lose the hosted UI.

**Privacy:** LangSmith stores prompts + completions on their infra —
review their data policy before enabling with sensitive data. The API
key is env-only (never in a tracked file). The `/api/admin/observability`
endpoint returns project + endpoint but never the key.

#### 3.4.5 Planned — athlete profile (A) + cycle config (B) capture

> **Status: designed, not built.** This subsection is the spec the
> implementation PRs will follow. Everything in §3.4.1–3.4.4 is the
> *continuous* stream (C) — the eyes. This is the missing *intake*: the
> enrollment form (A) and this cycle's battle plan (B).

##### Why

Frame it as a human coach taking on a new athlete. A coach asks ~25
questions before writing a single workout, and those questions fall into
three natures:

- **A — static profile.** Asked once; rarely changes. "Who are you."
- **B — per-cycle config.** Re-asked each training cycle; fixed within a
  cycle. "How do we lay out *this* campaign."
- **C — continuous.** Re-sampled constantly. "Today's / this week's real
  state."

Almost everything built in Phases 0–3 feeds **C** (decoupling / cadence /
sleep / volume baselines = "fitness is moving, keep recomputing"). But the
questions a coach asks *first* — A and B — have no structured home: goal
date lives in Calendar, everything else is scattered in chat or simply
never captured. So the agent has sharp eyes (weekly state) but no
enrollment form (A) and no campaign map (B). This feature gives it both,
stored in the CME, retrievable on demand, with the agent judging whether
each slot is filled *and specific enough* and following up when it isn't.

##### The intake (becomes `PROFILE_SLOTS` / `CYCLE_SLOTS`)

**A — static profile (8 slots, ranked by importance):**

1. **`injury_history`** — past injuries (stress fracture, ITB, plantar,
   Achilles), surgeries. The #1 safety gate; caps volume and ramp rate.
   Append-only.
2. **`medical`** — conditions (asthma / cardiac / anemia), meds, and known
   max HR. Sets the objective baseline; some conditions are hard limits.
3. **`background`** — years running, marathons done. Training age = the
   biggest lever on "how fast can I push you up."
4. **`demographics`** — age, sex. Recovery capacity + physiological floor.
5. **`gut_fueling`** — GI tolerance on long runs, caffeine tolerance.
   A marathon is a fueling event; half of bonking is fuel. (constitution
   = static; execution = C)
6. **`psychology`** — bonk/DNF history, resilience to hard sessions, past
   taper response ("dead legs" vs "sharp"). Decides whether they finish
   the cycle.
7. **`coaching_prefs`** — coached before? structure vs flexibility? HR-
   driven vs pace-driven? communication cadence?
8. **`devices`** — GPS watch / HR strap? The meta-question: what can we
   even measure.

**B — per-cycle config (11 slots, ranked by importance):**

1. **`goal`** — which race, when, target result (finish / sub-X / BQ),
   hard or soft date? The anchor everything derives from. "Sub-3:30 in 20
   weeks" and "first marathon someday" are different plans.
2. **`starting_volume`** — current days/week, weekly mileage, longest run
   in the last month, how long it's been stable. Safe ramp (10% / ACWR)
   starts from the *current* base, not the target. (confirmed once at
   cycle start, then handed to C)
3. **`blackout_dates`** — travel / vacation / surgery / PT / absolutely-
   can't-train days. Build *around* them, don't collide.
4. **`weekly_availability`** — which days can train, which are fixed vs
   flexible (e.g. "5 days, long run must be Sunday, Wed always rest").
5. **`session_time_caps`** — weekday vs weekend single-session ceiling
   (45 min weekday, 2.5 h Sunday?). Caps how long the long run can grow.
6. **`quality_capacity`** — which days can be hard (need recovery after),
   how many quality sessions per week the body tolerates.
7. **`race_details`** — course profile (flat / hills / altitude), expected
   temp, start time, surface. Drives the specialization block; flat-cool
   vs hilly-hot are two preparations.
8. **`life_load`** — foreseeable big events in the cycle (work crunch,
   move, baby, exam, long trip). Flag them up front, not after a blowup.
9. **`downweek_pref`** — 3:1 or 4:1 down-week rhythm; reaction to a down
   week (some get anxious when volume drops).
10. **`tuneup_races`** — willing to run a mid-cycle half / 10k as a fitness
    test + race rehearsal? when?
11. **`strength_crosstrain`** — strength / core / mobility this cycle?
    bike / swim / other sport? Affects durability and recovery budget.

*(C is **not** part of this build — it's the existing streams + models:
current pain (asked every session), sleep/stress, RHR/HRV/weight, the
moving easy-pace anchor, current long-run length, quality tolerance,
recent PRs/benchmarks, long-run fueling execution, ad-hoc schedule
disruptions.)*

##### Storage — lossless episode + embeddable conclusion

The user gives answers; the agent must persist them in the CME so they
survive across sessions. Two CME columns do the work:

- **`episodes.context_json`** holds the **lossless raw text** of what the
  user said (`{area, raw_text}`). Nothing is paraphrased away.
- **`topics.name`** is a fine-grained, **embeddable** label (LLM-generated)
  and **`topics.working_conclusion`** is the distilled current answer for
  that area. The name is fine enough that the embedding model can find the
  backing episodes from it later.

##### Write path — `record_coach_fact(area, raw_text)` (eager)

The agent writes a fact the moment it learns it (no batching to session
close):

1. Create an **episode** — `event_type='profile' | 'cycle_config'`,
   `context_json={area, raw_text}` (lossless).
2. Embedding-search topics **within that `root_category`**:
   - **≥ high threshold** → same fact → **update** that topic's
     `working_conclusion` + link the new episode.
   - **between low and high** → **park a `pending_clarification`**: "is
     this an update to X, or a new fact?" (confirm-below-threshold — never
     silently merge an ambiguous match).
   - **< low threshold** → **create a new topic** in that area (one area
     can hold several topics, e.g. `injury_history` with multiple sites).

##### Read + coverage — `get_coach_profile()` / `get_cycle_config()`

Returns `{area: {conclusion, filled, updated_at}, gaps: [...]}`.

Coverage is a **hard judgment by `root_category`**, *not* similarity:
iterate the canonical area list (the 8 / 11 above); any area with no
topic carrying a non-empty `working_conclusion` is a `gap`. Embeddings
decide *which topic within an area* a new fact belongs to; the canonical
list decides *whether the area is covered at all*.

##### Conflict → re-review

A topic can already have a conclusion that a new event contradicts. When
the agent detects a mismatch between a new event and an area's conclusion:
`get_topic_episodes(topic)` to pull the raw backing episodes → re-review →

- **confident** → rewrite the conclusion via `record_coach_fact` (update
  branch).
- **ambiguous** → mark the topic `Conflicting` + park a
  `pending_clarification` for the user.

##### Prompt section (behavior change, `PROMPT_VERSION` v8 → v9)

The agent gains an explicit loop:

1. **Read** `get_coach_profile` + `get_cycle_config` — `make_plan` *must*
   read both before planning; `review_workout` reads profile.
2. **Judge per task** which areas are *required* and whether each is
   *specific enough* — with good/vague exemplars baked into the prompt for
   the critical slots, e.g.
   - `goal`: ✅ "Berlin 2026-09-21, sub-3:30, fixed date" / ❌ "想跑个马拉松"
   - `starting_volume`: ✅ "40 mi/wk, 5 runs, longest 16 mi, stable 8 mo" /
     ❌ "跑得还行"
3. **Follow up** — a missing or vague *required* area → ask **one**
   targeted question before planning, and `record_coach_fact` the answer
   eagerly. Non-critical gaps → park a `pending_clarification` to ask
   later, don't block.

The guideline is deliberately tight: tell the agent exactly when to ask
(required + missing/vague) and when *not* to (covered, or non-critical —
park it), so it neither plans on air nor interrogates the user.

##### Recommended build split (prompt blast-radius isolation)

- **PR-1 (backend only, zero behavior change)** — taxonomy constants
  (`PROFILE_SLOTS` / `CYCLE_SLOTS`), `record_coach_fact`, the read helpers,
  the hard-coverage algorithm, the embedding-threshold write logic, tests.
  Safe; doesn't touch the agent.
- **PR-2 (behavior change)** — the 3 MCP tools (`record_coach_fact`,
  `get_coach_profile`, `get_cycle_config` + `get_topic_episodes` if not
  already exposed), the prompt section, `PROMPT_VERSION` v8 → v9 (+
  changelog row), prefetch wiring (`make_plan` prefetch reads profile +
  cycle config), tests. The prompt half lands for review before merge.

---

## 4. Engineering debt

*(The longest section — the engineering-quality backlog. Distinct from
the coach-brain feature roadmap in §3.4.1: that's "build the coaching
intelligence", this is "keep the codebase healthy". They cross-
reference but track separately.)*

### 4.1 CI + test coverage — ✅ largely done

GitHub Actions `ci.yml` runs Python (`uv run pytest`) + web
(`tsc --noEmit`, `eslint`). Reproduce locally:

```bash
uv run pytest -q            # backend, from repo root
cd web && npx tsc --noEmit && npx eslint .   # frontend
```

Coverage as of 2026-05-28: **711 tests passing, 3 skipped.** Per-module:

| Module | Tests | Notes |
|---|---|---|
| `data_processor.py` | 112 | RunActivity, health ledger, route profile, CRUD |
| `personal_coach_mcp.py` | 48+ | all tools (path + params + shape via mocked `_get`) |
| `cognitive_memory_engine.py` | 41+ | topics/episodes/models/links, migrations |
| `api_server.py` | smoke (≈65 routes no-500) + behavior | dispatch, mutations, memory CRUD |
| `google_calendar.py` | 35 | OAuth flow + event mapping (mocked googleapiclient) |
| `seed_models.py` | 60+ | 5 model refits + helpers, all math pinned |
| `agentic_coach.py` | basics + `_build_prompt` | session guards, today-date injection |
| `langsmith_setup.py` | 41 | env-var combos + no-key-leak invariant |
| Frontend | TS + ESLint gate | no Vitest unit tests yet (single-user scale) |

**Remaining (Phase 3/4 of the original CI plan):** frontend Vitest
unit tests for pure functions (`format`, `coach-errors`, `todays-read`,
`use-coach-session`); integration tests against a real Gemini key
behind a `--integration` flag. Neither blocking.

### 4.2 Data-layer filters (not prompt rules) — ✅ done

Garmin interpretive labels filtered at the MCP boundary, not by a
"SILENTLY IGNORE..." prompt block. The prompt is an unstable interface
(model swap / language / long context drop rules — burned twice); the
data layer is the stable place to filter noise. `hr_zones` →
`medium_term_hr_effort_map` projection so prompt + tool output share
the same explicit name. Saves ~30% of the review_workout first-turn
prompt budget.

### 4.3 Structured tracing — ✅ done

Both halves shipped: local JSONL (PR B) + LangSmith (PR E). See
[§3.4.4](#344-observability--traces--langsmith). The original ask
included "snapshot the prompt version per turn" (done via
`PROMPT_VERSION` + `prompt_hash`) and "trail through
consolidate_memory_background" (the CME proposal pipeline now logs
`topic_decisions` rows).

### 4.4 Repo layout reorg — ⚠️ partially done

Backend is now a `backend/` package (was a flat top-level dump);
`scripts/` holds one-off CLIs + `migrations/`. Further role-grouping
(e.g. `backend/ai/`, `backend/data/` subpackages) was scoped at
2026-05-13 but never executed and isn't urgent at current size.
**Decision pending:** keep as a backlog item or drop.

### 4.5 Open gaps (newer, found during feature work)

- **`POST /api/memory/models/refit-all`** — iterate `REFIT_REGISTRY`,
  refit every model in one call; wire to startup. Found during P6
  testing: a fresh/rebuilt `cognition.db` leaves the model store empty
  until each model is manually re-refit (the HRV baseline was missing
  on 2026-05-28 until we hit the endpoint by hand). ~½ day.
- **`tempo.pace_hr_table`** — deferred from P6 batch 2 because the
  user's `lap_categories` is sparse (~0 tempo-tagged laps). Build via
  an HR-band heuristic (laps with avg_hr in LT × [0.88, 1.02],
  duration ≥ 3 min) so it doesn't need user labels. ~½ day.
- **Non-running activity visibility** (swim/bike on Activity tab) — UI
  bug, not AI. `/api/runs` filters `"running" in typeKey`, so synced
  swims/bikes fall through both it and `/api/manual-activities`.

(*Sync gap-resilience + stub detection shipped in #77 — `_is_stub` in
`garmin_sync.py` + `days_back` bumped 5→30 — so it's no longer a gap.*)

### 4.6 Post-substrate features (sequenced after Phase 3)

- **Athlete profile (A) + cycle config (B) capture** — the next
  high-leverage build: A/B intake into the CME so the agent has an
  enrollment form + campaign map, not just the continuous (C) eyes. Full
  spec in [§3.4.5](#345-planned--athlete-profile-a--cycle-config-b-capture).
  Recommended PR-1 (backend) + PR-2 (agent/prompt) split.
- **§6 advice trail** — what the coach said, did the user accept it,
  what happened. ~2–3 days.
- **§8 goal feasibility** — projection + plan adjustment from completed
  work in the cycle. ~2–3 days.

---

## 5. Appendix

### Storage tour

```
data/
├── chat_memory.db        # LangGraph checkpoints + session_meta
├── cognition.db          # CME: topics · episodes · models ·
│                         #      topic_episode_links · topic_decisions ·
│                         #      pending_clarifications
├── get_activities/       # Garmin raw JSON dumps
├── get_activity_details/ # per-activity detail (telemetry source)
├── derived/              # processed CSVs incl. daily_health_metrics.csv
├── manual_inputs/        # daily_checkins.json · planned_workouts.json ·
│                         # user_zones.json · run_*_meta.json
├── oauth/                # google_token.json, garmin tokens
├── traces/               # YYYY-MM-DD.jsonl agent traces
└── sync_state.json       # garmin_sync cursor
```

### Provider routing

| Call site | Chain | Why |
|---|---|---|
| Agent ReAct | gemini 3.1-flash-lite (pinned) | tool-calling, large context |
| summarize / consolidate / episodic summary | groq → gemini | off the agent's gemini RPM budget |
| embeddings | gemini embedding-2 (pinned, no fallback) | vectors from different models live in different spaces |

Rate-limit-aware retry lives in the frontend (`coach-errors.ts`):
Gemini 429 → 10s cooldown + one auto-retry.

### Document history — why there used to be 8 docs

This guide replaced 6 older docs that had accumulated and drifted:

| Old doc | Folded into | Why it drifted |
|---|---|---|
| `architecture.md` | §1, §5 | said "17 tools", "streaming deferred", "planned stream not wired" — all since built |
| `coach_brain_design.md` | §3.4.1 | the living roadmap — current, just verbose |
| `coach_chat_design.md` | §3.4.2 | status header still said "not yet implemented" when it was built |
| `mcp_tools_design.md` | §3.3 | v2 design from 5/9, predated ~11 newer tools |
| `IMPROVEMENTS.md` | §4 | infra backlog; items 1–3 done, item 4 stale |
| `CI.md` | §4.1 | reference card, absorbed |
| `langsmith-setup.md` | §3.4.4 | static setup runbook, not append-only |
| `PROMPT_CHANGELOG.md` | §3.4.3 | folded the version table in; the per-commit "add a row" convention now points at §3.4.3 |

Nothing kept standalone — this is the single doc. When you bump
`PROMPT_VERSION`, add the row to [§3.4.3](#343-prompt-versioning).
