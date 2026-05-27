# Coach Brain — Information Layers & Storage Plan

**Status**: design doc, not yet implemented. Drives a multi-PR rollout
of AI coaching capability beyond current state (post §2 data-layer
filters, post §1 Phase 3 backend tests).

This doc answers two questions together:

1. **If I were your coach, what info would I want to see?** — 8 layers,
   shallowest to deepest.
2. **Where does each piece live architecturally?** — three buckets
   (MCP / CME-episode / CME-model) mapped onto each layer.

The 8 layers are the spine. Storage decisions hang off them.

---

## Mental model

A good human coach reasons over four kinds of input:

```
   objective sensor data        perceived athlete state
   (Garmin: HRV, pace, HR,      (athlete's voice: "legs heavy",
    cadence, sleep)              "knee twinged mile 3", "skipped
                                  Tuesday because work")
            \                    /
             \                  /
              ↓                ↓
        planned intent      external context
        (what was on        (weather, travel, jet lag,
         the schedule)       altitude, life events)
                  \    /
                   ↓ ↓
            patterns over time
        (your recovery curve, your
         heat response, your typical
         weekday quality, your
         adherence rate, your fitness
         trajectory)
                   ↓
          coach's continuity
        (what I advised, did you
         accept, what was the outcome)
```

Today the agent sees only **objective + a thin slice of perceived**
(per-lap RPE labels from `manual_meta`). Everything else is missing
or invisible to the agent.

---

## Storage decision: 3 buckets, 4 tables

| Bucket | What | Lives in | New? |
|---|---|---|---|
| **A. Objective data** | Read from DB / files. Deterministic, stateless. | **MCP tools** + existing FastAPI endpoints | Mostly additive: surface what's already on disk; add a few rolling-stat tools |
| **B. Patterns / Models** | Personal observations with parameters. Stateful, evolving. | **CME — new `models` table** | New table parallel to `episodes` |
| **C. Verbal / episodic** | Free-form text, events, conversation. | **CME — existing `episodes` table** + new event types | New event types (skip-reason, post-run-verbal, travel, illness, advice) |

`topics` is the **index**: each topic carries a name, status, and a
sticky-or-pending `working_conclusion`. The agent consults topics to
decide whether to descend into raw episodes or into parameterized
models. As more episodes accumulate under a topic, the LLM can propose
generalizing them into a model. Topics don't store data themselves —
they're the table of contents.

```
              topics                      ← index + conclusion
                ↓
           ┌────┴────┐
           ↓         ↓
       episodes    models                 ← evidence layer
       (raw)       (parameterized)
           ↑________↑
            generalize over time
```

---

## Layer-by-layer plan

For each layer: what info, what we have today, what's missing, where
new pieces land.

Legend: ✅ done · 🆕 to build · 🔄 surface existing data

---

### 1. Objective data — sensor truth

Coach wants: HRV trend with baseline + delta · pace-HR pairs over time
· aerobic decoupling per run · biomech (cadence, GCT, stride) drift
across sessions.

| Item | Today | To build | Bucket |
|---|---|---|---|
| Today's HRV / sleep / RHR | ✅ `get_health_today` | — | A (MCP) |
| 14d/7d rolling baselines + delta | partial in `/api/health/snapshot` | 🆕 dedicated MCP tool `get_health_baseline(metric, days)` | A (MCP) |
| Pace-HR table (last N tempos / longs) | — | 🆕 `get_pace_hr_table(workout_type, n)` | A (MCP) |
| Aerobic decoupling per run | — | 🆕 `get_run_decoupling(activity_id)` — pandas on telemetry | A (MCP) |
| Cadence / GCT cross-session series | — | 🆕 `get_biomech_series(metric, days)` | A (MCP) |
| Personal HRV recovery curve | — | 🆕 model `recovery.hrv_curve_post_long_run` (decay shape) | **B (CME model)** |
| Personal cadence baseline + warning threshold | — | 🆕 model `biomech.cadence_baseline` (mean_std) | **B (CME model)** |

### 2. Perceived — athlete's voice

Coach wants: daily check-in · skip reasons · post-run verbal notes ·
post-workout sleep quality.

