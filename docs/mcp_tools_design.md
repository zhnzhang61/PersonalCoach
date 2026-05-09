# Personal Coach MCP — Tool Schema Design (v2)

**Status**: design doc, not yet implemented. Supersedes the v1 schemas in `personal_coach_mcp.py` (which mirror raw FastAPI responses 1:1).

## Why a redesign

v1 tools forward Garmin payloads verbatim. A coach analyzing a session needs ~40 fields per run; the raw payload has ~100, half of which is noise (OAuth scopes, profile image URLs, dive info, internal IDs). More importantly, v1 collapses three orthogonal data streams that **must** stay separate (see `feedback_perceived_vs_intent.md`):

| Stream | What | Source | When |
|---|---|---|---|
| **objective** | Sensor-measured truth | Garmin telemetry / summary | During the run |
| **perceived** | Athlete's RPE labels | `manual_meta.category_stats` / `lap_categories` / `notes` | After the run |
| **planned** | Intended workout | Google Calendar event with `personalcoach.training=true` | Before the run |

Every tool that surfaces effort-related fields must nest them under one of these three keys. Single fused `effort` fields are forbidden.

A second redesign axis: the user has a **manually annotated HR zone file** at `data/manual_inputs/user_zones.json`. Bands are RPE-named (`Hold Back / Recovery` / `Steady / Constant` / `Increasing Effort` / `Marathon Pace` / `Lactate Threshold` / `VO2 Max`) — same vocabulary as `manual_meta.lap_categories`. Tools must compute time-in-zone against THESE bands, not Garmin's generic Z1–Z5 (`hrTimeInZone_*` is misaligned and will confuse the LLM).

A third axis: the **Cognitive Memory Engine** (`cognitive_memory_engine.py`, three-tier: Semantic / Topics / Episodes) is currently pre-injected into every system prompt by `agentic_coach.py`. The redesign exposes CME as tools so the agent can query memory on demand (search past similar runs, look up unresolved questions, retrieve user profile) instead of carrying the entire memory as static prompt context. Pre-injection stays for now; the agent refactor is the *next* PR.

---

## Tool inventory

13 tools total, grouped by surface. ✱ = changed schema vs v1. ✚ = new tool.

| # | Tool | Status |
|---|---|---|
| **Profile + zones** | | |
| 1 | `get_athlete_profile()` | ✱ |
| **Recent state / load** | | |
| 2 | `get_readiness(date?)` | ✱ |
| 3 | `get_training_load(window=28)` | ✚ |
| **Runs** | | |
| 4 | `list_runs(start, end)` | ✱ |
| 5 | `get_run_detail(activity_id)` | ✱ |
| 6 | `get_run_telemetry(activity_id, downsample_sec)` | ✱ |
| 7 | `get_run_weather(activity_id)` | ✚ |
| **Training cycle** | | |
| 8 | `list_blocks()` + `get_cycle_stats(...)` | (mostly unchanged, minor field rename) |
| 9 | `get_monthly_stats(activity_type)` | (unchanged) |
| **Calendar** | | |
| 10 | `get_calendar_events(start, end)` | (unchanged) |
| 11 | `get_workout_plan(date)` | ✚ Phase 2 |
| **Manual activities** | | |
| 12 | `list_manual_activities` / `get_manual_activity` | (unchanged) |
| **Memory (CME)** | | |
| 13 | `recall_topics(status?)` / `search_episodes(keywords, limit)` / `get_pending_clarifications()` | ✚ |

---

## 1. `get_athlete_profile()`

Combines user profile + manually annotated HR zones + current cycle phase.

