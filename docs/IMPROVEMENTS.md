# Improvement Backlog

Engineering-quality improvements that aren't blocking but should land
soon. Each item is self-contained — pick one and open a PR.

Order is rough priority. See discussion 2026-05-11 for context.

---

## 1. CI + test coverage across the project

> **Status (2026-05-11)**: Phase 1 of this section is being implemented
> now — see [docs/CI.md](CI.md). Subsequent phases below remain to be
> picked up.

### Current coverage map

| Module | LOC | Existing tests | Verdict |
|---|---:|---|---|
| `api_server.py` | ~1100 | ✅ `test_endpoint_smoke.py` (65) + `test_api_server_behavior.py` (37) | Smoke no-500 + per-domain behavior on AI dispatch / mutations / memory CRUD |
| `agentic_coach.py` | ~1000 | **0** direct (PR-1 left tests dead) | Agent core |
| `cognitive_memory_engine.py` | ~1300 | ✅ `test_cme_v2.py` + `test_cme_v2b.py` (41 tests) | Best-covered, skip |
| `data_processor.py` | ~600 | ✅ `test_data_processor.py` (81 tests) | Pass 1 done; Garmin-file paths deferred |
| `llm_provider.py` | ~430 | ✅ `test_llm_provider.py` (13 tests after cleanup) | Just cleaned up |
| `personal_coach_mcp.py` | ~700 | ✅ `test_personal_coach_mcp.py` (48 tests) | All 17 tools + 4 helpers covered |
| `garmin_sync.py` | — | **0** | External API, hard to test |
| `garmin_ticket_login.py` | — | **0** | OAuth flow |
| `google_calendar.py` | — | **0** | External API |
| `migrate.py` | — | **0** | One-shot script |
| **Frontend** | — | **0 unit tests** | A few pure functions in `web/lib/*` should be tested |

The project's bulk is untested. CI rolls out in four phases:

### Phase 1 — CI infrastructure (half day)

Just lock down "current testing can't regress". **No new tests.**

1. `.github/workflows/ci.yml`:
   - `py-test`: `uv run pytest tests/`
   - `web-typecheck`: `cd web && npx tsc --noEmit`
   - `web-lint`: `cd web && npx eslint .`
2. **Same PR must fix two pre-existing TS errors** (otherwise CI is
   red from day one):
   - `web/components/health/snapshot-cards.tsx` — `tone` type
   - `web/components/setup/sync-section.tsx:145` — `<Button asChild>`
3. GitHub Settings → Branches → `main` → require these 3 status
   checks before merge.
4. `docs/CI.md` documenting how to reproduce CI locally.

### Phase 2 — Module testability (1 day) ✅ done 2026-05-12

> Test count went from 54 → 128 passing (3 integration skipped).
> See `phase2-testability` branch.

1. ✅ `AgenticCoach.__init__(skip_api_probe: bool = False)` — tests
   pass True to skip `_require_api_reachable`.
2. ✅ Moved `test_mcp_tools.py` → `scripts/manual_mcp_smoke.py` —
   it's a dev tool, not a test.
3. ✅ Shared fixtures in `tests/conftest.py`:
   - `tmp_chat_db`, `tmp_cme_db` — isolated SQLite per test
   - `mock_app_deps` (autouse) — swaps `api_server.{processor,
     gcal, memory_engine, agent}` with MagicMocks pre-configured
     with shape-correct defaults
   - `client` — FastAPI TestClient bound to the mocked app
4. ✅ `tests/test_endpoint_smoke.py` — parametrized 65-row table
   hitting every documented endpoint; asserts "no 500" + status
   code in expected set. Caught one real bug along the way:
   `GET /` returned `FileResponse("webapp/index.html")` but
   `webapp/` doesn't exist (legacy Streamlit cruft). Route
   deleted in this PR.
5. ✅ `tests/test_agentic_coach_basics.py` — unit tests of the
   constructor + module-level helpers + delete_session guard,
   now possible thanks to skip_api_probe.

### Phase 3 — Per-module coverage (one PR per module)

Can interleave — each module landing in its own focused PR.

**Backend:**