| Item | Today | To build | Bucket |
|---|---|---|---|
| Per-lap RPE labels | ✅ `manual_meta.lap_categories` | — | A (MCP, existing) |
| Structured daily check-in (sleep quality 1-5, soreness location/level, mood, motivation) | — | 🆕 UI widget + `/api/checkins` CRUD + MCP tool `get_recent_checkins(days)` | A (MCP) + C (each saved check-in becomes an `episodes` row) |
| Skip reason for missed planned workout | — | 🆕 prompt user when planned-vs-actual mismatch; save as episode `event_type='workout_skip'` | C (CME episode) |
| Post-run verbal note ("knee twinged mile 3") | — | 🆕 chat input + auto-attach to most-recent run; save as episode `event_type='post_run_verbal'` | C (CME episode) |
| Weekday quality pattern ("Tuesdays are your worst") | — | 🆕 model `schedule.day_of_week_quality` (ordinal_score) | **B (CME model)** |
| Motivation-quality correlation | — | 🆕 model `subjective.motivation_quality_link` (rate) | **B (CME model)** |

### 3. Planned intent — what was supposed to happen

Coach wants: this week's planned workouts · planned-vs-actual gap per
session · narrative for WHY this block looks the way it does.

| Item | Today | To build | Bucket |
|---|---|---|---|
| Planned workout entries (date, type, duration, target pace/HR/distance) | — | 🆕 calendar CRUD + MCP tool `get_planned_workouts(start, end)` | A (MCP) |
| Plan-vs-actual deviation per workout | — | 🆕 compute tool `get_plan_actual_deviation(activity_id)` | A (MCP) |
| Block-level coaching narrative ("4 weeks base then 2 weeks speed because X") | — | 🆕 store as `topic` with `root_category='Plan/Rationale'`; agent writes prose | C (CME topic) |
| Adherence rate (planned hard done %, planned long done %) | — | 🆕 model `adherence.planned_vs_actual` (rate, by workout_type) | **B (CME model)** |
| Habitual deviation pattern ("you always downgrade Tuesday hard to easy") | — | 🆕 model `adherence.habitual_deviations` | **B (CME model)** |

> Note: this layer depends on a **planned-workout data source**. Phase 2
> calendar was scoped in `feedback_perceived_vs_intent.md` but not built.
> Initial implementation could be a manual JSON file
> (`data/manual_inputs/planned_workouts.json`) before wiring Google Cal.

### 4. External context — same data, different meaning

Coach wants: weather attached to runs · jet-lag/travel notes · altitude
& route change · life events (illness, deadlines) · menstrual phase.

| Item | Today | To build | Bucket |
|---|---|---|---|
| Weather per run | ✅ `/api/runs/{id}/weather` exists | 🔄 MCP surface: include weather block in `get_run_detail` | A (MCP) |
| Route altitude / terrain profile | partial in telemetry | 🆕 `get_run_route_profile(activity_id)` returning gain/loss/grade distribution | A (MCP) |
| Menstrual cycle phase | ✅ Garmin `get_menstrual_data_for_date` synced | 🔄 MCP surface: `get_menstrual_phase(date)` | A (MCP) |
| Travel / time-zone events ("flew to Tokyo, 13h ahead") | — | 🆕 chat input or quick widget; save as episode `event_type='travel'` | C (CME episode) |
| Illness events ("stomach bug 5/10-5/12") | — | 🆕 same as travel; episode `event_type='illness'` | C (CME episode) |
| Life stress events ("demo prep all week") | — | 🆕 episode `event_type='life_stress'` | C (CME episode) |
| Personal heat response ("you pace -8% in 30°C+") | — | 🆕 model `heat.pace_drop_at_temp` (linear_trend) | **B (CME model)** |
| Personal altitude response | — | 🆕 model `altitude.hr_at_grade` (linear_trend) | **B (CME model)** |
| Luteal-phase HRV drop | — | 🆕 model `menstrual.hrv_phase_response` | **B (CME model)** |

### 5. History as patterns — pre-computed, not re-derived per turn

Coach wants: rolling stats AND characterized models (the latter is
what makes a coach feel like a coach).