```json
{
  "athlete": {
    "age": 33,
    "sex": "M",
    "weight_kg": 82.6,
    "height_cm": 180.3
  },
  "fitness": {
    "vo2max": 48,
    "lactate_threshold_hr": 179,
    "lactate_threshold_pace": "7:25/mi",
    "hr_zones": [
      {"name": "Hold Back / Recovery", "low": 0,   "high": 144, "rpe_label": "Hold Back Easy"},
      {"name": "Steady / Constant",    "low": 145, "high": 162, "rpe_label": "Steady Effort"},
      {"name": "Increasing Effort",    "low": 163, "high": 173, "rpe_label": "Increasing Effort"},
      {"name": "Marathon Pace",        "low": 174, "high": 178, "rpe_label": "Marathon"},
      {"name": "Lactate Threshold",    "low": 179, "high": 183, "rpe_label": "LT Effort"},
      {"name": "VO2 Max",              "low": 184, "high": 220, "rpe_label": "VO2Max"}
    ]
  },
  "current_block": {
    "id": "block_002",
    "name": "Pre Fall 2026 Build",
    "start_date": "2026-04-20",
    "end_date": "2026-05-31",
    "primary_event": "running",
    "weeks_total": 6,
    "weeks_elapsed": 3,
    "weeks_to_event": 3,
    "phase": "build"
  },
  "preferences": ["Prefers pace in min/mi"],
  "medical_notes": ["No known injuries."]
}
```

**Notes on HR zones**:
- Source: `data/manual_inputs/user_zones.json`. Parsed at server boot and re-read on file change.
- `rpe_label` matches the strings used in `manual_meta.lap_categories` exactly so the AI can map "this lap was VO2Max RPE" ↔ "this lap's HR was in VO2 Max zone".
- The first band's `low=0` is a sentinel; the last band's `high=220` is a typical max-HR ceiling.

**Phase derivation rule** (cycle.phase): linear bucketing by week_num — base / build / peak / taper. First quartile = base, middle half = build, next eighth = peak, last eighth = taper. (Coach can override later if user provides explicit phase boundaries; for now this is the heuristic.)

---

## 2. `get_readiness(date?: str = today)`

Single-day readiness signal. Replaces v1's raw 7-day dump.

```json
{
  "date": "2026-05-07",
  "readiness": {
    "score": "yellow",
    "rationale": "RHR 52 vs 7d-baseline 50.7 (+2.6%, mild upward trend); HRV 74 stable vs baseline 73.4; sleep 6.3h (below 7h target)."
  },
  "today": {
    "sleep_score": 73,
    "sleep_hours": 6.32,
    "rhr": 52,
    "hrv": 74,
    "stress_avg": 29
  },
  "baseline_7d": {
    "rhr": 50.7,
    "hrv": 73.4,
    "sleep_hours": 6.97,
    "stress_avg": 26.4
  },
  "deltas_pct": {
    "rhr": "+2.6",
    "hrv": "+0.8",
    "sleep_hours": "-9.3"
  },
  "history_7d": [
    {"date": "2026-04-30", "sleep_score": 81, "sleep_hours": 6.75, "rhr": 51, "hrv": 73, "stress_avg": 14},
    {"date": "2026-05-01", "sleep_score": 68, "sleep_hours": 6.43, "rhr": 53, "hrv": 73, "stress_avg": 30},
    "..."
  ]
}
```

**Score rule** (green/yellow/red):
- **green**: HRV within ±5% of baseline AND RHR within ±5% AND sleep ≥ 7h
- **red**: HRV down >10% OR RHR up >10% OR sleep < 5h
- **yellow**: anything in between

These thresholds are heuristic; the rationale string spells out the actual numbers so the AI can second-guess.

---

## 3. `get_training_load(window: int = 28)` ✚

Acute / chronic training load, ACWR ratio, weekly mileage trend. Surfaces injury-risk signal.

```json
{
  "today": "2026-05-07",
  "window_days": 28,
  "acute_7d": {
    "miles": 21.8,
    "moving_hours": 3.2,
    "session_count": 4,
    "garmin_load_sum": 730
  },
  "chronic_28d": {
    "miles": 102.3,
    "miles_per_week_avg": 25.6,
    "moving_hours": 15.5,
    "session_count": 18,
    "garmin_load_sum": 3024,
    "garmin_load_per_week_avg": 756
  },
  "acwr": 0.97,
  "acwr_band": "sweet",
  "weekly_miles_trend": [
    {"week_start": "2026-04-13", "miles": 23.0},
    {"week_start": "2026-04-20", "miles": 12.0},
    {"week_start": "2026-04-27", "miles": 23.3},
    {"week_start": "2026-05-04", "miles": 16.8}
  ],
  "trend_summary": "Last 4 weeks miles: 23.0 → 12.0 → 23.3 → 16.8 (W2 of build cycle, taper-style downweek)"
}
```