| Module | Test file | Focus |
|---|---|---|
| `data_processor.py` | `tests/test_data_processor.py` ✅ pass-1 done 2026-05-12 | 81 tests covering: RunActivity from_garmin + derived props, ManualActivity round-trip, `_bucket_run_surface`, DataProcessor bootstrap on tmp_path, semantic memory CRUD, training blocks CRUD + date validation, manual activity CRUD, `calculate_category_stats` (perceived-stream derivation), `compute_telemetry_summary` (pandas pure fn). **Pass 2 (later)** to cover the Garmin-file-dependent paths: compile_health_ledger, get_hr_zones, get_athlete_profile_full, get_readiness, get_training_load, compute_cycle_and_week_stats. |
| `agentic_coach.py` | `tests/test_agentic_coach.py` | Session lifecycle (chat → archive → list); idempotent archive; `delete_session` guards; `_started_at_from_thread_id`. Mock LangGraph agent. |
| `personal_coach_mcp.py` | `tests/test_personal_coach_mcp.py` ✅ done 2026-05-13 | 48 tests covering all 17 tools (path + params + return shape via mocked `_get`) and the 4 pure helpers (`_pace_str_from_dec`, `_format_duration`, `_split_pace_dec`, `_zones_time_min`). Pure async-via-`asyncio.run`; no pytest-asyncio dep needed. |
| `api_server.py` | `tests/test_endpoint_smoke.py` + `tests/test_api_server_behavior.py` ✅ done 2026-05-13 | 37 behavior tests on the hot paths: 5-way action dispatch + error branches; chat + sessions wire shape (`role` not `type`); training blocks / manual activities / runs laps mutation contracts (kwargs forwarded correctly, None-valued fields stripped); memory topics + episodes + pending CRUD; Garmin sync 3-outcome classification (ok / token_expired / generic-error). Smoke layer (65 routes, no-500) stays as a backstop. Per-domain split out only if any one class exceeds ~10 tests. |
| `garmin_sync.py` | `tests/test_garmin_sync.py` | Mock `garminconnect.Garmin`. Pagination, file writes, error retry. |
| `google_calendar.py` | `tests/test_google_calendar.py` | Mock `googleapiclient`. Event mapping, timezone. |

**Frontend** — first decide on a test runner. Vitest is the natural
match for Next.js 16.

| Module | Test file | Focus |
|---|---|---|
| `web/lib/coach-errors.ts` | `coach-errors.test.ts` | `classifyCoachError` is pure — all regex branches + retry hint correctness. |
| `web/lib/todays-read.ts` | `todays-read.test.ts` | `extractFirstSentence` against 10+ real LLM outputs; cache date rollover. |
| `web/lib/hooks/use-coach-session.ts` | `use-coach-session.test.ts` | Mint format, localStorage persistence, clear semantics. |
| `web/lib/format.ts` | `format.test.ts` | Date/pace/distance formatters. |