| Item | Today | To build | Bucket |
|---|---|---|---|
| Rolling 14d/7d aggregates | ad-hoc in `data_processor` | 🆕 unified tool `get_rolling_stat(metric, days, agg)` | A (MCP) |
| Cycle-to-cycle volume diff | — | 🆕 `get_cycle_volume_diff(current_cycle, prev_cycle)` | A (MCP) |
| Sleep debt last N days | — | 🆕 `get_sleep_debt(days, target_hours)` | A (MCP) |
| Pace-at-LT-HR trajectory | — | 🆕 model `fitness.pace_at_lt_hr` (linear_trend) | **B (CME model)** |
| Recovery curve characterized | — | 🆕 model `recovery.hrv_curve_post_long_run` (decay) — also in §1 | **B (CME model)** |
| Same-pace HR drift across recent long runs | — | 🆕 model `fitness.hr_drift_at_pace_long_runs` (linear_trend) | **B (CME model)** |

### 6. Coach continuity — what I said, did you accept, what happened

Coach wants: trail of advice given · acceptance/pushback per advice ·
outcome tracked back to the advice that drove it · validity check on
month-old advice given new data.

| Item | Today | To build | Bucket |
|---|---|---|---|
| What we discussed (topic level) | ✅ `topics` + `episodes` | — | C (CME, existing) |
| **Advice node — specific recommendation with context** | — | 🆕 new `event_type='advice_given'` on episodes; carries fields `{recommendation, rationale, expected_outcome}` | C (CME episode, new event_type) |
| **Acceptance / pushback tracking** | — | 🆕 follow-up episode `event_type='advice_response'` linked to original advice episode | C (CME episode, new event_type) |
| **Outcome tracking** | — | 🆕 follow-up episode `event_type='advice_outcome'` linked to original advice; auto-promoted when relevant data arrives | C (CME episode, new event_type) |
| Advice acceptance rate by topic | — | 🆕 model `interaction.advice_acceptance` (rate, by_topic) | **B (CME model)** |
| Advice validity re-check ("is month-old advice still good?") | — | 🆕 agent-driven, no new storage — uses advice trail + current data | (logic, not storage) |

### 7. Data quality — when to dismiss a number

Coach wants: flag HRV unreliable (sleep <4h, illness) · flag GPS dropout
in laps · catch sync stubs (the 5/7 bug class).

| Item | Today | To build | Bucket |
|---|---|---|---|
| Sleep-too-short → HRV unreliable | — | 🆕 quality-flag tool on `get_health_today` and baseline computations | A (MCP) |
| GPS dropout / impossible-pace lap detection | — | 🆕 `get_lap_quality_flags(activity_id)` | A (MCP) |
| Sync stub detection (existence check ≠ data check) | — | 🆕 fix at sync layer — see IMPROVEMENTS §5 Garmin gap-resilience | (sync layer, not CME) |
| Cross-source composite signal ("HRV + sleep + stress all bad → strong signal") | — | 🆕 model `recovery.composite_signal` (ordinal_score over 3 inputs) | **B (CME model)** |

### 8. Goal feasibility — projection + plan adjustment

Coach wants: current fitness numbers · trajectory projection ·
gap-to-goal · what-it-would-take-to-hit-target.

| Item | Today | To build | Bucket |
|---|---|---|---|
| Current VO2Max / LT pace / race predictor | ✅ in `get_athlete_profile.fitness` | — | A (MCP, existing) |
| Race goal statement (name, date, target pace, history of framing) | — | 🆕 topic `Goals/Race-{name}` with structured fields | C (CME topic, possibly new event_type for race goal updates) |
| Trajectory projection (4 weeks out) | — | 🆕 model `fitness.trajectory` (linear_trend over recent LT slope) | **B (CME model)** |
| Plan-adjustment recommendation | — | (agent-driven; uses §1 + §3 + §5 + §8 models above) | (logic) |

---

## CME schema upgrade — adding `models` table

The new `models` table sits parallel to `episodes`. Topics get one new
column to point at related models.

### `models` table