**ACWR bands**:
- `< 0.8` → **detraining** (load too low)
- `0.8–1.3` → **sweet spot** (productive)
- `1.3–1.5` → **caution** (spike)
- `> 1.5` → **danger** (high injury risk)

Garmin's `activityTrainingLoad` is the per-run load summed across the window. ACWR uses 7-day-average-load / 28-day-average-load.

---

## 4. `list_runs(start: str, end: str)` ✱

Compact summary list. Drops Garmin's 60+ noise fields. Each run is **~12 fields, not 100**.

```json
{
  "start": "2026-05-01",
  "end": "2026-05-07",
  "runs": [
    {
      "id": 22779224627,
      "name": "Weehawken Running",
      "date": "2026-05-05",
      "start_time": "18:52",
      "type": "running",
      "summary": {
        "distance_mi": 3.50,
        "moving_time": "33:46",
        "avg_pace": "9:38/mi",
        "elevation_gain_ft": 246
      },
      "objective": {
        "avg_hr": 162,
        "max_hr": 180,
        "garmin_label": "IMPACTING_TEMPO_22",
        "training_effect_aerobic": 3.5,
        "training_load": 140
      },
      "perceived": {
        "category_breakdown": [
          {"category": "Increasing Effort", "miles": 2.0, "pace": "9:53"},
          {"category": "Hold Back Easy",    "miles": 1.5, "pace": "9:25"}
        ],
        "notes": "温度有点高，风大"
      },
      "planned": null
    },
    "..."
  ]
}
```

Garbage dropped from each run: `userRoles` (30 OAuth scopes!), `ownerProfileImageUrl{Small,Medium,Large}`, `summarizedDiveInfo`, `qualifyingDive`, `decoDive`, `atpActivity`, `splitSummaries` (Garmin auto-detected RWD_STAND/RUN/WALK), `fastestSplit_*`, `powerTimeInZone_*` (we keep HR-zone breakdowns at the detail level instead), `deviceId`, `timeZoneId`, `beginTimestamp`, `manufacturer`, `locationName`, `eventType`, `endLatitude`/`endLongitude`, `hasPolyline`/`hasImages`/`hasVideo`/`hasHeatMap`/`hasIntensityIntervals`/`hasSplits`, `userPro`, `privacy`, `messageIndex`, `parent`, `purposeful`, `pr`, `favorite`, `autoCalcCalories`, `elevationCorrected`, `manualActivity`, `bmrCalories`, `differenceBodyBattery`, `waterEstimated`, `vO2MaxValue` (lives in athlete profile, not per-run), `activityUUID`, `sportTypeId`, `endTimeGMT`, `minActivityLapDuration`, `aerobicTrainingEffectMessage` is condensed into `garmin_label`, etc.

---

## 5. `get_run_detail(activity_id: int)` ✱

The big one. Coach-curated single-run view with all three streams nested.

