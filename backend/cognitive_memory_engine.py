"""
Cognitive Memory Engine (CME) — "知行合一" Memory-as-a-Service

Three-dimensional hybrid memory model:
  "我" (Semantic Identity) — user profile, baselines, preferences
  "知" (Topics / Cognitive Graph) — structured knowledge with state machine (Open → Testing → Resolved)
  "行" (Episodes) — event-centric 5W1H+E capsules with lessons learned

The agent interacts with this engine exclusively through 4 APIs:
  1. retrieve_working_context()   — assemble context for each user message
  2. resolve_pending_question()   — close a pending clarification
  3. consolidate_memory_background() — post-conversation memory consolidation
  4. get_active_concierge_prompts()  — proactive greeting / follow-up prompts
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, ClassVar

from backend.coach_intake import (
    ALL_AREAS,
    CYCLE_SLOTS,
    PROFILE_SLOTS,
    SLOT_BY_AREA,
    CoachSlot,
    event_type_for_area,
)
from backend.llm_provider import call_embedding, call_llm, cosine_similarity
from backend.trace_logger import TraceLogger


# Version label for the CME consolidate prompt. Bump when
# consolidate_memory_background's prompt template changes. Embedded in
# every trace row so we can grep "all consolidates on v2b" or detect
# behavior regressions across prompt edits.
CME_CONSOLIDATE_VERSION = "v2b"


# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS topics (
    topic_id          TEXT PRIMARY KEY,
    root_category     TEXT NOT NULL,
    name              TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'Open'
                      CHECK(status IN ('Open', 'Testing', 'Resolved')),
    working_conclusion TEXT,              -- nullable while Open
    related_episodes  TEXT DEFAULT '[]',  -- JSON array of episode_ids
    related_models    TEXT DEFAULT '[]',  -- JSON array of model_ids (PR P1)
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id        TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,       -- when row was created
    event_type        TEXT NOT NULL,
    context_json      TEXT NOT NULL,       -- full 5W1H+E JSON
    lesson_learned    TEXT,
    related_topic_ids TEXT DEFAULT '[]',   -- JSON array (legacy; canonical is junction)
    created_at        TEXT NOT NULL,
    event_timestamp   TEXT,                -- ISO date the event actually happened (≠ row creation)
    event_date_text   TEXT,                -- user's original date phrase ("上周三" etc.)
    timestamp_source  TEXT                 -- 'user_explicit' | 'caller' | 'unknown'
);

CREATE TABLE IF NOT EXISTS pending_clarifications (
    pending_id         TEXT PRIMARY KEY,
    trigger_type       TEXT NOT NULL
                       CHECK(trigger_type IN ('Entity_Conflict', 'Preference_Conflict')),
    question_for_user  TEXT NOT NULL,
    resolution_callback TEXT NOT NULL,     -- JSON describing the action
    is_resolved        INTEGER DEFAULT 0,
    resolution_answer  TEXT,
    created_at         TEXT NOT NULL,
    resolved_at        TEXT
);

-- v2b queue for low-confidence consolidation proposals.
-- When LLM proposes a new topic or conflict but the embedding matcher
-- can't cross MATCH_THRESHOLD against any existing topic, we park it here
-- instead of creating a possibly-duplicate row. UI surfaces `status='pending'`
-- rows for the user to confirm: merge-into-X / create-new / reject.
-- PR P1 added `new_model` kind so the same queue carries pattern-store
-- proposals (LLM proposes "promote these 7 episodes to a parameterized
-- recovery curve model") routed through the same confirm-or-reject UI.
CREATE TABLE IF NOT EXISTS topic_decisions (
    decision_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK(kind IN (
                      'new_topic', 'conflict', 'episode_linking', 'new_model'
                    )),
    proposal_json   TEXT NOT NULL,           -- LLM proposal as emitted
    candidates_json TEXT NOT NULL DEFAULT '[]',  -- [{topic_id, name, status, score}, ...]
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'merged', 'created', 'rejected', 'linked')),
    resolution      TEXT,                     -- "merged:tpc_xxx" | "created:tpc_xxx" | "linked:tpc_a,tpc_b" | "rejected" | "created:mdl_xxx"
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

-- Junction: topic ↔ episode (v2 canonical, replaces the legacy
-- JSON array on topics.related_episodes + episodes.related_topic_ids).
-- This table has existed in production cognition.db since the v2
-- migration but was never added to _SCHEMA_SQL, which meant fresh
-- DBs (tests, ad-hoc scripts) had to bootstrap it manually. Added
-- here in PR P2 since propose_model_from_topic / get_topic_episodes
-- now exercise it from fresh DBs in tests. Idempotent CREATE IF NOT
-- EXISTS so existing DBs (which have it) aren't affected.
CREATE TABLE IF NOT EXISTS topic_episode_links (
    topic_id   TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (topic_id, episode_id),
    FOREIGN KEY (topic_id)   REFERENCES topics(topic_id)   ON DELETE CASCADE,
    FOREIGN KEY (episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tel_episode ON topic_episode_links(episode_id);
CREATE INDEX IF NOT EXISTS idx_tel_topic   ON topic_episode_links(topic_id);

-- Pattern store (PR P1). Parallel to `episodes`: each row is a
-- parameterized observation about the user — "your HRV recovery
-- curve", "your heat response", "your weekday quality pattern" —
-- not a free-text concept (those stay in topics) and not a single
-- event (those stay in episodes). Models accumulate over time and
-- get refit incrementally as new data arrives. Topics point at
-- models via topics.related_models; the agent can ask for either
-- the conceptual summary (topic.working_conclusion) or the
-- parameterized version (model.params_json).
CREATE TABLE IF NOT EXISTS models (
    model_id          TEXT PRIMARY KEY,            -- mdl_xxxxxx
    model_key         TEXT UNIQUE NOT NULL,        -- "recovery.hrv_curve_post_long_run"
    name              TEXT NOT NULL,               -- human label, may be Chinese
    category          TEXT NOT NULL,               -- "Health/Recovery", "Running/Performance"...
    model_type        TEXT NOT NULL
                      CHECK(model_type IN (
                        'decay', 'linear_trend', 'mean_std',
                        'ordinal_score', 'rate', 'fixed_obs'
                      )),
    params_json       TEXT NOT NULL,               -- shape determined by model_type
    n_samples         INTEGER NOT NULL DEFAULT 0,
    confidence        TEXT CHECK(confidence IN ('low', 'medium', 'high')),
    evidence_json     TEXT,                        -- {"episodes":[], "activities":[], "dates":[]}
    derivation_method TEXT NOT NULL
                      CHECK(derivation_method IN ('stat', 'llm', 'hybrid')),
    status            TEXT NOT NULL DEFAULT 'Forming'
                      CHECK(status IN ('Forming', 'Stable', 'Stale', 'Drifting')),
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    last_verified_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp);
CREATE INDEX IF NOT EXISTS idx_pending_unresolved ON pending_clarifications(is_resolved);
CREATE INDEX IF NOT EXISTS idx_topic_decisions_pending ON topic_decisions(status);
CREATE INDEX IF NOT EXISTS idx_models_category ON models(category);
CREATE INDEX IF NOT EXISTS idx_models_status   ON models(status);
"""