```sql
CREATE TABLE models (
    model_id          TEXT PRIMARY KEY,           -- mdl_xxxxxx
    model_key         TEXT UNIQUE NOT NULL,       -- "recovery.hrv_curve_post_long_run"
    name              TEXT NOT NULL,              -- human-readable, may be Chinese
    category          TEXT NOT NULL,              -- "Health/Recovery", "Running/Performance"...
    model_type        TEXT NOT NULL
                      CHECK(model_type IN ('decay', 'linear_trend', 'mean_std',
                                            'ordinal_score', 'rate', 'fixed_obs')),
    params_json       TEXT NOT NULL,              -- parameters; shape determined by model_type
    n_samples         INTEGER NOT NULL DEFAULT 0,
    confidence        TEXT CHECK(confidence IN ('low', 'medium', 'high')),
    evidence_json     TEXT,                       -- {"episodes":[], "activities":[], "dates":[]}
    derivation_method TEXT NOT NULL
                      CHECK(derivation_method IN ('stat', 'llm', 'hybrid')),
    status            TEXT NOT NULL DEFAULT 'Forming'
                      CHECK(status IN ('Forming', 'Stable', 'Stale', 'Drifting')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_verified_at  TEXT
);

CREATE INDEX idx_models_category ON models(category);
CREATE INDEX idx_models_status   ON models(status);
```

### `topics` schema change (1 column)

```sql
ALTER TABLE topics ADD COLUMN related_models TEXT DEFAULT '[]';
-- JSON array of model_ids that abstract this topic
```

### `topic_decisions` CHECK extension

```sql
-- kind enum currently: ('new_topic', 'conflict', 'episode_linking')
-- add: 'new_model' for model proposals
-- (SQLite rebuild required to alter CHECK)
```

### Why this shape

- **`models` separate from `topics`**: topics is index + sticky
  conclusion (prose); models is parameterized observation. Different
  birth paths (chat-LLM vs stat-job or LLM-from-history) and different
  update mechanics (LLM rewrite vs param refit). Conflating them
  pollutes topics over time.
- **`status` enum is model-specific**: Forming/Stable/Stale/Drifting
  matches model lifecycle. Reusing topic's Open/Testing/Resolved/
  Conflicting overloads the semantics.
- **`params_json` keeps schema flexible**: each `model_type` has its
  own param shape. App-layer validation; `models.py` exports typed
  schemas per type.
- **`evidence_json` flexible**: a model can be backed by Garmin
  activities, dates, OR episodes (or all three). JSON shape avoids
  three separate columns.

---

## Episode → Model lifecycle

The pipeline that "generalizes" raw episodes into models:

```
1. Episodes accumulate under a topic
       ↓
2. After consolidate_memory_background runs, OR on a scheduled cron:
   For each topic with ≥ K related episodes since last_verified:
     ask LLM: "Are these episodes parametrically generalizable?
               If yes, propose {model_key, model_type, params, n_samples}."
       ↓
3. LLM proposal goes into topic_decisions queue with kind='new_model'
       ↓
4. UI surfaces it to user: "Promote N episodes to model X?"
   user can: confirm-create / reject / adjust
       ↓
5. On confirm:
     - Insert into models table
     - Append model_id to topic.related_models
     - Mark topic_decisions row as resolved
       ↓
6. After model exists, refit pipeline:
     - derivation_method='stat'  → background cron refits from raw data
     - derivation_method='llm'   → re-proposed after N new related episodes
     - derivation_method='hybrid'→ stat refit + LLM commentary
```

Status transitions on a model:

```
Forming  (n_samples < threshold; just created)
   ↓ enough samples accumulated
Stable   (n_samples ≥ threshold; params well-supported)
   ↓ no refit in 30d (e.g. user hasn't done a long run)    OR     ↓ recent data diverges from stored params
Stale    (params still useful but aging)              Drifting (refit needed; alert user)
```

---

## MCP tools surfaced to the agent

Beyond the existing `recall_topics` + `search_episodes`:

```python
@mcp.tool()
async def get_model(model_key: str) -> dict | None:
    """Get a personal pattern model by key.
    Examples:
      - 'recovery.hrv_curve_post_long_run'
      - 'heat.pace_drop_at_temp'
      - 'adherence.planned_vs_actual'
    Returns null if model doesn't exist yet (not enough data)."""

@mcp.tool()
async def list_models(category: str | None = None,
                     status: str | None = None) -> list[dict]:
    """List personal patterns. Filter by category (e.g. 'Health/Recovery')
    or status (Forming/Stable/Stale/Drifting)."""
```

The agent uses these the way a coach uses memory:

```
user: "Why is my HRV down today?"
agent: → get_health_today() → "HRV 69, baseline 70.3, -2%"
       → search topics for "HRV" → tpc_xxx points at mdl_recovery.hrv_curve_post_long_run
       → get_model('recovery.hrv_curve_post_long_run') → "your typical
         post-long-run nadir is day-2, -8%; today is day-2 post your
         Sat long run; -2% is actually milder than your norm"
       → reply with context-aware answer
```

Without the model, the agent would have to re-derive that curve from
30 days of HRV + the run log every turn. With the model, it's one tool
call.

---

## Sequenced rollout

10 PRs in 3 phases. Strict order — each builds on the previous. Items
A/B/C/E originate from open AI-side work tracked here (also referenced
in IMPROVEMENTS.md §3); P1-P6 are the coach-brain buildout from this
doc.

### Phase 0 — Felt-pain quick wins (do these FIRST for momentum)

| PR | Scope | Estimate | Why this slot |
|---|---|---|---|
| **A** ✅ done 2026-05-27 ([#71](https://github.com/zhnzhang61/PersonalCoach/pull/71)) | Fix Coach UI multi-day timeline confusion. Backend `get_history_with_ts` walks `checkpointer.list()` to tag each message with first-seen ts; `/api/ai/history` emits ts. Frontend `<DayDivider>` inserted at calendar-day transitions (local TZ) in both active + closed session streams. 8 new unit tests + 2 new API behavior tests. | ~½ day | User-flagged felt pain. Isolated UI/session-boundary issue, no deps. |
| **C** ✅ done 2026-05-27 | SSE streaming for `/api/ai/chat`. Backend `agent.chat_stream` async-generator wraps `astream_events(version='v2')`, filters to {token, tool_call, done, error} events. New endpoint `POST /api/ai/chat/stream` returns `text/event-stream`. Frontend `streamSSE` consumer in `web/lib/api.ts`; `sendChat` switches to optimistic user bubble + accumulating AI bubble during stream. 5 unit tests + 4 endpoint behavior tests. | ~1 day | Felt every chat turn. Independent of coach-brain buildout. |

### Phase 1 — Foundation before P2

| PR | Scope | Estimate | Why this slot |
|---|---|---|---|
| **B** ✅ done 2026-05-27 | Structured tracing MVP. New `backend/trace_logger.py` with `Trace` dataclass + `TraceLogger` class (daily JSONL rotation under `data/traces/YYYY-MM-DD.jsonl`, swallow-and-continue on disk errors). `PROMPT_VERSION="v7"` constant + sha1-truncated `prompt_hash` in `agentic_coach.py`. Hooks in `_run_turn` (chat), `_action_turn` (actions, with `kind` param), `chat_stream`'s producer, and `consolidate_memory_background` in CME — covers all 4 LLM-turn paths. `docs/PROMPT_CHANGELOG.md` seeded with v6/v7. 17 unit tests including the "never raise on disk failure" contract. | ~1 day | **Blocking for P2**: P2 introduces async LLM proposal → confirm flow. Without traces we'll be flying blind when proposals don't fire / parse / land. |

> **Removed from earlier roadmap draft**: a "D — CME tests" PR. CME is
> already well-covered: `tests/test_cme_v2.py` (9 tests) +
> `tests/test_cme_v2b.py` (32 tests) = 41 tests covering episode-topic
> junctions, retrieve_working_context, embedding match, topic_decisions
> park/resolve, consolidation flow, conflict promotion, link resolution.
> IMPROVEMENTS.md §1 Phase 3 marks CME "best-covered, skip." The only
> NEW coverage P1 needs — migration idempotency, `kind='new_model'`
> enum round-trip, `related_models` column round-trip — is bundled
> into P1 itself (3–5 tests). `agentic_coach.py` Phase 3 tests
> (session lifecycle, archive, delete-session) remain a genuine gap
> but P1 doesn't touch agent code, so they're a parallel track via
> IMPROVEMENTS §1 Phase 3 — not blocking this roadmap.

### Phase 2 — Coach-brain buildout (the main course)

| PR | Scope | Estimate | Why this slot |
|---|---|---|---|
| **P1** ✅ done 2026-05-27 | `models` table scaffolding. New CME `models` table (model_id PK + UNIQUE model_key + model_type/derivation_method/status/confidence CHECK enums + params_json + n_samples + evidence_json + timestamps) parallel to `episodes`. `topics.related_models` JSON column added via idempotent ALTER. `topic_decisions.kind` CHECK extended to include `'new_model'` via table-rebuild. MemoryOS helpers: `create_model`, `get_model`, `list_models`, `update_model_params`, `link_topic_to_model`. New `/api/memory/models[/{key}]` + `/api/memory/models/refit/{model_key}` endpoints. MCP tools `get_model` + `list_models` for the agent. Seed model: `recovery.hrv_14d_baseline` (mean_std) from `get_health_stats`. 22 new tests (migration idempotency, CRUD, validation, seed refit). | ~1 day | Foundation. No user-visible feature, but unblocks everything else. |
| **P2** ✅ done 2026-05-27 | Episode → model generalize pipeline. `MemoryOS.propose_model_from_topic(topic_id, trigger='manual')` gathers a topic's linked episodes, LLM asks "parametrically generalizable?", parks `kind='new_model'` decision. `resolve_topic_decision` handles `'new_model'`: `create_new` → `create_model` (derivation='llm') + `link_topic_to_model`. Endpoints: `POST /api/memory/topics/{tid}/propose_model`, `GET /api/memory/decisions`, `POST /api/memory/decisions/{id}/resolve`. MCP tools `propose_model_from_topic` / `list_pending_decisions` / `resolve_decision` so the **agent drives confirm/reject in chat** (per design A — no separate UI page). LLM JSON parser strips markdown fences + leading prose. Tracer hooks via PR B (`kind='model_propose'`). 16 unit tests; side-fix `_SCHEMA_SQL` to canonically include `topic_episode_links` + episodes event-time columns (pre-existing schema gap that made fresh DBs crash on `get_topic_episodes`). | ~1-2 days | Makes models grow. Without P2, P1 is a passive store. |
| **P3** | Daily check-in (perceived layer §2): UI widget on Health/Coach tab + `/api/checkins` CRUD + episode integration + MCP tool `get_recent_checkins(days)` | ~1-2 days | First C-bucket user-visible feature. Closes "agent blind to subjective state" gap. |
| **P4** | Planned workouts (intent layer §3): start with manual JSON file (`data/manual_inputs/planned_workouts.json`) + MCP tool `get_planned_workouts(start, end)` + plan-vs-actual deviation compute. Google Cal wiring deferred. | ~2 days | Unlocks 95% of coaching value (adherence + deviation). Manual JSON keeps scope tight. |
| **P5** | External context channels (§4): travel / illness / life-stress quick-add UI + new episode types + weather/menstrual MCP surface (already-synced data not exposed) | ~1-2 days | Closes "agent can't see why" gap. Mostly surfacing existing data + small UI for free-text events. |
| **P6** | First batch of stat-derived models (§1, §5): aerobic decoupling per run, pace-HR table for tempos, cadence baseline, sleep debt, cycle-volume diff. Each follows P1+P2 stat-derivation path. | ~2-3 days | First real B-bucket payloads. Builds on P1 store + P2 pipeline. |

### Phase 3 — Trace upgrade

| PR | Scope | Estimate | Why this slot |
|---|---|---|---|
| **E** | LangSmith wiring (`LANGSMITH_API_KEY` env + middleware around agent calls). Free tier OK for single-user volume. | ~½ day | Pure upgrade on top of B's JSONL traces. After ≥1 month of usage data so we can query meaningfully. |

### After Phase 3

Layers §6 (advice trail) and §8 (goal feasibility) build on top of the
P1-P6 substrate. Spec'd in this doc but not yet sequenced — likely
becomes Phase 4 once we see how the agent actually uses §3 (planned) +
§4 (external) + the model store in practice.

### Sequencing rationale

**Why A + C before any foundation work.** Both are felt-pain items
the user has flagged or experiences every chat turn. They're isolated
(no deps on coach-brain buildout) and quick. Doing them first means:
(a) the UX pain doesn't persist while we spend a week on
infrastructure, and (b) momentum — two visible wins before the slower
foundation phase.

**Why migration-safety tests live in P1, not a separate PR.** P1
introduces an ALTER on `topics` + new `models` table + new
`topic_decisions.kind='new_model'` enum value. CME's existing
behavioral surface is already covered (41 tests via `test_cme_v2.py`
+ `test_cme_v2b.py`); what's NOT covered is P1's net-new schema. The
right place for those 3–5 tests is the same PR that ships the
schema change — they verify what P1 itself just added, not a
pre-existing gap. Spinning them off into a separate "CME tests" PR
would (a) misrepresent the actual CME coverage state and (b) split
the schema change from its own safety net.

**Why B must land before P2.** P2 wires an async pipeline: nightly
job (or manual trigger) → LLM proposes models from episode clusters →
land in `topic_decisions` queue → user confirms via UI →
write to `models`. Three async hops, three places where the proposal
can silently die. Without B's per-turn JSONL traces +
`PROMPT_VERSION` constant, debugging "the LLM didn't propose"
or "the proposal landed but never showed up in UI" means staring at
chat history and guessing. With B, every step emits a trace row we
can grep.

**Why E (LangSmith) is last.** E is purely a UI upgrade on top of B's
JSONL traces — same data, just queryable in a hosted UI. Doing E
without ≥1 month of usage data means the LangSmith dashboard is
empty and we can't yet tell which views are useful. Better to land
E after P6 when we have real traces accumulated through the full
flow (planned input → check-in → external context → model proposals
→ agent reply).

**Total time estimate.** ~10–14 working days (not counting review +
fix cycles). Phase 0: 1.5 days. Phase 1: 1 day. Phase 2: 7–11 days
(P1 includes its own migration-safety tests). Phase 3: 0.5 day.

### Where to start

**Next PR: P3** — Daily check-in (perceived layer §2). UI widget on
Health/Coach tab + `/api/checkins` CRUD + episode integration + MCP
tool `get_recent_checkins(days)`. First C-bucket user-visible feature.

Suggested branch: `add-daily-checkin-widget`.

Phase 0 + Phase 1 + P1 + P2 complete. The pattern-store pipeline is
now live end-to-end: episodes accumulate under topics → user asks
agent to scan a topic → LLM proposes a parameterized model → user
confirms in chat → model lands in CME with link back. P3 moves to
the next missing input layer — subjective check-in data.

**Previously landed**:
- **A** ✅ 2026-05-27 ([#71](https://github.com/zhnzhang61/PersonalCoach/pull/71)) —
  per-message ts + DayDivider for multi-day sessions.
- **C** ✅ 2026-05-27 ([#73](https://github.com/zhnzhang61/PersonalCoach/pull/73)) —
  SSE streaming for `/api/ai/chat` via `astream_events`; frontend
  renders accumulating AI bubble during stream. Cross-loop bridge
  through `self._loop` after codex P1 catch.
- **B** ✅ 2026-05-27 ([#74](https://github.com/zhnzhang61/PersonalCoach/pull/74)) —
  Structured tracing MVP (JSONL traces + `PROMPT_VERSION` +
  `docs/PROMPT_CHANGELOG.md`).
- **P1** ✅ 2026-05-27 ([#76](https://github.com/zhnzhang61/PersonalCoach/pull/76)) —
  `models` table + helpers + `get_model` / `list_models` MCP tools +
  seed `recovery.hrv_14d_baseline` model.
- **P2** ✅ 2026-05-27 — Episode → model generalize pipeline:
  `propose_model_from_topic` + `resolve_topic_decision` extended for
  `new_model` kind + MCP tools for chat-driven confirm/reject.

---

## What this doc does NOT cover

- **`agentic_coach.py` Phase 3 tests** (session lifecycle, archive,
  delete-session) — see IMPROVEMENTS §1 Phase 3. Genuine coverage gap,
  but P1 doesn't touch agent code, so parallel track. NOT blocking.
- **Frontend tests** (Vitest setup, format/coach-errors/todays-read/
  use-coach-session unit tests) — see IMPROVEMENTS §1 Phase 3. Parallel
  track, not blocking.
- **Sync gap-resilience** + **stub detection** — see IMPROVEMENTS §5.
  Not AI work; sync layer fix.
- **Non-running activity visibility** (swim/bike on Activity tab) — see
  IMPROVEMENTS §5. UI bug, not AI.