Components stay untested (cost/value isn't there at single-user scale).

### Phase 4 — Integration (1 day)

End-to-end against real Gemini key (or recorded fixtures):

- Marked `@pytest.mark.integration` (convention exists)
- Doesn't run on CI by default — `--integration` flag
- Cron once/day OR manual before each release

### Suggested cadence

| Phase | Work | Blocks next? | Doable now? |
|---|---|---|---|
| 1. CI infrastructure | half day | yes | ✅ landing |
| 2. Module testability refactor | 1 day | yes | after 1 |
| 3. Per-module coverage | 0.5–1 day per module | no | after 2 |
| 4. Integration | 1 day | no | any time |

Recommended rhythm: Phase 1 now → wait a few weeks, let real bugs
indicate which modules to prioritize → Phase 4 once we have a real
release/deploy story.

---

## 2. Move "what the agent sees" into data-layer filters, not prompt rules

**Symptom**

The prompt currently contains rules like:

> SILENTLY IGNORE Garmin's interpretive label fields — pretend they
> aren't in the data. Do not cite them, do not refute them...
>
>   • `aerobicTrainingEffect` / `anaerobicTrainingEffect`
>   • `trainingEffectLabel` (TEMPO / VO2MAX / RECOVERY / etc.)
>   • `trainingStatus`, `vO2MaxValue`, `performanceCondition`
>   • `primaryBenefit`, `primaryTrainingEffect`

But those fields are still present in the JSON the agent receives from
`get_run_detail`'s pre-fetched MCP call. We push the noise in, then
tell the LLM "don't look". That's the prompt acting as a filter.

Same shape with the three-streams rule: the prompt has a whole
"Vocabulary Trap" section explaining that `athlete.fitness.hr_zones[].
rpe_label` and `manual_meta.category_stats[].category` are different
concepts even though they share vocabulary — but the JSON keys are
opaque, so the LLM has to use prose context to tell them apart.

**Why it matters**

- Prompt is an unstable interface. Change model, change language,
  long context — rules get dropped. Already burned twice (the
  "右脚后跟" invented label, the Garmin-Tempo-as-objective bug).
- Prompt tokens are RPM/TPM budget. The 14k first-turn prompt for
  review_workout is mostly the pre-fetched JSON dump. Trimming noise
  fields would save 30%+, directly extending the free-tier ceiling.
- Shorter LLM decision path = more predictable behavior.

**Proposed change**

The MCP layer does **projection** — return a curated shape with
self-describing keys, not a Garmin raw passthrough.

```python
# personal_coach_mcp.py
@mcp.tool()
async def get_run_detail(activity_id: int) -> dict:
    raw = await fetch_from_api(f"/api/runs/{activity_id}")
    return {
        # objective stream (raw sensors only)
        "objective": {
            "distance_mi": raw["run"]["distance"] / 1609.34,
            "duration_sec": raw["run"]["duration"],
            "avg_hr": raw["run"]["averageHR"],
            "avg_pace": ...,
            "hr_drift": raw["run"]["derived"]["hr_drift"],
            "elevation_gain_ft": ...,
        },
        # perceived (short-term) stream
        "perceived_short_term": {
            "category_stats": raw["run"]["manual_meta"]["category_stats"],
            "lap_categories": raw["run"]["manual_meta"]["lap_categories"],
            "notes": raw["run"]["manual_meta"]["notes"],
        },
        # NOTE: Garmin's interpretive labels (trainingEffectLabel,
        # aerobicTrainingEffect, vO2MaxValue, ...) deliberately not
        # included. We do NOT pass-through; the data layer is where
        # noise gets filtered, not the prompt.
    }
```

Same for `get_athlete_profile`: rename `hr_zones` → `perceived_medium_term_hr_mapping` so the schema itself tells the LLM which stream the data belongs to.

**Spillover benefits**

- Prompt's "Streams (NEVER collapse them)" section can shrink ~60%.
  Rules move into schema names; we don't repeatedly teach the LLM.
- `_REVIEW_WORKOUT_INSTRUCTIONS` Step 1 / Step 2 / Step 3 become
  shorter — the data structure already enforces the read order.
- Easier to add new derived data layer fields without prompt
  rewrites.

**Risk**

- Frontend consumers of `get_run_detail` (if any go through MCP
  directly rather than the FastAPI endpoint) need migration.
- The FastAPI `/api/runs/{activity_id}` endpoint is currently the
  pre-fetch source — we'd either project at the MCP layer (cleaner)
  or at the FastAPI layer (saves a round of data shaping but couples
  the API to AI consumers' needs). Recommend the MCP-layer
  projection.

---

## 3. Structured tracing — replayable agent runs

**Symptom**

When the user reports "the agent didn't see my lap labels", how do
we currently debug?

1. Manually curl the same `review_workout` with the same `activity_id`
2. Read the returned JSON, guess which tools the agent called
3. Stare at the prompt and the agent response, guess which clause
   the LLM followed or skipped
4. Edit the prompt, repeat

There is no:

- Record of which tool calls happened on the original failing turn
- Token counts (input/output) per LLM call
- Snapshot of the system prompt as it stood when the bad reply was
  generated (we've iterated through ~6 versions; no version stamping)
- Trail through `consolidate_memory_background` showing whether the
  CME LLM proposed 0 topics or proposed N and they all auto-merged
  (today `topics_added: 0` covers all three failure modes silently)

**Why it matters**

- Prompt iteration loop has no memory. "Why did review_health stop
  including Garmin labels?" — we can't answer without re-running
  the regression.
- LLM behavior isn't reproducible across calls. Without trace we
  can't distinguish "the prompt is wrong" from "the LLM had a bad
  draw".
- CME is the most opaque part of the system. The agent's memory
  formation happens in a background `consolidate_memory_background`
  call the user never sees. When memories don't form, there's no
  feedback loop.

**Proposed change — MVP (one day)**

```python
# trace_logger.py
@dataclass
class Trace:
    turn_id: str           # uuid per agent turn
    thread_id: str
    timestamp: str
    prompt_version: str    # PROMPT_VERSION constant, bumped per prompt edit
    prompt_hash: str       # sha1 of _SYSTEM_PROMPT, redundant with version
                           # but catches typos / partial saves
    user_input: str
    tool_calls: list[dict] # [{name, args, latency_ms, result_preview}]
    llm_calls: list[dict]  # [{provider, model, input_tokens,
                           #   output_tokens, latency_ms,
                           #   finish_reason}]
    final_answer: str
    error: str | None
```

Write JSONL per day under `data/traces/2026-05-11.jsonl`. Add
middleware to FastAPI `/api/ai/chat` and `/api/ai/action/*` to emit
one Trace row per turn. Same for `consolidate_memory_background` —
emit a row with `{proposed_topics, accepted_topics, auto_merged,
parked_in_topic_decisions}`.

Add a `PROMPT_VERSION = "v7"` constant in `agentic_coach.py` and a
short `docs/PROMPT_CHANGELOG.md` mapping each version to what
changed.

**Proposed change — medium term**

Wire up **LangSmith** (langchain's tracing UI). We already have
`langsmith` as a transitive dep. With `LANGSMITH_API_KEY` env var
set, every agent turn auto-traces to a queryable UI. No code change
beyond the env var.

LangSmith gives:
- Token counts and latencies per step
- Tool call inspection (args + return)
- Trace replay (re-run a recorded turn through a different prompt
  version to A/B)
- Comments / annotations on traces

**Spillover benefit**

Trace data feeds capacity planning. Currently we estimate "review_workout
prompt is ~14k tokens" from the one 413 error we hit. With traces we
have real distributions per action type. RPM/TPM budget decisions
become data, not gut.

**Risk**

- PII / health data in traces. Traces live on disk locally (MVP);
  for LangSmith they'd ship to Anthropic's cloud peer — review
  whether the user is OK with that. Probably yes since they're
  the only user.
- Disk usage. JSONL grows. Rotate weekly (drop > 30 day).

---

## 4. Repo layout reorg — group by role (frontend / backend / scripts)

> **Status (2026-05-13)**: PR A landing now. PRs B + C scheduled
> after.

### Current pain

Top-level is a flat dump of 9 backend `.py` files (api_server,
agentic_coach, data_processor, …), 1 dead legacy (dashboard.py), a
one-off CLI (migrate.py), a sibling `migrations/` dir of one-off
schema scripts, a `deprecated/` dir of dead code, a `scripts/` dir
with a single dev tool, and a stray `cme.db` test artifact at the
repo root. There's no visual signal for "what's the agent vs what's
the integration vs what's a CLI tool".

### Target layout

```
PersonalCoach/
├── web/                          # frontend (Next.js)
│
├── backend/                      # ← all server-side Python
│   ├── api_server.py
│   ├── agentic_coach.py
│   ├── cognitive_memory_engine.py
│   ├── data_processor.py
│   ├── llm_provider.py
│   ├── personal_coach_mcp.py
│   ├── garmin_sync.py
│   ├── garmin_ticket_login.py
│   └── google_calendar.py
│
├── scripts/                      # ← CLI + one-off tools
│   ├── manual_mcp_smoke.py       (already here)
│   ├── migrate_garmin_token.py   (renamed from top-level migrate.py)
│   └── migrations/               (moved from top-level migrations/)
│       ├── v2_cme_schema.py
│       ├── v3_dedupe_topics.py
│       └── v4_link_episodes.py
│
├── tests/                        # unchanged
├── docs/                         # unchanged
├── data/                         # unchanged (runtime, gitignored)
│
├── pyproject.toml  uv.lock  README.md  LICENSE.TXT
└── .env  .gitignore  .python-version
```

Plus removals: `deprecated/`, `dashboard.py`, `.streamlit/`, root
`cme.db`, the `streamlit` dependency in pyproject.

### Phased rollout — 3 PRs

**PR A — Delete dead code (low risk, ~30 min)**
- Remove `deprecated/` (3 unimported files)
- Remove `dashboard.py` (1318 lines of legacy Streamlit, no
  imports anywhere)
- Remove `.streamlit/` config dir
- Remove root `cme.db` (test artifact, never should have been
  tracked) and add to `.gitignore`
- Remove `streamlit==1.55.0` from pyproject deps
- Refresh stale comments in `agentic_coach.py` that reference
  "streamlit / dashboard" callers
- Update `README.md` attribution that thanks Streamlit

**PR B — Move backend Python under `backend/` (1 day, biggest risk)** ✅ done 2026-05-13
- ✅ `git mv` 9 .py files into `backend/`
- ✅ Added `backend/__init__.py` documenting entry points
- ✅ Rewrote every `from data_processor import X` →
  `from backend.data_processor import X` across tests, migrations,
  `migrate.py`, sibling backend modules
- ✅ `import api_server` in conftest → `import backend.api_server as api_server`
- ✅ `patch("X.…")` targets in tests → `patch("backend.X.…")`
- ✅ Subprocess invocations:
  - `subprocess.run([sys.executable, "garmin_sync.py", ...])`
    → `[..., "-m", "backend.garmin_sync", ...]` (api_server)
  - same for `garmin_ticket_login.py`
  - MCP spawn: `uv run python -m personal_coach_mcp` →
    `uv run python -m backend.personal_coach_mcp` (agentic_coach +
    scripts/manual_mcp_smoke)
- ✅ `uvicorn api_server:app` → `uvicorn backend.api_server:app` in
  `.claude/launch.json` and the error message inside
  agentic_coach._require_api_reachable
- ✅ No pyproject.toml change needed — Python 3.12 treats a dir with
  `__init__.py` at the project root as importable as long as cwd is
  on `sys.path`, which it is for both uvicorn and `python -m`. Verified
  with smoke-import of all three entry points.
- ✅ 209 tests still pass

**PR C — Move CLI + migrations to `scripts/` (~30 min)** ✅ done 2026-05-13
- ✅ `git mv migrate.py scripts/migrate_garmin_token.py`
- ✅ `git mv migrations/ scripts/migrations/`
- ✅ Added `scripts/__init__.py` + `scripts/migrations/__init__.py`
  (explicit package markers, mirroring `backend/__init__.py` from
  PR B; lets `python -m scripts.migrate_garmin_token` and
  `python -m scripts.migrations.vN_*` work cleanly)
- ✅ Updated v4 docstring usage:
  `python -m migrations.v4_link_episodes` →
  `python -m scripts.migrations.v4_link_episodes`
- ✅ **Fixed real bug along the way**: `v3_dedupe_topics.py` and
  `v4_link_episodes.py` both did
  `_ROOT = Path(__file__).resolve().parent.parent` assuming the file
  sat at `migrations/X.py` (two levels deep). After the move the file
  is at `scripts/migrations/X.py` (three levels), so `_ROOT` would
  point at `scripts/` not the repo root — `from backend.X import Y`
  inside the migration would fail at runtime. Bumped to
  `.parent.parent.parent`.
- ✅ README.md `uv run python migrate.py` →
  `uv run python -m scripts.migrate_garmin_token`
- ✅ docs/architecture.md: Backend table now lists modules under
  `backend/` with full paths; CLI subgraph + table now lists
  `scripts/*` with new names. Dropped the (already-deleted)
  `dashboard.py` row. Updated the Mermaid edges to show the
  subprocess spawn from api_server → backend.garmin_sync, and
  migrations writing to cognition.db.
- ✅ No test imports `migrations` directly, so no test refactor
  needed.

---

## (5 and beyond — open for additions)

Ideas not yet developed:

- **Frontend backups for CME** — `cognition.db` corruption today =
  lose every topic/episode. `data/backups/` exists but isn't
  written to.
- **Multi-user readiness** — `thread_id`, OAuth tokens, athlete
  profile all assume one user. Retrofit cost is high.
- **i18n** — UI chrome English, AI Chinese. Works for current
  bilingual user but is a smell.
- **Streaming SSE for /api/ai/chat** — deferred per Coach design
  doc. Real value-add for long tool-using turns.
- **Persist CME embeddings to SQLite** — at ~300 topics cold-start
  cost starts mattering (see cognitive_memory_engine.py comments).