```json
{
  "id": 22739453672,
  "name": "Weehawken Running",
  "date": "2026-05-02",
  "start_time": "09:31",
  "type": "running",

  "summary": {
    "distance_mi": 10.01,
    "moving_time": "1:29:54",
    "elapsed_time": "1:43:51",
    "avg_pace": "9:02/mi",
    "elevation_gain_ft": 436,
    "elevation_loss_ft": 364,
    "calories": 1271
  },

  "objective": {
    "heart_rate": {
      "avg": 161,
      "max": 185,
      "drift_pct": 4.2,
      "zones_min": [
        {"name": "Hold Back / Recovery", "minutes": 0.0,  "pct": 0.0},
        {"name": "Steady / Constant",    "minutes": 41.0, "pct": 45.6},
        {"name": "Increasing Effort",    "minutes": 42.2, "pct": 46.9},
        {"name": "Marathon Pace",        "minutes": 1.7,  "pct": 1.9},
        {"name": "Lactate Threshold",    "minutes": 3.5,  "pct": 3.9},
        {"name": "VO2 Max",              "minutes": 0.5,  "pct": 0.6}
      ]
    },
    "training_effect": {
      "aerobic": 4.9,
      "anaerobic": 1.0,
      "load": 279,
      "garmin_label": "HIGHLY_IMPACTING_TEMPO_23"
    },
    "power": {"avg": 355, "max": 551, "normalized": 362},
    "form": {
      "cadence_avg": 170,
      "ground_contact_ms": 261,
      "stride_length_cm": 104,
      "vertical_oscillation_cm": 8.5,
      "vs_4w_baseline": "cadence stable; ground contact -3ms (more efficient than baseline)"
    },
    "splits": [
      {"mile": 1, "pace": "9:29", "hr_avg": 158, "hr_max": 169, "elev_gain_ft": 13},
      {"mile": 2, "pace": "9:38", "hr_avg": 162, "hr_max": 176, "elev_gain_ft": 102},
      "...10 splits total"
    ],
    "splits_pattern": "Negative split: avg 9:23 first 5mi → 8:49 last 5mi"
  },

  "perceived": {
    "category_breakdown": [
      {"category": "Steady Effort", "miles": 10.0, "pace": "9:02", "avg_hr": 161}
    ],
    "lap_rpe": ["Steady", "Steady", "Steady", "Steady", "Steady", "Steady", "Steady", "Steady", "Steady", "Steady"],
    "notes": "Somehow 爬坡之后 跑得快心率也不涨了",
    "labeled_at": "2026-05-02T16:34:10"
  },

  "planned": null,

  "weather": {
    "temp_f": 53,
    "feels_like_f": 50,
    "humidity_pct": 74,
    "dew_point_f": 45,
    "wind_mph": null
  },

  "route": {
    "polyline_summary": "Weehawken NJ; rolling — main climbs miles 2-3 (ascending Boulevard East to ~77ft) and 5 (Cliff section)",
    "start_latlng": [40.7734, -74.0122],
    "end_latlng": [40.7731, -74.0110]
  }
}
```