# ---------------------------------------------------------------------------
# MemoryOS — the public interface
# ---------------------------------------------------------------------------
class MemoryOS:
    """Cognitive Memory Engine.  Agent-facing memory micro-service."""

    def __init__(
        self,
        db_path: str = "data/cognition.db",
        semantic_profile_path: str = "data/memory/user_profile.json",
    ):
        self.db_path = db_path
        self.semantic_profile_path = semantic_profile_path

        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        self._migrate_topic_decisions_check()
        self._migrate_topics_related_models()
        self._migrate_episodes_event_timestamp_columns()

        # In-memory topic-embedding cache.
        #
        # Key:   (provider, topic_id)  — the provider component prevents
        #        a mid-process change of TOPIC_EMBED_PROVIDER from causing
        #        old vectors (in space A) to be cosine-compared with new
        #        vectors (in space B) silently. Different providers →
        #        different cache slots → no cross-space contamination.
        # Value: (signature, vector) — the `signature` is a hash of the
        #        topic_signature_text (name + working_conclusion + open_question).
        #        If it differs from the stored signature we re-embed, so
        #        stale entries self-heal after update_topic() without
        #        needing explicit invalidation.
        #
        # NOTE: not persisted. Process restart = empty cache = vectors
        # get recomputed lazily on next find_matching_topic call. At
        # 11 topics today this is ~1s of API time. If we ever cross
        # 300+ topics, persist this to a `topic_embeddings` table
        # (PRIMARY KEY (provider, topic_id), columns signature + vec_blob)
        # — at that scale the cold-start latency starts to matter.
        self._topic_embeddings: dict[tuple[str, str], tuple[str, list[float]]] = {}

        # Structured tracing. Same JSONL store as AgenticCoach
        # (data/traces/YYYY-MM-DD.jsonl); we just emit different
        # `kind` values so grep can filter agent vs memory turns.
        self.tracer = TraceLogger()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _migrate_topic_decisions_check(self) -> None:
        """
        CHECK constraints in SQLite are immutable; if the existing DB was
        created before the current set of legal `kind`/`status` values,
        rebuild the table. Idempotent: noop when the current schema
        already allows everything we need.

        Tracked legal values (must match _SCHEMA_SQL):
          kind:   'new_topic', 'conflict', 'episode_linking', 'new_model'
          status: 'pending', 'merged', 'created', 'rejected', 'linked'

        PR #66 (Reorg) added 'episode_linking' + 'linked'.
        PR P1 added 'new_model' for the pattern-store proposal queue.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='topic_decisions'"
        ).fetchone()
        if not row or not row["sql"]:
            return
        sql = row["sql"]
        needed_kinds = ("new_topic", "conflict", "episode_linking", "new_model")
        needed_statuses = ("pending", "merged", "created", "rejected", "linked")
        if all(k in sql for k in needed_kinds) and all(s in sql for s in needed_statuses):
            return  # already migrated

        self.conn.executescript(
            """
            BEGIN;
            CREATE TABLE topic_decisions_new (
                decision_id     TEXT PRIMARY KEY,
                kind            TEXT NOT NULL CHECK(kind IN (
                                  'new_topic', 'conflict', 'episode_linking', 'new_model'
                                )),
                proposal_json   TEXT NOT NULL,
                candidates_json TEXT NOT NULL DEFAULT '[]',
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'merged', 'created', 'rejected', 'linked')),
                resolution      TEXT,
                created_at      TEXT NOT NULL,
                resolved_at     TEXT
            );
            INSERT INTO topic_decisions_new SELECT * FROM topic_decisions;
            DROP TABLE topic_decisions;
            ALTER TABLE topic_decisions_new RENAME TO topic_decisions;
            CREATE INDEX IF NOT EXISTS idx_topic_decisions_pending ON topic_decisions(status);
            COMMIT;
            """
        )

    def _migrate_topics_related_models(self) -> None:
        """PR P1: topics.related_models holds JSON list of model_ids
        that point at the parameterized version of this topic's belief.
        For fresh DBs, _SCHEMA_SQL already includes the column; for
        DBs created pre-P1, ALTER ADD COLUMN is safe + cheap.
        Idempotent: checks PRAGMA before issuing the ALTER."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(topics)")}
        if "related_models" in cols:
            return
        self.conn.execute(
            "ALTER TABLE topics ADD COLUMN related_models TEXT DEFAULT '[]'"
        )
        self.conn.commit()

    def _migrate_episodes_event_timestamp_columns(self) -> None:
        """PR P2: episodes carry event_timestamp / event_date_text /
        timestamp_source (the LLM-derived event-time tracking). These
        existed in production cognition.db from a prior ALTER but
        were never added to _SCHEMA_SQL, so fresh DBs hit
        sqlite3.OperationalError on get_topic_episodes (which JOINs
        ORDER BY COALESCE(event_timestamp, timestamp)). Idempotent:
        skip any column already present."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(episodes)")}
        additions = [
            ("event_timestamp", "TEXT"),
            ("event_date_text", "TEXT"),
            ("timestamp_source", "TEXT"),
        ]
        for name, type_ in additions:
            if name not in cols:
                self.conn.execute(
                    f"ALTER TABLE episodes ADD COLUMN {name} {type_}"
                )
        self.conn.commit()

    def _now(self) -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    def _load_semantic_profile(self) -> dict:
        try:
            with open(self.semantic_profile_path, "r") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _llm_invoke(self, prompt: str) -> str:
        from langchain_core.messages import HumanMessage, SystemMessage

        # Route through groq-first fallback. CME consolidation is a
        # background job that fires on every End & Save (and on every
        # imported workout via the episodic path) — keeping it off the
        # gemini RPM budget protects user-facing chat/action latency.
        # Falls back to gemini if Groq is down or Llama struggles with
        # the JSON shape on a particular input.
        msg, _ = call_llm(
            [
                SystemMessage(content="You are a memory analysis assistant. Always respond in valid JSON when asked for JSON."),
                HumanMessage(content=prompt),
            ],
            role="structured",
            fallback_chain=["groq", "gemini"],
        )
        return str(msg.content).strip()

    # ------------------------------------------------------------------
    # Topic CRUD
    # ------------------------------------------------------------------
    def create_topic(
        self,
        name: str,
        root_category: str,
        status: str = "Open",
        working_conclusion: str | None = None,
        related_episodes: list[str] | None = None,
    ) -> str:
        topic_id = f"tpc_{uuid.uuid4().hex[:8]}"
        now = self._now()
        self.conn.execute(
            """INSERT INTO topics
               (topic_id, root_category, name, status, working_conclusion,
                related_episodes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                topic_id,
                root_category,
                name,
                status,
                working_conclusion,
                json.dumps(related_episodes or []),
                now,
                now,
            ),
        )
        self.conn.commit()
        return topic_id

    def update_topic(self, topic_id: str, **kwargs) -> bool:
        row = self.conn.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if not row:
            return False

        allowed = {
            "name", "root_category", "status", "working_conclusion", "related_episodes"
        }
        sets, vals = [], []
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k == "related_episodes":
                v = json.dumps(v)
            sets.append(f"{k} = ?")
            vals.append(v)

        if not sets:
            return False

        sets.append("updated_at = ?")
        vals.append(self._now())
        vals.append(topic_id)

        self.conn.execute(
            f"UPDATE topics SET {', '.join(sets)} WHERE topic_id = ?", vals
        )
        self.conn.commit()
        return True

    def get_topic(self, topic_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["related_episodes"] = json.loads(d["related_episodes"])
        # related_models added in PR P1; legacy rows (pre-migration)
        # may have NULL in this column — coerce to [].
        d["related_models"] = json.loads(d.get("related_models") or "[]")
        return d

    def list_topics(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM topics WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM topics ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["related_episodes"] = json.loads(d["related_episodes"])
            d["related_models"] = json.loads(d.get("related_models") or "[]")
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Episode CRUD
    # ------------------------------------------------------------------
    def create_episode(
        self,
        event_type: str,
        context: dict,
        lesson_learned: str | None = None,
        related_topic_ids: list[str] | None = None,
        timestamp: str | None = None,
    ) -> str:
        episode_id = f"epi_{uuid.uuid4().hex[:8]}"
        now = self._now()
        # Legacy JSON array column stays populated during transition; the
        # canonical source of truth going forward is topic_episode_links.
        self.conn.execute(
            """INSERT INTO episodes
               (episode_id, timestamp, event_type, context_json,
                lesson_learned, related_topic_ids, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                episode_id,
                timestamp or now,
                event_type,
                json.dumps(context, ensure_ascii=False),
                lesson_learned,
                json.dumps(related_topic_ids or []),
                now,
            ),
        )
        for tid in related_topic_ids or []:
            self.conn.execute(
                """INSERT OR IGNORE INTO topic_episode_links
                   (topic_id, episode_id, created_at) VALUES (?, ?, ?)""",
                (tid, episode_id, now),
            )
        self.conn.commit()
        return episode_id

    def _find_duplicate_episode(
        self, thread_id: str, event_type: str, what: str
    ) -> str | None:
        """
        Return the episode_id of an existing row with the same (thread, type, what),
        or None if no match. Used by consolidation to skip re-extraction of the same
        historical fact across consecutive runs.
        """
        if not what:
            return None
        row = self.conn.execute(
            """SELECT episode_id FROM episodes
               WHERE event_type = ?
                 AND json_extract(context_json, '$.source_thread') = ?
                 AND json_extract(context_json, '$.what') = ?
               LIMIT 1""",
            (event_type, thread_id, what),
        ).fetchone()
        return row["episode_id"] if row else None

    def add_topic_episode_link(self, topic_id: str, episode_id: str) -> bool:
        """
        Link an existing episode to an existing topic. Idempotent — re-linking
        the same pair is a no-op. Use this when the connection is discovered
        after the episode was already created (e.g., topic matcher runs later).
        """
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO topic_episode_links
               (topic_id, episode_id, created_at) VALUES (?, ?, ?)""",
            (topic_id, episode_id, self._now()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_topic_episodes(self, topic_id: str, limit: int = 20) -> list[dict]:
        """Return episodes linked to a topic via the junction table."""
        rows = self.conn.execute(
            """SELECT e.* FROM episodes e
               JOIN topic_episode_links l ON l.episode_id = e.episode_id
               WHERE l.topic_id = ?
               ORDER BY COALESCE(e.event_timestamp, e.timestamp) DESC
               LIMIT ?""",
            (topic_id, limit),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["context"] = json.loads(d["context_json"])
            del d["context_json"]
            d["related_topic_ids"] = json.loads(d["related_topic_ids"])
            result.append(d)
        return result

    def list_episodes(self, limit: int = 20, event_type: str | None = None) -> list[dict]:
        if event_type:
            rows = self.conn.execute(
                "SELECT * FROM episodes WHERE event_type = ? ORDER BY timestamp DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM episodes ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["context"] = json.loads(d["context_json"])
            del d["context_json"]
            d["related_topic_ids"] = json.loads(d["related_topic_ids"])
            result.append(d)
        return result

    # Event types that act as "external context" for a run — the
    # things that explain why a sensor number looked the way it did
    # without being IN the sensor stream (P5 §4). Centralized here so
    # the API endpoint, MCP tool, and any future migration share
    # one source of truth.
    EXTERNAL_EVENT_TYPES: ClassVar[tuple[str, ...]] = (
        "travel", "illness", "life_stress",
    )

    def list_external_events(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Return external-context episodes (travel / illness /
        life_stress) whose date range overlaps [start_date, end_date]
        (YYYY-MM-DD, inclusive on both sides).

        Each event's `context` dict carries `start_date` + `end_date`;
        an event qualifies when `event.start_date <= range.end_date`
        AND `event.end_date >= range.start_date`.

        Date-resolution fallback chain (most to least authoritative):
          1. context.start_date / context.end_date (the normal path —
             this is what `create_external_event` writes).
          2. episode.timestamp[:10] (legacy / partially-filled rows
             saved before P5 standardized the context shape).
          3. episode.created_at[:10] (sentinel — for the case where
             both context dates AND timestamp are missing, which
             should never happen via the API but can if a row was
             hand-edited or imported from elsewhere).

        Rows for which ALL THREE are missing are skipped rather than
        included with empty-string sentinels (empty-string sort places
        them before any real date, which is misleading; and an event
        with no date is functionally indistinguishable from "no event
        recorded").

        Returns episodes ordered earliest-first by start_date (the
        agent reasons over the timeline), unlike list_episodes which
        is recency-first.
        """
        placeholders = ",".join("?" * len(self.EXTERNAL_EVENT_TYPES))
        rows = self.conn.execute(
            f"""SELECT * FROM episodes
                WHERE event_type IN ({placeholders})
                ORDER BY timestamp ASC""",
            self.EXTERNAL_EVENT_TYPES,
        ).fetchall()

        out: list[dict] = []
        for r in rows:
            d = dict(r)
            ctx = json.loads(d["context_json"])
            d["context"] = ctx
            del d["context_json"]
            d["related_topic_ids"] = json.loads(d["related_topic_ids"])
            # Try each fallback in order; bail if all empty so we
            # don't ship a "" sentinel that distorts the timeline.
            ev_start = (
                ctx.get("start_date")
                or (d["timestamp"] or "")[:10]
                or (d.get("created_at") or "")[:10]
            )
            if not ev_start:
                continue
            ev_end = (
                ctx.get("end_date")
                or (d["timestamp"] or "")[:10]
                or (d.get("created_at") or "")[:10]
                or ev_start
            )
            # Range overlap.
            if ev_start <= end_date and ev_end >= start_date:
                # Promote derived range to top level for caller
                # convenience (agent doesn't have to dig into
                # context to know span).
                d["start_date"] = ev_start
                d["end_date"] = ev_end
                out.append(d)
        out.sort(key=lambda e: e.get("start_date") or "")
        return out

    def delete_episode(self, episode_id: str) -> bool:
        """Hard-delete an episode + its topic links. Used by the
        external-events UI when the user removes a logged event.
        Returns True when a row was removed, False when nothing
        matched (idempotent — UI delete sweeps don't 404 on a
        re-click)."""
        cur = self.conn.execute(
            "DELETE FROM episodes WHERE episode_id = ?", (episode_id,)
        )
        self.conn.execute(
            "DELETE FROM topic_episode_links WHERE episode_id = ?",
            (episode_id,),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def search_episodes(self, keywords: list[str], limit: int = 10) -> list[dict]:
        """Simple keyword search across context_json and lesson_learned."""
        conditions = []
        params: list[Any] = []
        for kw in keywords:
            conditions.append(
                "(context_json LIKE ? OR lesson_learned LIKE ? OR event_type LIKE ?)"
            )
            like = f"%{kw}%"
            params.extend([like, like, like])

        where = " OR ".join(conditions) if conditions else "1=1"
        rows = self.conn.execute(
            f"SELECT * FROM episodes WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            params + [limit],
        ).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            d["context"] = json.loads(d["context_json"])
            del d["context_json"]
            d["related_topic_ids"] = json.loads(d["related_topic_ids"])
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Model CRUD (PR P1 — pattern store)
    # ------------------------------------------------------------------
    #
    # Models are parameterized observations about the user, parallel
    # to episodes. See docs/coach_brain_design.md §"CME schema upgrade"
    # for the design rationale.
    #
    # SQLite CHECK can't express "field X required when kind=Y", so
    # we validate model_type-specific params in app code via
    # _validate_params. Schema-level CHECK still guards the enums
    # (model_type, status, derivation_method, confidence).

    _MODEL_TYPES = (
        "decay",          # post-X recovery curves: {peak_drop_day, peak_drop_pct, ...}
        "linear_trend",   # X-vs-Y slope: {slope, intercept, monthly_change_pct, ...}
        "mean_std",       # baseline: {mean, sd, threshold_warning, ...}
        "ordinal_score",  # per-bucket scores: {scores: {mon: 0.8, tue: 0.5, ...}}
        "rate",           # rate/ratio: {by_topic: {volume_up: 0.7, ...}}
        "fixed_obs",      # one-off observation snapshot: free shape
    )
    _MODEL_STATUSES = ("Forming", "Stable", "Stale", "Drifting")
    _DERIVATION_METHODS = ("stat", "llm", "hybrid")
    _CONFIDENCE_LEVELS = ("low", "medium", "high")

    @staticmethod
    def _validate_params(model_type: str, params: dict) -> None:
        """Light type-aware validation. Each model_type has a small
        set of expected keys; we warn on missing but don't enforce —
        the param shape may evolve and we don't want to block a real
        observation on a strict schema check. Hard errors only for
        obvious mistakes (params isn't a dict, model_type not in
        the enum)."""
        if not isinstance(params, dict):
            raise ValueError(f"params_json must be a dict, got {type(params).__name__}")
        # model_type enum is enforced by SQLite CHECK; this is the
        # app-layer mirror for clearer errors before INSERT.
        if model_type not in MemoryOS._MODEL_TYPES:
            raise ValueError(
                f"model_type {model_type!r} not in {MemoryOS._MODEL_TYPES}"
            )

    def create_model(
        self,
        *,
        model_key: str,
        name: str,
        category: str,
        model_type: str,
        params_json: dict,
        derivation_method: str,
        n_samples: int = 0,
        confidence: str | None = None,
        evidence_json: dict | None = None,
        status: str = "Forming",
    ) -> str:
        """Insert a new model row. Returns the generated model_id.

        Raises ValueError on bad enums (model_type / status /
        derivation_method / confidence) or non-dict params. Raises
        sqlite3.IntegrityError on duplicate model_key (UNIQUE)."""
        self._validate_params(model_type, params_json)
        if status not in self._MODEL_STATUSES:
            raise ValueError(f"status {status!r} not in {self._MODEL_STATUSES}")
        if derivation_method not in self._DERIVATION_METHODS:
            raise ValueError(
                f"derivation_method {derivation_method!r} not in {self._DERIVATION_METHODS}"
            )
        if confidence is not None and confidence not in self._CONFIDENCE_LEVELS:
            raise ValueError(
                f"confidence {confidence!r} not in {self._CONFIDENCE_LEVELS}"
            )

        model_id = f"mdl_{uuid.uuid4().hex[:8]}"
        now = self._now()
        self.conn.execute(
            """INSERT INTO models
               (model_id, model_key, name, category, model_type,
                params_json, n_samples, confidence, evidence_json,
                derivation_method, status, created_at, updated_at,
                last_verified_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                model_id, model_key, name, category, model_type,
                json.dumps(params_json, ensure_ascii=False),
                n_samples, confidence,
                json.dumps(evidence_json or {}, ensure_ascii=False),
                derivation_method, status, now, now, now,
            ),
        )
        self.conn.commit()
        return model_id

    def get_model(self, model_key: str) -> dict | None:
        """Return a model by its stable key, or None if missing.
        params_json and evidence_json are deserialized for caller
        convenience — same convention as get_topic."""
        row = self.conn.execute(
            "SELECT * FROM models WHERE model_key = ?", (model_key,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["params_json"] = json.loads(d["params_json"] or "{}")
        d["evidence_json"] = json.loads(d["evidence_json"] or "{}")
        return d

    def list_models(
        self,
        category: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        """List models, optionally filtered by category prefix or
        status enum. Returns rows with JSON fields deserialized."""
        clauses, params = [], []
        if category:
            clauses.append("category LIKE ?")
            params.append(f"{category}%")
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM models {where} ORDER BY updated_at DESC", params
        ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["params_json"] = json.loads(d["params_json"] or "{}")
            d["evidence_json"] = json.loads(d["evidence_json"] or "{}")
            out.append(d)
        return out

    def update_model_params(
        self,
        model_key: str,
        *,
        params_json: dict | None = None,
        n_samples: int | None = None,
        confidence: str | None = None,
        evidence_json: dict | None = None,
        status: str | None = None,
        bump_verified: bool = True,
    ) -> bool:
        """Incremental update of a model. None args leave fields
        untouched. `bump_verified` updates last_verified_at — set to
        False for trivial edits (e.g., manual rename) that aren't a
        refit. Returns False if model_key doesn't exist."""
        existing = self.get_model(model_key)
        if not existing:
            return False
        sets, vals = [], []
        if params_json is not None:
            self._validate_params(existing["model_type"], params_json)
            sets.append("params_json = ?")
            vals.append(json.dumps(params_json, ensure_ascii=False))
        if n_samples is not None:
            sets.append("n_samples = ?")
            vals.append(n_samples)
        if confidence is not None:
            if confidence not in self._CONFIDENCE_LEVELS:
                raise ValueError(
                    f"confidence {confidence!r} not in {self._CONFIDENCE_LEVELS}"
                )
            sets.append("confidence = ?")
            vals.append(confidence)
        if evidence_json is not None:
            sets.append("evidence_json = ?")
            vals.append(json.dumps(evidence_json, ensure_ascii=False))
        if status is not None:
            if status not in self._MODEL_STATUSES:
                raise ValueError(f"status {status!r} not in {self._MODEL_STATUSES}")
            sets.append("status = ?")
            vals.append(status)
        if not sets:
            return True  # nothing to update is success
        now = self._now()
        sets.append("updated_at = ?")
        vals.append(now)
        if bump_verified:
            sets.append("last_verified_at = ?")
            vals.append(now)
        vals.append(model_key)
        self.conn.execute(
            f"UPDATE models SET {', '.join(sets)} WHERE model_key = ?", vals
        )
        self.conn.commit()
        return True

    def link_topic_to_model(self, topic_id: str, model_id: str) -> bool:
        """Append model_id to topics.related_models (JSON array).
        Idempotent: noop if already linked. Returns False if topic
        doesn't exist."""
        row = self.conn.execute(
            "SELECT related_models FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if not row:
            return False
        current = json.loads(row["related_models"] or "[]")
        if model_id in current:
            return True
        current.append(model_id)
        self.conn.execute(
            "UPDATE topics SET related_models = ?, updated_at = ? WHERE topic_id = ?",
            (json.dumps(current), self._now(), topic_id),
        )
        self.conn.commit()
        return True

    # ------------------------------------------------------------------
    # Model proposal pipeline (PR P2 — episode → model generalize)
    # ------------------------------------------------------------------
    #
    # When a topic has accumulated enough episodes, ask the LLM whether
    # the events are parametrically generalizable into a model. If so,
    # the proposal lands in topic_decisions for user confirmation
    # (chat-driven via MCP tools — no separate /memory page in MVP).
    #
    # Trigger is manual today (user clicks a button, or asks the agent
    # in chat to scan a topic). Cron-based auto-trigger is deferred to
    # a follow-up.

    PROPOSAL_VERSION = "v1"  # bump on _PROPOSAL_PROMPT edits; lands in trace

    _PROPOSAL_PROMPT = """你正在帮助提炼一个跑步教练系统的"模式库"。

下面是一个 topic 及其关联的 episodes（事件）。判断这些事件是否可以提炼成一个可参数化的模型（model），用于今后量化追踪。

**Topic**:
{topic_block}

**Episodes** ({n_episodes} 个):
{episodes_block}

**已有 models（避免重复）**:
{existing_models_block}

判断标准：
- 至少 3 个相关 episodes 才考虑提议
- 事件之间应有可量化的共性（数值、频率、形状）
- 如果已有 model 已经覆盖了这个 pattern，不要重复提议
- 不确定时返回 propose=false 比乱建好

可用 model_type:
  decay         — 衰减/恢复曲线，params 含 peak_drop_day / peak_drop_pct / return_to_baseline_day
  linear_trend  — 趋势/斜率，params 含 slope / intercept / unit
  mean_std      — 基线 + 离散，params 含 mean / sd / window 或 sample 描述
  ordinal_score — 按桶分数，params 含 scores: {{key: float}}
  rate          — 比率/概率，params 含 by_X: {{key: float}} 或 overall_rate
  fixed_obs     — 一次性快照观察，params 自由 shape

返回 JSON（仅这两种形式之一，不要多余文字）：

{{
  "propose": true,
  "model_key": "category.short_descriptive_key",
  "name": "中文标签",
  "category": "Health/Recovery" | "Running/Performance" | ...,
  "model_type": "decay" | "linear_trend" | ...,
  "params": {{ ... 与 model_type 匹配的字段 ... }},
  "n_samples": <实际支撑参数的事件数>,
  "confidence": "low" | "medium" | "high",
  "rationale": "1-2 句解释为什么这些 episodes 适合参数化"
}}

或：

{{
  "propose": false,
  "reason": "为什么这些 episodes 还不能 / 不适合参数化"
}}
"""

    MIN_EPISODES_TO_PROPOSE = 3

    @staticmethod
    def _strip_llm_json(text: str) -> str:
        """Strip the common LLM JSON-wrapping artifacts before
        json.loads: leading/trailing whitespace, ```json fences, or
        leading prose with a JSON block embedded. If the cleaned
        string still doesn't parse, the caller surfaces llm_error and
        returns the raw text for debugging."""
        s = text.strip()
        # Markdown code fence: ```json\n...\n``` or ```\n...\n```
        if s.startswith("```"):
            first_nl = s.find("\n")
            if first_nl > 0:
                s = s[first_nl + 1:]
            if s.rstrip().endswith("```"):
                s = s.rsplit("```", 1)[0]
            s = s.strip()
        # If there's prose before/after a clean JSON object, slice
        # from the first '{' to the last '}'. Conservative — works
        # for single top-level object responses (which our prompt
        # asks for) but not arrays.
        if not s.startswith("{") and "{" in s:
            first = s.find("{")
            last = s.rfind("}")
            if first >= 0 and last > first:
                s = s[first:last + 1]
        return s

    def propose_model_from_topic(
        self, topic_id: str, *, trigger: str = "manual"
    ) -> dict:
        """
        Ask the LLM whether the topic's related episodes can be
        parameterized into a model. Either parks a 'new_model'
        decision (user confirms in chat / via API) or returns
        a no-op result with a reason.

        `trigger`: free-form label ('manual' / 'cron' / 'post_consolidate')
        recorded in the trace + decision proposal so we can grep by
        source. Defaults to 'manual'.

        Returns a dict shaped:
          {"status": "parked",       "decision_id": "dec_...", "proposal": {...}}
          {"status": "skipped",      "reason": "..."}
          {"status": "llm_error",    "raw": "...", "reason": "..."}

        Skipped cases:
          - topic doesn't exist → status='skipped', reason='topic_missing'
          - < MIN_EPISODES_TO_PROPOSE episodes → status='skipped', reason='too_few_episodes'
          - LLM says propose=false → status='skipped', reason=<LLM's reason>
        """
        topic = self.get_topic(topic_id)
        if not topic:
            return {"status": "skipped", "reason": "topic_missing"}

        episodes = self.get_topic_episodes(topic_id, limit=50)
        if len(episodes) < self.MIN_EPISODES_TO_PROPOSE:
            return {
                "status": "skipped",
                "reason": "too_few_episodes",
                "n_episodes": len(episodes),
                "min_required": self.MIN_EPISODES_TO_PROPOSE,
            }

        # Existing models linked to this topic — included in the prompt
        # so the LLM doesn't propose duplicates. Lookup by model_id.
        existing_ids = topic.get("related_models", []) or []
        existing_models = []
        for mid in existing_ids:
            row = self.conn.execute(
                "SELECT model_key, name, model_type, params_json "
                "FROM models WHERE model_id = ?", (mid,)
            ).fetchone()
            if row:
                existing_models.append({
                    "model_key": row["model_key"],
                    "name": row["name"],
                    "model_type": row["model_type"],
                    "params": json.loads(row["params_json"] or "{}"),
                })

        # Build the prompt block-by-block — keeps payload predictable
        # and easier to grep in the trace row.
        topic_block = json.dumps(
            {
                "name": topic["name"],
                "category": topic.get("root_category"),
                "status": topic["status"],
                "working_conclusion": topic.get("working_conclusion"),
            },
            ensure_ascii=False,
            indent=2,
        )
        episodes_block = json.dumps(
            [
                {
                    "event_type": e["event_type"],
                    "timestamp": e.get("timestamp"),
                    "context": e.get("context"),  # 5W1H+E
                    "lesson_learned": e.get("lesson_learned"),
                }
                for e in episodes
            ],
            ensure_ascii=False,
            indent=2,
        )
        existing_models_block = (
            json.dumps(existing_models, ensure_ascii=False, indent=2)
            if existing_models else "(none)"
        )

        prompt = self._PROPOSAL_PROMPT.format(
            topic_block=topic_block,
            n_episodes=len(episodes),
            episodes_block=episodes_block,
            existing_models_block=existing_models_block,
        )

        # Trace the whole proposal call so we can grep
        # `kind='model_propose'` later for "why didn't the LLM propose
        # X?" debugging.
        with self.tracer.turn(
            kind="model_propose",
            thread_id=f"topic:{topic_id}",
            prompt_version=self.PROPOSAL_VERSION,
            prompt_hash="cme_propose_v1",
            user_input=topic["name"],
        ) as trace:
            trace.extras["trigger"] = trigger
            trace.extras["n_episodes"] = len(episodes)
            trace.extras["existing_models"] = [m["model_key"] for m in existing_models]

            raw = self._llm_invoke(prompt)
            trace.final_answer = raw[:500]

            # LLMs occasionally wrap JSON in markdown code fences
            # (```json ... ```) or include leading prose. Be lenient:
            # strip a leading fence + trailing fence, and if that still
            # doesn't parse, try to locate the first { ... } block.
            cleaned = self._strip_llm_json(raw)
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError as e:
                trace.extras["parse_error"] = str(e)
                return {
                    "status": "llm_error",
                    "raw": raw[:500],
                    "reason": f"unparseable JSON: {e}",
                }

            if not parsed.get("propose"):
                return {
                    "status": "skipped",
                    "reason": parsed.get("reason") or "llm_declined",
                }

            # LLM proposes — validate the required fields are there
            # before parking. Don't validate model_type strictly here
            # (let park surface as-is so user can see the LLM's full
            # output even if it's malformed); resolve will validate
            # before insert.
            for required in ("model_key", "name", "model_type", "params"):
                if required not in parsed:
                    return {
                        "status": "llm_error",
                        "raw": raw[:500],
                        "reason": f"missing required field: {required}",
                    }

            # Wrap the LLM proposal with the topic_id so resolve knows
            # what to link the resulting model to.
            proposal = {
                "topic_id": topic_id,
                "trigger": trigger,
                **parsed,  # propose, model_key, name, category, model_type, params, n_samples, confidence, rationale
            }
            decision_id = self.park_topic_decision(
                kind="new_model", proposal=proposal, candidates=[]
            )
            trace.extras["decision_id"] = decision_id
            return {
                "status": "parked",
                "decision_id": decision_id,
                "proposal": proposal,
            }

    # ------------------------------------------------------------------
    # Pending Clarifications CRUD
    # ------------------------------------------------------------------
    def create_pending(
        self,
        trigger_type: str,
        question_for_user: str,
        resolution_callback: dict,
    ) -> str:
        # Dedup: if an unresolved pending with identical question text already
        # exists, return it instead of inserting a duplicate. Consolidation can
        # re-detect the same conflict across turns; without this the UI ends up
        # showing the same question multiple times with different ask_ IDs.
        normalized = (question_for_user or "").strip()
        existing = self.conn.execute(
            """SELECT pending_id FROM pending_clarifications
               WHERE is_resolved = 0 AND TRIM(question_for_user) = ?
               LIMIT 1""",
            (normalized,),
        ).fetchone()
        if existing:
            return existing["pending_id"]

        pending_id = f"ask_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """INSERT INTO pending_clarifications
               (pending_id, trigger_type, question_for_user,
                resolution_callback, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                pending_id,
                trigger_type,
                question_for_user,
                json.dumps(resolution_callback, ensure_ascii=False),
                self._now(),
            ),
        )
        self.conn.commit()
        return pending_id

    def list_pending(self, resolved: bool = False) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM pending_clarifications WHERE is_resolved = ? ORDER BY created_at DESC",
            (1 if resolved else 0,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["resolution_callback"] = json.loads(d["resolution_callback"])
            result.append(d)
        return result

    # ==================================================================
    # API 1: retrieve_working_context
    # ==================================================================
    def retrieve_working_context(
        self, user_query: str, current_metrics: dict | None = None
    ) -> str:
        """
        Assemble high-density context for the agent's system prompt.

        1. Extract intent / keywords from user_query
        2. Fetch active Topics + related Episodes + Semantic Profile
        3. Prepend any unresolved Pending Clarifications
        """
        # --- 1. Simple keyword extraction from user query ---
        keywords = [
            w
            for w in user_query.replace("，", " ").replace("。", " ").split()
            if len(w) >= 2
        ]

        # --- 2. Gather active topics + recently resolved as reference knowledge ---
        active_topics = self.list_topics(status="Open") + self.list_topics(
            status="Testing"
        )
        resolved_topics = self.list_topics(status="Resolved")
        conflicting_topics = self.list_topics(status="Conflicting")

        # --- 3. Search related episodes ---
        related_episodes = self.search_episodes(keywords, limit=5) if keywords else []

        # If no keyword matches, fall back to recent episodes
        if not related_episodes:
            related_episodes = self.list_episodes(limit=5)

        # --- 4. Semantic profile ---
        profile = self._load_semantic_profile()

        # --- 5. Pending clarifications ---
        pending = self.list_pending(resolved=False)

        # --- 6. Assemble context ---
        sections: list[str] = []

        # Conflicting topics take highest priority — user stated something
        # contradicting the archive, we MUST clarify before trusting either side.
        if conflicting_topics:
            conflict_lines = [
                "⚠️ **冲突待澄清 (用户最新说法与档案矛盾，请优先提问以下问题):**"
            ]
            for t in conflicting_topics:
                q = t.get("open_question") or "（无 open_question，请追问用户）"
                conflict_lines.append(f"  - [{t['topic_id']}] {t['name']} → {q}")
            sections.append("\n".join(conflict_lines))

        # Legacy pending_clarifications path — kept for backward compat.
        # After the v2 migration this table has 0 unresolved rows, but old
        # consolidation code may still write here until Phase 2b lands.
        if pending:
            pq_lines = ["⚠️ **待确认事项 (legacy pending_clarifications):**"]
            for p in pending:
                pq_lines.append(
                    f"  - [{p['pending_id']}] ({p['trigger_type']}) {p['question_for_user']}"
                )
            sections.append("\n".join(pq_lines))

        # Active topics — frame as questions the AI must ask
        if active_topics:
            topic_lines = ["📌 **活跃话题 — 你必须向用户逐一询问以下话题的最新进展:**"]
            for t in active_topics[:10]:
                if t["status"] == "Testing":
                    topic_lines.append(
                        f"  - [验证中] {t['name']} — 之前的结论: {t['working_conclusion'] or '无'}。请问用户验证结果如何？"
                    )
                else:
                    topic_lines.append(
                        f"  - [待探索] {t['name']}（尚无结论）。请询问用户目前情况如何？"
                    )
            sections.append("\n".join(topic_lines))

        # Resolved topics — settled knowledge the AI should reference
        if resolved_topics:
            resolved_lines = ["✅ **已解决的话题 (settled knowledge — 请在相关讨论中引用这些结论):**"]
            for t in resolved_topics[:10]:
                resolved_lines.append(
                    f"  - {t['name']} — 最终结论: {t['working_conclusion']}"
                )
            sections.append("\n".join(resolved_lines))

        # Related episodes / lessons
        if related_episodes:
            ep_lines = ["📖 **相关历史经验 (Episodic Memory):**"]
            for ep in related_episodes[:5]:
                ctx = ep.get("context", {})
                what = ctx.get("what", ep.get("event_type", ""))
                lesson = ep.get("lesson_learned", "")
                ts = ep.get("timestamp", "")[:10]
                line = f"  - [{ts}] {what}"
                if lesson:
                    line += f" → 教训: {lesson}"
                ep_lines.append(line)
            sections.append("\n".join(ep_lines))

        # Semantic profile snippet
        if profile:
            profile_lines = ["👤 **用户档案 (Semantic Identity):**"]
            if profile.get("medical_notes"):
                profile_lines.append(
                    f"  - 医疗备注: {'; '.join(profile['medical_notes'])}"
                )
            if profile.get("preferences"):
                profile_lines.append(
                    f"  - 偏好: {'; '.join(profile['preferences'])}"
                )
            baseline = profile.get("physiological_baseline", {})
            if baseline:
                profile_lines.append(f"  - 生理基线: {json.dumps(baseline)}")
            sections.append("\n".join(profile_lines))

        # Current metrics
        if current_metrics:
            sections.append(
                f"📊 **今日指标:** {json.dumps(current_metrics, ensure_ascii=False)}"
            )

        if not sections:
            return "（记忆引擎暂无相关上下文）"

        return "\n\n".join(sections)

    # ==================================================================
    # API 2: resolve_pending_question
    # ==================================================================
    def resolve_pending_question(self, pending_id: str, user_answer: str) -> bool:
        """
        Mark a pending clarification as resolved and execute its callback.
        """
        row = self.conn.execute(
            "SELECT * FROM pending_clarifications WHERE pending_id = ? AND is_resolved = 0",
            (pending_id,),
        ).fetchone()
        if not row:
            return False

        callback = json.loads(row["resolution_callback"])
        action = callback.get("action")

        # Execute resolution actions
        if action == "merge_nodes":
            # Entity merge — record the answer as a resolved episode
            self.create_episode(
                event_type="Entity_Resolution",
                context={
                    "what": f"已确认: {row['question_for_user']}",
                    "user_answer": user_answer,
                    "target_node": callback.get("target_node"),
                },
                lesson_learned=f"用户确认: {user_answer}",
            )
        elif action == "refine_preference_rule":
            # Preference update — create an episode recording the change
            conflict = callback.get("conflict_context", {})
            self.create_episode(
                event_type="Preference_Update",
                context={
                    "what": f"偏好修正: {row['question_for_user']}",
                    "old_belief": conflict.get("old_belief"),
                    "new_evidence": conflict.get("new_evidence"),
                    "user_answer": user_answer,
                },
                lesson_learned=f"偏好更新: {user_answer}",
            )
        elif action == "update_topic":
            target = callback.get("target_topic_id")
            if target:
                new_status = callback.get("new_status")
                updates: dict[str, Any] = {}
                if new_status:
                    updates["status"] = new_status
                conclusion = callback.get("conclusion_from_answer")
                if conclusion:
                    updates["working_conclusion"] = conclusion
                if updates:
                    self.update_topic(target, **updates)

        # Mark as resolved
        self.conn.execute(
            """UPDATE pending_clarifications
               SET is_resolved = 1, resolution_answer = ?, resolved_at = ?
               WHERE pending_id = ?""",
            (user_answer, self._now(), pending_id),
        )
        self.conn.commit()
        return True

    # ==================================================================
    # Topic matching (embedding-based)
    # ==================================================================
    # A "topic" is uniquely identified by its semantic subject, not its row id.
    # When LLM consolidation proposes a new topic or a new conflict, we
    # embedding-match it against all existing topics. If similarity with the
    # best candidate clears MATCH_THRESHOLD we treat it as the SAME topic and
    # update in place; otherwise we create a new one. This prevents the
    # "N rows for one real-world conflict" failure we saw with rainy-day running.

    MATCH_THRESHOLD: float = 0.80  # cosine sim above this → auto-merge
    TOPIC_EMBED_PROVIDER: str = "gemini"

    # record_coach_fact uses two thresholds (vs the single MATCH_THRESHOLD)
    # so an ambiguous match doesn't silently merge OR silently fork:
    #   score ≥ HIGH         → same fact, update the existing topic in place
    #   LOW ≤ score < HIGH   → ambiguous, park a decision ("update X or new?")
    #   score < LOW          → distinct fact, create a new topic in the area
    COACH_FACT_HIGH_THRESHOLD: float = 0.80
    COACH_FACT_LOW_THRESHOLD: float = 0.60

    @staticmethod
    def _topic_signature_text(topic: dict) -> str:
        """The text that represents a topic in embedding space."""
        parts = [
            topic.get("name", ""),
            topic.get("working_conclusion") or "",
            topic.get("open_question") or "",
        ]
        return " :: ".join(p.strip() for p in parts if p and p.strip())

    def _invalidate_topic_cache(self, topic_id: str) -> None:
        """Drop every cached embedding for `topic_id` regardless of which
        provider produced it. Called whenever a topic's signature text
        changes (update / merge / delete) so the next match recomputes."""
        keys = [k for k in self._topic_embeddings if k[1] == topic_id]
        for k in keys:
            self._topic_embeddings.pop(k, None)

    def _embed_topic(self, topic: dict) -> list[float]:
        """Return the cached embedding for a topic, recomputing if stale."""
        import hashlib

        sig_text = self._topic_signature_text(topic)
        sig = hashlib.sha1(sig_text.encode("utf-8")).hexdigest()
        tid = topic["topic_id"]
        cache_key = (self.TOPIC_EMBED_PROVIDER, tid)

        cached = self._topic_embeddings.get(cache_key)
        if cached and cached[0] == sig:
            return cached[1]

        vec = call_embedding([sig_text], provider=self.TOPIC_EMBED_PROVIDER)[0]
        self._topic_embeddings[cache_key] = (sig, vec)
        return vec

    def find_matching_topic(
        self,
        query_text: str,
        top_k: int = 5,
        threshold: float | None = None,
        root_category: str | None = None,
        raise_on_embed_error: bool = False,
    ) -> tuple[str | None, list[dict]]:
        """
        Find the topic most semantically similar to `query_text`.

        Args:
            query_text: natural-language text describing the new proposal.
            top_k: candidates to return when no auto-match is confident enough.
            threshold: cosine similarity above which we auto-merge. None uses
                       MATCH_THRESHOLD.
            root_category: when set, only topics in this category are scored.
                       record_coach_fact uses this to keep matching scoped to
                       one intake area (e.g. a new injury fact can only merge
                       into an existing injury_history topic, never into goal).

        Returns:
            (auto_match_topic_id_or_None, ranked_candidates_with_scores)

            Candidates list: [{topic_id, name, status, score}, ...] sorted by
            score desc, length min(top_k, N). Always returned even when an
            auto-match is found, so UI can show runners-up.
        """
        if threshold is None:
            threshold = self.MATCH_THRESHOLD

        topics = self.list_topics()
        if root_category is not None:
            topics = [t for t in topics if t["root_category"] == root_category]
        if not topics or not (query_text and query_text.strip()):
            return (None, [])

        try:
            query_vec = call_embedding([query_text], provider=self.TOPIC_EMBED_PROVIDER)[0]
        except Exception as e:
            # Embedding failure. For batch consolidation (default) we swallow
            # it as "no match" and the caller falls back to create-new — it
            # dedupes later, so a transient miss is harmless. record_coach_fact
            # is eager with no such backstop, so it passes raise_on_embed_error
            # to PARK instead of forking a duplicate that would shadow the real
            # topic in coverage (most-recent-wins).
            print(f"[CME] topic match embedding failed: {e}")
            if raise_on_embed_error:
                raise
            return (None, [])

        scored: list[dict] = []
        for t in topics:
            sig_text = self._topic_signature_text(t)
            if not sig_text:
                continue
            try:
                t_vec = self._embed_topic(t)
            except Exception as e:
                print(f"[CME] skip topic {t['topic_id']} — embed failed: {e}")
                continue
            score = cosine_similarity(query_vec, t_vec)
            scored.append(
                {
                    "topic_id": t["topic_id"],
                    "name": t["name"],
                    "status": t["status"],
                    "score": score,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)
        candidates = scored[:top_k]
        best = scored[0] if scored else None
        auto = best["topic_id"] if best and best["score"] >= threshold else None
        return (auto, candidates)

    # ------------------------------------------------------------------
    # Topic decision queue (for low-confidence matches — user confirms)
    # ------------------------------------------------------------------
    def park_topic_decision(
        self,
        kind: str,
        proposal: dict,
        candidates: list[dict],
    ) -> str:
        """
        Queue a consolidation proposal that the matcher couldn't auto-resolve.
        kind: 'new_topic' | 'conflict' | 'episode_linking' | 'new_model'.
        UI / chat surfaces pending rows for user action.

        PR P2 added 'new_model' for the pattern-store proposal pipeline:
        when the LLM concludes that a topic's accumulated episodes are
        parametrically generalizable, it proposes a model_key + params,
        and the agent (or any explicit /memory route in the future)
        asks the user to confirm.
        """
        if kind not in ("new_topic", "conflict", "episode_linking", "new_model"):
            raise ValueError(f"Unknown decision kind: {kind}")
        decision_id = f"dec_{uuid.uuid4().hex[:8]}"
        self.conn.execute(
            """INSERT INTO topic_decisions
               (decision_id, kind, proposal_json, candidates_json, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (
                decision_id,
                kind,
                json.dumps(proposal, ensure_ascii=False),
                json.dumps(candidates, ensure_ascii=False),
                self._now(),
            ),
        )
        self.conn.commit()
        return decision_id

    def list_pending_decisions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM topic_decisions WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["proposal"] = json.loads(d.pop("proposal_json") or "{}")
            d["candidates"] = json.loads(d.pop("candidates_json") or "[]")
            out.append(d)
        return out

    def resolve_topic_decision(
        self,
        decision_id: str,
        action: str,
        target_topic_id: str | None = None,
        target_topic_ids: list[str] | None = None,
    ) -> str | list[str] | None:
        """
        Apply the user's verdict on a parked decision.

        action:
          - 'merge'      → fold proposal into `target_topic_id` (required).
                           For new_topic: updates target's conclusion.
                           For conflict: promote_topic_to_conflicting on target.
          - 'create_new' → create the topic the LLM originally proposed.
          - 'link'       → (episode_linking only) link the parked episode to
                           every topic in `target_topic_ids`. Empty list means
                           "keep episode unlinked" (still closes the decision).
          - 'reject'     → discard the proposal entirely.

        Returns:
          - topic_id str for 'merge' / 'create_new'
          - list[str] of linked topic_ids for 'link'
          - None for 'reject'
          - ""  on failure (decision missing or already resolved).
        """
        row = self.conn.execute(
            "SELECT * FROM topic_decisions WHERE decision_id = ? AND status = 'pending'",
            (decision_id,),
        ).fetchone()
        if not row:
            return ""

        proposal = json.loads(row["proposal_json"] or "{}")
        kind = row["kind"]
        result_tid: str | None = None
        resolution: str

        if action == "merge":
            if not target_topic_id:
                raise ValueError("action='merge' requires target_topic_id")
            if kind == "new_topic":
                updates: dict[str, Any] = {}
                if proposal.get("working_conclusion"):
                    updates["working_conclusion"] = proposal["working_conclusion"]
                if proposal.get("status") in ("Open", "Testing"):
                    existing = self.get_topic(target_topic_id) or {}
                    if existing.get("status") not in ("Resolved", "Conflicting"):
                        updates["status"] = proposal["status"]
                if updates:
                    self.update_topic(target_topic_id, **updates)
                    self._invalidate_topic_cache(target_topic_id)
                # record_coach_fact parks decisions carrying the lossless
                # episode it already created; on merge, link that episode to
                # the chosen topic so the raw text is reachable via
                # get_topic_episodes. Guarded: consolidation-parked proposals
                # have no episode_id, so this is a no-op for them.
                if proposal.get("episode_id"):
                    self.add_topic_episode_link(
                        target_topic_id, proposal["episode_id"]
                    )
            elif kind == "conflict":
                self.promote_topic_to_conflicting(
                    target_topic_id,
                    open_question=proposal.get("question_for_user", ""),
                    conflict_context={
                        "old_belief": proposal.get("old_belief"),
                        "new_evidence": proposal.get("new_evidence"),
                    },
                )
            else:
                # Codex P2 catch on #78: the old `else: # conflict`
                # branch silently caught new_model + episode_linking,
                # corrupting the target topic (promote_topic_to_conflicting
                # with empty question / context) and resolving the
                # decision without creating any of the things it should.
                # Force the caller to pick a kind-appropriate action.
                raise ValueError(
                    f"action='merge' is not supported for kind={kind!r}. "
                    f"new_model: use 'create_new' or 'reject'. "
                    f"episode_linking: use 'link' or 'reject'."
                )
            result_tid = target_topic_id
            resolution = f"merged:{target_topic_id}"

        elif action == "create_new":
            if kind == "new_topic":
                result_tid = self.create_topic(
                    name=proposal.get("name", "Untitled"),
                    root_category=proposal.get("root_category", "General"),
                    status=proposal.get("status", "Open"),
                    working_conclusion=proposal.get("working_conclusion"),
                )
                # record_coach_fact parks the lossless episode it created; on
                # create_new, link it to the freshly-made topic. Guarded:
                # consolidation-parked proposals carry no episode_id (no-op).
                if proposal.get("episode_id"):
                    self.add_topic_episode_link(result_tid, proposal["episode_id"])
            elif kind == "new_model":
                # PR P2: confirm the LLM-proposed model — create the
                # model row and link it back to the source topic. The
                # proposal carries every field create_model needs
                # (validated by propose_model_from_topic), plus
                # topic_id added at park time.
                source_topic_id = proposal.get("topic_id")
                if not source_topic_id:
                    raise ValueError(
                        "new_model proposal missing topic_id "
                        "(propose_model_from_topic should have set this)"
                    )
                model_id = self.create_model(
                    model_key=proposal["model_key"],
                    name=proposal["name"],
                    category=proposal.get("category", "General"),
                    model_type=proposal["model_type"],
                    params_json=proposal.get("params") or {},
                    derivation_method="llm",  # came from LLM, not stat
                    n_samples=int(proposal.get("n_samples") or 0),
                    confidence=proposal.get("confidence"),
                    evidence_json={
                        "episode_count_at_proposal": int(
                            proposal.get("n_samples") or 0
                        ),
                        "proposal_rationale": proposal.get("rationale"),
                        "trigger": proposal.get("trigger", "manual"),
                    },
                    status="Forming",  # llm-derived starts Forming until stat refit
                )
                self.link_topic_to_model(source_topic_id, model_id)
                # Override the resolution string format below — models
                # use mdl_ prefix so the "created:tpc_xxx" pattern
                # doesn't fit; we set it inline and skip the default.
                self.conn.execute(
                    """UPDATE topic_decisions
                       SET status = 'created', resolution = ?, resolved_at = ?
                       WHERE decision_id = ?""",
                    (f"created:{model_id}", self._now(), decision_id),
                )
                self.conn.commit()
                return model_id  # early return: skip the trailing UPDATE
            elif kind == "conflict":
                # Conflict → create Conflicting topic from scratch.
                result_tid = f"tpc_{uuid.uuid4().hex[:8]}"
                now = self._now()
                name_seed = (
                    proposal.get("subject_summary") or proposal.get("question_for_user") or "Conflict"
                )[:40]
                self.conn.execute(
                    """INSERT INTO topics
                       (topic_id, root_category, name, status, working_conclusion,
                        open_question, conflict_context, related_episodes,
                        created_at, updated_at)
                       VALUES (?, ?, ?, 'Conflicting', NULL, ?, ?, '[]', ?, ?)""",
                    (
                        result_tid,
                        "Conflict",
                        name_seed,
                        proposal.get("question_for_user", ""),
                        json.dumps(
                            {
                                "old_belief": proposal.get("old_belief"),
                                "new_evidence": proposal.get("new_evidence"),
                            },
                            ensure_ascii=False,
                        ),
                        now,
                        now,
                    ),
                )
                self.conn.commit()
            else:
                # Codex P2 catch on #78 (mirror of the merge branch
                # fix): the prior `else: # conflict` swallowed
                # episode_linking too, which would create a bogus
                # Conflicting topic from a parked link decision.
                # Force kind-appropriate action.
                raise ValueError(
                    f"action='create_new' is not supported for kind={kind!r}. "
                    f"episode_linking: use 'link' or 'reject'."
                )
            resolution = f"created:{result_tid}"

        elif action == "link":
            if kind != "episode_linking":
                raise ValueError("action='link' only valid for kind='episode_linking'")
            episode_id = proposal.get("episode_id")
            if not episode_id:
                raise ValueError("episode_linking proposal missing episode_id")
            ids = target_topic_ids or []
            for tid in ids:
                self.add_topic_episode_link(tid, episode_id)
            result_tid = ids  # type: ignore[assignment]
            resolution = f"linked:{','.join(ids)}" if ids else "linked:"

        elif action == "reject":
            result_tid = None
            resolution = "rejected"

        else:
            raise ValueError(f"Unknown action: {action}")

        status_map = {
            "merge": "merged",
            "create_new": "created",
            "link": "linked",
            "reject": "rejected",
        }
        self.conn.execute(
            """UPDATE topic_decisions
               SET status = ?, resolution = ?, resolved_at = ?
               WHERE decision_id = ?""",
            (status_map[action], resolution, self._now(), decision_id),
        )
        self.conn.commit()
        return result_tid

    # ==================================================================
    # Coach intake — athlete profile (A) + cycle config (B) capture
    # (PROJECT_GUIDE §3.4.5). The continuous (C) stream lives in the
    # data_processor / models layer, not here.
    # ==================================================================
    def record_coach_fact(
        self,
        area: str,
        raw_text: str,
        conclusion: str | None = None,
        name: str | None = None,
    ) -> dict:
        """
        Eagerly capture one athlete-profile / cycle-config fact. Two CME
        columns carry it: episodes.context_json holds the LOSSLESS raw text;
        topics.working_conclusion holds the distilled answer for `area`, on a
        topic whose root_category == area.

        Flow:
          1. Create a lossless episode (event_type 'profile'|'cycle_config',
             context = {area, raw_text}).
          2. Embedding-match `raw_text` against existing topics IN THIS area:
               score ≥ HIGH        → update that topic's conclusion + link ep.
               LOW ≤ score < HIGH  → park a 'new_topic' decision (update X /
                                     new fact?) — never silently merge.
               score < LOW / none  → create a new topic in the area + link ep.

        Args:
            area: qualified coach-intake area (see coach_intake.ALL_AREAS).
            raw_text: what the user said, stored verbatim. Required.
            conclusion: distilled answer to store on the topic. The agent
                (PR-2) passes a clean one-liner; defaults to raw_text.
            name: fine-grained embeddable topic name (LLM-generated in PR-2);
                defaults to "<label>: <raw_text snippet>".

        Returns:
            {"action": "updated"|"created"|"parked", "episode_id", "score",
             plus "topic_id" (updated/created) or
             "decision_id"+"candidates" (parked)}.

        Raises:
            ValueError on an unknown area or empty raw_text.
        """
        if area not in ALL_AREAS:
            raise ValueError(f"Unknown coach-intake area: {area!r}")
        raw_text = (raw_text or "").strip()
        if not raw_text:
            raise ValueError("record_coach_fact requires non-empty raw_text")

        conclusion = (conclusion or raw_text).strip()
        slot = SLOT_BY_AREA[area]
        name = (name or "").strip() or f"{slot.label}: {raw_text[:40]}"

        # 1. lossless episode
        episode_id = self.create_episode(
            event_type=event_type_for_area(area),
            context={"area": area, "raw_text": raw_text},
        )

        # 2. area-scoped embedding match (two-threshold decision). raise on an
        #    embedding failure so we can PARK rather than fork — see below.
        embed_failed = False
        try:
            auto, candidates = self.find_matching_topic(
                raw_text,
                threshold=self.COACH_FACT_HIGH_THRESHOLD,
                root_category=area,
                raise_on_embed_error=True,
            )
        except Exception:
            auto, candidates, embed_failed = None, [], True
        best_score = candidates[0]["score"] if candidates else 0.0

        if auto:  # score ≥ HIGH → same fact, update in place
            self.update_topic(auto, working_conclusion=conclusion)
            self._invalidate_topic_cache(auto)
            self.add_topic_episode_link(auto, episode_id)
            return {
                "action": "updated",
                "topic_id": auto,
                "episode_id": episode_id,
                "score": best_score,
                "embed_error": False,
            }

        # Decide park-vs-create. Two reasons to park (let the user
        # disambiguate) rather than create a fresh topic:
        #   (a) ambiguous score (LOW ≤ best < HIGH), or
        #   (b) the embedding call FAILED and the area already has topics we
        #       couldn't compare against — creating now would fork a duplicate
        #       that shadows the real topic in coverage (most-recent-wins).
        # When embedding failed we have no scored candidates, so surface the
        # area's existing topics (no score) for the user to pick from.
        park = best_score >= self.COACH_FACT_LOW_THRESHOLD
        if embed_failed:
            candidates = [
                {"topic_id": t["topic_id"], "name": t["name"],
                 "status": t["status"], "score": None}
                for t in self.list_topics()
                if t["root_category"] == area
            ]
            park = bool(candidates)  # only park if there's something to match

        if park:
            decision_id = self.park_topic_decision(
                kind="new_topic",
                proposal={
                    "name": name,
                    "root_category": area,
                    "status": "Resolved",
                    "working_conclusion": conclusion,
                    "area": area,
                    "raw_text": raw_text,
                    "episode_id": episode_id,
                    "embed_error": embed_failed,
                },
                candidates=candidates,
            )
            return {
                "action": "parked",
                "decision_id": decision_id,
                "episode_id": episode_id,
                "candidates": candidates,
                "score": best_score,
                "embed_error": embed_failed,
            }

        # score < LOW (or area empty even on embed failure) → distinct fact,
        # new topic
        topic_id = self.create_topic(
            name=name,
            root_category=area,
            status="Resolved",
            working_conclusion=conclusion,
        )
        self.add_topic_episode_link(topic_id, episode_id)
        return {
            "action": "created",
            "topic_id": topic_id,
            "episode_id": episode_id,
            "score": best_score,
            "embed_error": embed_failed,
        }

    def _pending_coach_facts_by_area(self) -> dict[str, int]:
        """Count unresolved record_coach_fact decisions per area. A parked
        (ambiguous) fact creates no topic, so coverage alone can't tell
        'never asked' from 'asked, answered, parked'. PR-2 reads this to avoid
        re-asking a question the user already answered — nudge them to resolve
        the pending decision instead."""
        rows = self.conn.execute(
            """SELECT json_extract(proposal_json, '$.area') AS area,
                      COUNT(*) AS n
               FROM topic_decisions
               WHERE status = 'pending' AND kind = 'new_topic'
                 AND json_extract(proposal_json, '$.area') IS NOT NULL
               GROUP BY area"""
        ).fetchall()
        return {r["area"]: r["n"] for r in rows}

    def _coverage_for_slots(self, slots: tuple[CoachSlot, ...]) -> dict:
        """Hard coverage over an ordered slot list. Pure SQL, no embeddings.
        An area is 'filled' iff it has ≥1 topic with a non-blank
        working_conclusion — a hard judgment by root_category, never by
        similarity. `pending_count` per area = parked-but-unresolved coach
        facts (a gap with pending_count>0 means the user answered but the
        match was ambiguous → resolve, don't re-ask). Returns numeric counts +
        pre-formatted labels/questions side by side so the same payload serves
        the UI and the agent prompt."""
        pending_by_area = self._pending_coach_facts_by_area()
        areas_out: list[dict] = []
        gaps: list[dict] = []
        filled_count = 0
        pending_count = 0
        for slot in slots:
            rows = self.conn.execute(
                """SELECT topic_id, working_conclusion, updated_at
                   FROM topics WHERE root_category = ?
                   ORDER BY updated_at DESC""",
                (slot.area,),
            ).fetchall()
            filled_rows = [
                r for r in rows if (r["working_conclusion"] or "").strip()
            ]
            filled = bool(filled_rows)
            n_pending = pending_by_area.get(slot.area, 0)
            pending_count += n_pending
            areas_out.append(
                {
                    "area": slot.area,
                    "label": slot.label,
                    "question": slot.question,
                    "filled": filled,
                    "conclusion": filled_rows[0]["working_conclusion"]
                    if filled
                    else None,
                    "updated_at": filled_rows[0]["updated_at"] if filled else None,
                    "topic_ids": [r["topic_id"] for r in filled_rows],
                    "pending_count": n_pending,
                }
            )
            if filled:
                filled_count += 1
            else:
                gaps.append(
                    {
                        "area": slot.area,
                        "label": slot.label,
                        "question": slot.question,
                        "pending_count": n_pending,
                    }
                )
        return {
            "areas": areas_out,
            "gaps": gaps,
            "filled_count": filled_count,
            "pending_count": pending_count,
            "total": len(slots),
        }

    def get_coach_profile(self) -> dict:
        """Static athlete profile (A) coverage — PROJECT_GUIDE §3.4.5.
        {areas:[{area,label,question,filled,conclusion,updated_at,topic_ids,
        pending_count}], gaps:[{area,label,question,pending_count}],
        filled_count, pending_count, total}. A gap with pending_count>0 was
        answered but parked (ambiguous) — resolve, don't re-ask."""
        return self._coverage_for_slots(PROFILE_SLOTS)

    def get_cycle_config(self) -> dict:
        """Per-cycle config (B) coverage — same shape as get_coach_profile."""
        return self._coverage_for_slots(CYCLE_SLOTS)

    def promote_topic_to_conflicting(
        self,
        topic_id: str,
        open_question: str,
        conflict_context: dict,
    ) -> bool:
        """
        Flip an existing topic to status='Conflicting' with a user-facing
        clarifying question. Merges prior conflict_context if present.
        """
        row = self.conn.execute(
            "SELECT conflict_context FROM topics WHERE topic_id = ?", (topic_id,)
        ).fetchone()
        if not row:
            return False

        # Merge: preserve prior merged_from_pending_ids / target_node across re-detections
        prior: dict = {}
        try:
            prior = json.loads(row["conflict_context"] or "{}")
        except (TypeError, json.JSONDecodeError):
            prior = {}
        merged_ctx = {**prior, **conflict_context}

        self.conn.execute(
            """UPDATE topics
               SET status = 'Conflicting',
                   open_question = ?,
                   conflict_context = ?,
                   updated_at = ?
               WHERE topic_id = ?""",
            (
                open_question,
                json.dumps(merged_ctx, ensure_ascii=False),
                self._now(),
                topic_id,
            ),
        )
        self.conn.commit()
        # Invalidate cached embedding — signature changed
        self._invalidate_topic_cache(topic_id)
        return True

    # ==================================================================
    # API 3: consolidate_memory_background
    # ==================================================================
    def consolidate_memory_background(
        self, thread_id: str, chat_history: list[dict]
    ) -> None:
        """
        Post-conversation memory consolidation.

        v2 flow (as of Phase 2b):
          1. LLM extracts {new_topics, topic_updates, new_episodes, conflicts}
             from chat_history, with ALL existing topics (all statuses) in the
             prompt so it can reference them by id.
          2. Every new_topic proposal is embedding-matched against existing
             topics. If similarity ≥ MATCH_THRESHOLD we update the existing
             topic instead of creating a duplicate row.
          3. Every conflict is embedding-matched the same way. Match hits get
             their topic flipped to status='Conflicting' with open_question +
             conflict_context filled in; misses create a new Conflicting topic.
             pending_clarifications is no longer written — that was the source
             of "3 rows for one rainy-day conflict".
          4. Each new_episode carries related_topic_names the LLM proposes; we
             embedding-match each name to link the episode to the right topics
             via topic_episode_links junction table. Event time, when stated,
             is extracted too.
        """
        if not chat_history:
            return

        # Wrap the whole consolidate body so the trace row captures
        # total duration + before/after row counts (the closest thing
        # to a "result" this background job has). Trace context exits
        # at function return; the with-block spans the entire method.
        before_stats = self.stats()

        with self.tracer.turn(
            kind="consolidate",
            thread_id=thread_id,
            prompt_version=CME_CONSOLIDATE_VERSION,
            prompt_hash="cme_consolidate_v2b",
            user_input=f"<{len(chat_history)} chat messages>",
        ) as trace:
            self._consolidate_inner(thread_id, chat_history)
            after_stats = self.stats()
            trace.final_answer = (
                f"topics_added={after_stats['topics'] - before_stats['topics']} "
                f"episodes_added={after_stats['episodes'] - before_stats['episodes']} "
                f"pending_delta={after_stats['pending_unresolved'] - before_stats['pending_unresolved']}"
            )

    def _consolidate_inner(
        self, thread_id: str, chat_history: list[dict]
    ) -> None:
        """The actual consolidation work. Extracted so the public
        method can wrap it in a tracer.turn() context without
        indentation gymnastics on a ~300 line body."""
        chat_text = "\n".join(
            f"{msg.get('role', msg.get('type', 'unknown'))}: {msg.get('content', '')}"
            for msg in chat_history
        )

        # Show LLM ALL topics (not just active) so it can reference resolved/conflicting ones too
        all_topics = self.list_topics()
        topics_ctx = json.dumps(
            [
                {
                    "topic_id": t["topic_id"],
                    "name": t["name"],
                    "status": t["status"],
                    "working_conclusion": t["working_conclusion"],
                }
                for t in all_topics
            ],
            ensure_ascii=False,
        )

        profile = self._load_semantic_profile()
        profile_ctx = json.dumps(profile, ensure_ascii=False)

        today_iso = datetime.date.today().isoformat()

        prompt = f"""分析以下教练-运动员对话，提取结构化记忆更新。

**当前所有话题 (含 Resolved 和 Conflicting):**
{topics_ctx}

**用户档案摘要:**
{profile_ctx}

**今日日期:** {today_iso}

**对话记录 (thread: {thread_id}):**
{chat_text}

请输出严格的 JSON，格式如下 (所有字段可选，如无则给空数组):
{{
  "new_topics": [
    {{
      "name": "话题简称（10-30字之内）",
      "root_category": "Running/Injury 或 Running/Performance 或 Health/Sleep 或 Preference/* 等",
      "status": "Open 或 Testing",
      "working_conclusion": "阶段性结论或 null"
    }}
  ],
  "topic_updates": [
    {{
      "topic_id": "已有的 topic_id（必须来自上方列表）",
      "new_status": "Testing 或 Resolved（不要在这里用 Conflicting，走 conflicts[]）",
      "updated_conclusion": "更新后的结论"
    }}
  ],
  "new_episodes": [
    {{
      "event_type": "Training_Insight 或 Health_Observation 或 Race_Performance 等",
      "what": "发生了什么",
      "emotion": "用户的情绪/感受 或 null",
      "lesson_learned": "经验教训",
      "related_topic_names": ["相关话题的名字"],
      "event_date_text": "用户提到的日期原文，如 '3月28号' '上周三' '最近'；没提就 null",
      "event_timestamp": "ISO 8601 日期 YYYY-MM-DD。**只要 what 里出现了任何可以推出具体日期的线索**（'3月22号' / '2026-04-02' / '上周三' / '前天'），就必须换算成 ISO 日期。今日是 {today_iso}，相对日期按此推算。只有真的模糊到无法推断（'最近' '前阵子'）才填 null。"
    }}
  ],
  "conflicts": [
    {{
      "subject_summary": "一句话概括这个冲突的主题，供匹配现有 topic 用",
      "question_for_user": "需要向用户确认的问题",
      "old_belief": "档案/历史中的旧记录",
      "new_evidence": "本次对话中的新证据"
    }}
  ]
}}

重要规则:
- 只提取对话中有实质内容的部分，不要编造
- 如果对话太短或没有有价值的信息，返回所有字段为空数组
- conflicts[] 只在新信息与用户档案或既有话题**真正矛盾**时才生成。**不要**为"同一话题再讨论一次"生成 conflict。
- topic_updates 只针对 topic_id 在上方列表中已存在的
- related_topic_names 写话题的名字（不是 id），系统会自动匹配到正确的 topic_id
- **event_timestamp 铁律**：只要 what 里写了日期（任何形式），就必须同时在 event_timestamp 填成 YYYY-MM-DD。日期不能只存在文本里而结构化字段为 null。
- **related_topic_names 铁律**：如果 episode 的内容明显关联上方列表里的某个 topic（同一症状、同一训练主题、同一偏好等），必须把那个 topic 的名字填进 related_topic_names。不要偷懒留空数组；只有当 episode 真的和所有已知 topic 都无关时才填 []。
"""

        try:
            raw = self._llm_invoke(prompt)
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            print(f"[CME] consolidate parse error: {e}")
            return

        # --- Step 1: new_topics — auto-match or queue for user confirmation ---
        for t in result.get("new_topics", []):
            name = (t.get("name") or "").strip()
            if not name:
                continue
            query = self._topic_signature_text({
                "name": name,
                "working_conclusion": t.get("working_conclusion"),
                "open_question": None,
            })
            match_tid, candidates = self.find_matching_topic(query)
            if match_tid:
                # Same topic re-proposed — update existing instead of duplicating
                updates: dict[str, Any] = {}
                if t.get("working_conclusion"):
                    updates["working_conclusion"] = t["working_conclusion"]
                if t.get("status") and t["status"] in ("Open", "Testing"):
                    # Don't downgrade a Resolved/Conflicting topic via a stray proposal
                    existing = self.get_topic(match_tid) or {}
                    if existing.get("status") not in ("Resolved", "Conflicting"):
                        updates["status"] = t["status"]
                if updates:
                    self.update_topic(match_tid, **updates)
                    self._invalidate_topic_cache(match_tid)
                print(f"[CME] new_topic '{name}' → auto-merged into {match_tid}")
            else:
                # Below threshold — park for user confirmation. UI will let user
                # pick from `candidates` (top-5) or ask for more / create-new / reject.
                # Exception: if there are NO candidates at all (empty topics table),
                # just create — user review queue for an empty DB is silly.
                if candidates:
                    did = self.park_topic_decision("new_topic", t, candidates)
                    print(f"[CME] new_topic '{name}' → parked for review ({did})")
                else:
                    self.create_topic(
                        name=name,
                        root_category=t.get("root_category", "General"),
                        status=t.get("status", "Open"),
                        working_conclusion=t.get("working_conclusion"),
                    )

        # --- Step 2: topic_updates — direct by id ---
        for u in result.get("topic_updates", []):
            tid = u.get("topic_id")
            if not tid:
                continue
            updates: dict[str, Any] = {}
            if u.get("new_status") and u["new_status"] in ("Open", "Testing", "Resolved"):
                updates["status"] = u["new_status"]
            if u.get("updated_conclusion"):
                updates["working_conclusion"] = u["updated_conclusion"]
            if updates:
                self.update_topic(tid, **updates)
                self._invalidate_topic_cache(tid)

        # --- Step 3: conflicts — auto-promote or queue for user confirmation ---
        for c in result.get("conflicts", []):
            question = (c.get("question_for_user") or "").strip()
            if not question:
                continue

            match_query = c.get("subject_summary") or c.get("new_evidence") or question
            match_tid, candidates = self.find_matching_topic(match_query)

            conflict_ctx = {
                "old_belief": c.get("old_belief"),
                "new_evidence": c.get("new_evidence"),
                "detected_in_thread": thread_id,
            }

            if match_tid:
                self.promote_topic_to_conflicting(
                    topic_id=match_tid,
                    open_question=question,
                    conflict_context=conflict_ctx,
                )
                print(f"[CME] conflict → auto-promoted {match_tid} to Conflicting")
            elif candidates:
                # Park for user review — they'll pick the right topic, or say "create new".
                # Carry conflict_context through so resolve can reconstruct the promotion.
                did = self.park_topic_decision("conflict", c, candidates)
                print(f"[CME] conflict → parked for review ({did})")
            else:
                # Empty topics table: just create a fresh Conflicting topic
                new_tid = f"tpc_{uuid.uuid4().hex[:8]}"
                now = self._now()
                self.conn.execute(
                    """INSERT INTO topics
                       (topic_id, root_category, name, status, working_conclusion,
                        open_question, conflict_context, related_episodes,
                        created_at, updated_at)
                       VALUES (?, ?, ?, 'Conflicting', NULL, ?, ?, '[]', ?, ?)""",
                    (
                        new_tid,
                        "Conflict",
                        match_query[:40] or question[:40],
                        question,
                        json.dumps(conflict_ctx, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                self.conn.commit()
                print(f"[CME] conflict → created new Conflicting topic {new_tid} (empty DB path)")

        # --- Step 4: new_episodes — link to topics via embedding match ---
        for ep in result.get("new_episodes", []):
            tids: list[str] = []
            for tname in ep.get("related_topic_names", []) or []:
                if not tname or not tname.strip():
                    continue
                mid, _ = self.find_matching_topic(tname.strip())
                if mid and mid not in tids:
                    tids.append(mid)

            event_type = ep.get("event_type", "General")
            what = ep.get("what", "")

            # Dedup: same (thread, event_type, what) already exists → skip create,
            # but still try to connect any newly-discovered topic links.
            dup_id = self._find_duplicate_episode(thread_id, event_type, what)
            if dup_id:
                for tid in tids:
                    self.add_topic_episode_link(tid, dup_id)
                print(f"[CME] duplicate episode skipped: {dup_id}")
                continue

            event_ts = ep.get("event_timestamp")
            event_date_text = ep.get("event_date_text")
            ts_source = "unknown"
            if event_ts:
                ts_source = "user_explicit" if event_date_text else "caller"

            episode_id = self.create_episode(
                event_type=event_type,
                context={
                    "what": what,
                    "emotion": ep.get("emotion"),
                    "source_thread": thread_id,
                },
                lesson_learned=ep.get("lesson_learned"),
                related_topic_ids=tids,
            )
            # Always write event-time columns so timestamp_source reflects provenance
            # ('unknown' when the LLM couldn't extract a date, not NULL).
            self.conn.execute(
                """UPDATE episodes
                   SET event_timestamp = ?, event_date_text = ?, timestamp_source = ?
                   WHERE episode_id = ?""",
                (event_ts, event_date_text, ts_source, episode_id),
            )
            self.conn.commit()

            # If the LLM left related_topic_names empty, park a decision so the
            # user can pick manually. Don't auto-guess via heuristics — ground
            # truth comes from the user (see feedback memory).
            if not tids and all_topics:
                self.park_topic_decision(
                    kind="episode_linking",
                    proposal={
                        "episode_id": episode_id,
                        "event_type": event_type,
                        "what": what,
                        "lesson_learned": ep.get("lesson_learned"),
                    },
                    candidates=[],
                )

    # ==================================================================
    # API 4: get_active_concierge_prompts
    # ==================================================================
    def get_active_concierge_prompts(self) -> str:
        """
        Generate proactive greeting prompts for the start of a new session.
        Checks for:
          - Topics in 'Conflicting' status (highest priority — must clarify)
          - Legacy pending_clarifications rows (backward compat)
          - Topics in 'Testing' status that may need follow-up
          - Topics in 'Open' status that still lack conclusions
        """
        sections: list[str] = []

        # Conflicting topics — top priority, these contain an open_question
        conflicting = self.list_topics(status="Conflicting")
        if conflicting:
            lines = ["以下话题存在未解决的冲突，请优先向用户澄清:"]
            for t in conflicting:
                q = t.get("open_question") or "（无 open_question）"
                lines.append(f"  - {t['name']}: {q} (topic_id: {t['topic_id']})")
            sections.append("\n".join(lines))

        # Legacy pending_clarifications path — will be empty after v2 migration
        # but consolidation may still emit here until Phase 2b.
        pending = self.list_pending(resolved=False)
        if pending:
            lines = ["有一些之前遗留的问题需要确认 (legacy):"]
            for p in pending:
                lines.append(f"  - {p['question_for_user']} (ID: {p['pending_id']})")
            sections.append("\n".join(lines))

        # Testing topics needing follow-up
        testing = self.list_topics(status="Testing")
        if testing:
            lines = ["以下话题正在验证中，请主动询问用户最新进展和结果:"]
            for t in testing:
                lines.append(f"  - {t['name']}: {t['working_conclusion'] or '等待反馈'}")
            sections.append("\n".join(lines))

        # Open topics still without conclusion
        open_topics = self.list_topics(status="Open")
        if open_topics:
            lines = ["以下话题仍然悬而未决，请询问用户是否有新的进展或信息:"]
            for t in open_topics:
                lines.append(f"  - {t['name']}（尚无结论）")
            sections.append("\n".join(lines))

        if not sections:
            return ""

        return (
            "🚨 **重要指令 — 你必须在本次回复中执行以下操作：**\n"
            "在回答用户当前问题之后，你**必须**在回复末尾专门用一个段落，逐一向用户询问以下遗留话题的最新进展。\n"
            "不要跳过任何一条。用自然的对话方式提问。\n\n"
            + "\n\n".join(sections)
            + "\n\n--- 以上内容必须在回复中体现，否则视为任务未完成 ---"
        )

    # ------------------------------------------------------------------
    # Convenience: stats
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        topics = self.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        episodes = self.conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM pending_clarifications WHERE is_resolved = 0"
        ).fetchone()[0]
        return {"topics": topics, "episodes": episodes, "pending_unresolved": pending}