**Cross-stream coach signal example** (what the AI does with this):
- `objective.training_effect.garmin_label: HIGHLY_IMPACTING_TEMPO` (Garmin says hard tempo)
- `perceived.category_breakdown: Steady Effort` (athlete felt steady, not tempo)
- `perceived.notes: "爬坡之后 跑得快心率也不涨了"` (athlete observed: HR didn't rise as expected)
- `planned: null` (no plan)
- → Coach reasoning: positive adaptation. Body now produces tempo-grade load at steady-effort RPE. HR floor is climbing.

---

## 6. `get_run_telemetry(activity_id: int, downsample_sec: int = 30)` ✱

This tool delivers the elevation-aware AI view that already exists in
`dashboard.py` (the "🤖 AI Data View" tab) and `data_processor.df_ai`.
The trick is **`ElevationChange` per bucket** — *delta* metres climbed
in the window — not absolute altitude. With this, the AI sees one row
like "seconds 60-90: paced 9:30, HR 162, climbed 4m" and can reason
about cause (climb) → effect (HR spike) without joining two series.

Default returns the bucket frame; `raw=true` overrides to per-second.
v1 used to return both — wasteful, the LLM only needs one.

```json
{
  "activity_id": 22739453672,
  "downsample_sec": 30,
  "lap_count": 10,
  "total_buckets": 208,

  "summary": {
    "heart_rate": {"avg": 161, "min": 114, "max": 185},
    "pace": {"avg_str": "9:02", "avg_dec": 9.03, "min_str": "5:32", "max_str": "13:59"},
    "cadence": {"avg": 169, "min": 0, "max": 182},
    "elevation_total_gain_m": 133,
    "elevation_total_loss_m": 111
  },

  "drift": {
    "hr_drift_pct": 4.2,
    "elevation_adjusted_drift_pct": 1.8,
    "rationale": "Raw HR drift +4.2% (first third 158 → last third 165); ~2.4 pts of that explained by net climbing in last third (cumulative +94m). Elevation-adjusted drift 1.8% — within normal aerobic decoupling range."
  },

  "buckets": [
    {
      "second_start": 0,
      "lap": 1,
      "distance_mi": 0.0,
      "pace_str": "11:34",
      "pace_dec": 11.57,
      "heart_rate": 119,
      "cadence": 152,
      "elevation_change_m": 0.4
    },
    {
      "second_start": 30,
      "lap": 1,
      "distance_mi": 0.07,
      "pace_str": "9:48",
      "pace_dec": 9.80,
      "heart_rate": 142,
      "cadence": 168,
      "elevation_change_m": -0.2
    },
    "..."
  ]
}
```

`pace_str` + `pace_dec` is the double-track standard (resolution to Q1):
`pace_dec` (numeric min/mi) for math, `pace_str` ("9:48") for prompt
sentences. Same dual on summary.

Drops `RespirationRate` and `GroundContactBalanceLeft` when null across
the entire run. Drops `Speed_mps` (redundant with `pace_dec`).

---

## 7. `get_run_weather(activity_id: int)` ✚

Wraps existing `/api/runs/{id}/weather` endpoint into an MCP tool. Standalone for cases where the agent has only an activity id and needs weather.

```json
{
  "activity_id": 22739453672,
  "temp_f": 53,
  "feels_like_f": 50,
  "humidity_pct": 74,
  "dew_point_f": 45,
  "wind_mph": null,
  "source": "open-meteo",
  "fetched_at": "2026-05-02T13:35:00Z"
}
```

---

## 8. `list_blocks()` + `get_cycle_stats(...)` (minor cleanup)

Same shape as v1, but rename `category_breakdown` rows' field from `effort` to `perceived_category` for consistency, since these are aggregated from `manual_meta.lap_categories` (perceived stream).

---

## 9. `get_monthly_stats(activity_type)` (unchanged)

Already AI-friendly (numeric + pre-formatted pace). Keep as-is.

---

## 10. `get_calendar_events(start, end)` (unchanged)

Already source-discriminated. Keep.

---

## 11. `get_workout_plan(date: str)` ✚ Phase 2

Reads Google Calendar for events tagged `extendedProperties.private.personalcoach_training=true`. Returns the planned workout's intent for that date, or `null` if nothing planned.

```json
{
  "date": "2026-05-09",
  "planned": {
    "title": "Long run",
    "category": "Steady Effort",
    "target_distance_mi": 14,
    "target_pace": "8:45-9:15/mi",
    "target_hr_zone": "Steady / Constant",
    "calendar_event_id": "_8h336dhj...",
    "start_time": "07:00"
  }
}
```

This is what populates the `planned` field on `get_run_detail` once a planned-workout calendar event has been "consumed" by an actual completed run (Phase 3 link-back).

Phase 1: this tool returns `null` for everything. The schema is reserved.

---

## 12. `list_manual_activities` / `get_manual_activity` (unchanged)

Compact already. Add `start_time` field surfacing.

---

## 13. CME tools ✚

Cognitive Memory Engine surface — three tools that map to CME's read APIs. Pre-injection in `agentic_coach.py` stays for now; these tools let the agent query memory **on demand**, which is the eventual replacement.

### `recall_topics(status: str = "active")`

```json
{
  "topics": [
    {
      "topic_id": "tpc_0a3b...",
      "name": "膝盖外侧紧张 (ITB syndrome)",
      "root_category": "injury",
      "status": "Testing",
      "working_conclusion": "上周加冰敷+滚泡沫轴后缓解",
      "open_question": "今天跑完后 ITB 紧张感如何？比上周轻还是重？",
      "related_episode_count": 3,
      "updated_at": "2026-05-04T14:22:00"
    }
  ],
  "filter": "active"
}
```

`status` accepts `"active"` (Open + Testing), `"resolved"`, `"conflicting"`, `"all"`.

### `search_episodes(keywords: list[str], limit: int = 10)`

```json
{
  "keywords": ["hot", "long run"],
  "episodes": [
    {
      "episode_id": "ep_7f2c...",
      "timestamp": "2025-08-14",
      "event_type": "run_analysis",
      "what": "16-mile long run in 88°F heat",
      "lesson_learned": "Pace dropped 30s/mi after mile 10; HR drift 11%. Lesson: cap heat-day long runs at 12mi or move to early morning.",
      "related_topic_ids": ["tpc_heat_perf"]
    }
  ]
}
```

### `get_pending_clarifications()`

```json
{
  "pending": [
    {
      "pending_id": "pc_3a91...",
      "trigger": "Preference_Conflict",
      "question_for_user": "你之前说过 marathon pace 8:45，最近又提到 8:30 — 哪个是当前目标？",
      "created_at": "2026-05-05T10:11:00"
    }
  ]
}
```

The agent should always check this tool at the start of a session; if non-empty, it must ask the user the listed question(s) before any other coaching.

---

## What's NOT in v2 (deferred)

- **Write tools** for any of the above (Phase 2b — calendar event creation; Phase 3 — link planned workout to completed run).
- **`consolidate_memory_background` exposure**: stays as a method called by `agentic_coach.consolidate_and_learn()` after a session ends. Not an LLM-callable tool.
- **`get_active_concierge_prompts` exposure**: stays as a session-init helper.
- **Strava / external integrations**.
- **VO2max / LT pace trend over time** (could be its own tool — `get_fitness_trend(metric, weeks)` — but Phase 1 keeps it implicit via monthly stats).

---

## Resolutions (from user review 2026-05-07)

1. **Pace formatting → resolved: double-track everywhere AI might do math**.
   - Numeric (`pace_dec` decimal min/mi) + string (`pace_str` "9:02") side by
     side for any pace field. Display-only contexts can stay string-only.
   - Implemented in `get_run_telemetry`'s `summary.pace` and `buckets[].pace_*`
     above. Same convention applies to splits in `get_run_detail` and the
     `category_breakdown.pace` everywhere. **Implementation TODO**: sweep the
     other tool examples in this doc to enforce the dual.

2. **Splits unit → resolved: per-mile**. User preference is `min/mi`. Hardcoded
   for now; if multi-unit users ever appear, derive from `preferences` field.

3. **Weather on `get_run_detail` → resolved: inline**. Small payload, saves a
   tool roundtrip. `get_run_weather` stays as a standalone tool for cases
   where the agent only has an activity id.

4. **HR drift / elevation-aware → resolved**: build the bucket-level frame
   (above) and compute `elevation_adjusted_drift_pct` on top. The dashboard
   already has the drop-in primitive (`df_ai` with `ElevationChange` per
   bucket, `data_processor.py:1105`). Reusing it instead of inventing a new
   formula. The `drift` block in `get_run_telemetry` ships both raw and
   elevation-adjusted numbers + a rationale string so the AI can weigh them.

5. **`planned: null` → resolved: always emit `null`** in Phase 1 rather than
   omitting the key. Stable schema makes prompt construction reliable.

6. **CME pre-injection → resolved: keep**. Agent refactor is a separate PR.
   The `recall_topics` / `search_episodes` / `get_pending_clarifications`
   tools land in this PR but the agent doesn't switch over yet.

7. **Q: Is `list_runs` for "AI sees recent activity to manage load"?**
   Yes, but with a clear division of labour against the other recent-state
   tools:

   - **`list_runs(start, end)`** → narrative, per-run detail. "Here's what
     each run looked like in the window." Each item is compact (~12 fields)
     but shows you the *shape* of each session: distance, perceived
     category, notes, brief objective summary. Use when the agent wants to
     reason about a specific run or compare runs day-by-day ("you've done
     three Steady Effort sessions this week, all under 5mi — peak week
     mileage planned, this is light").
   - **`get_training_load(window=28)`** → aggregate, no narrative. ACWR
     ratio, weekly miles trend, total session count. Use when the agent
     wants the high-level injury-risk / fitness-trajectory signal without
     looking at individual runs ("ACWR 1.4, caution band").
   - **`get_readiness(date?)`** → today's recovery snapshot, not load.
     Sleep / RHR / HRV vs baseline. Use to decide *can the user train hard
     today* given the current state.

   These three answer different questions. The agent will typically chain
   them: `get_readiness(today)` → `get_training_load(28)` → `list_runs(last 14
   days)` → `get_run_detail(today's run)` to build a full picture.

---

## Note on `build_agent_working_memory` (data_processor.py:396)

`agentic_coach.analyze_run()` currently calls `build_agent_working_memory`,
which bundles profile + readiness + workout summary + recent manual
activities + block goal into one giant dict and stuffs it into a prompt.
With the v2 tool surface, this becomes redundant — the agent can compose
the same context from `get_athlete_profile` + `get_readiness` + 
`get_run_detail` + `list_manual_activities` + `list_blocks`, calling
each only when actually needed instead of always-prebuilding everything.

**Action**: leave `build_agent_working_memory` in place this PR (no agent
changes). Mark it `# DEPRECATED: superseded by MCP tools, remove after
agent refactor` in a docstring update so the next pass knows to delete.