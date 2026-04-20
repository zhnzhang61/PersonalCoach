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
from typing import Any

from llm_provider import call_embedding, call_llm, cosine_similarity


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
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id        TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    context_json      TEXT NOT NULL,       -- full 5W1H+E JSON
    lesson_learned    TEXT,
    related_topic_ids TEXT DEFAULT '[]',   -- JSON array
    created_at        TEXT NOT NULL
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
CREATE TABLE IF NOT EXISTS topic_decisions (
    decision_id     TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK(kind IN ('new_topic', 'conflict', 'episode_linking')),
    proposal_json   TEXT NOT NULL,           -- LLM proposal as emitted
    candidates_json TEXT NOT NULL DEFAULT '[]',  -- [{topic_id, name, status, score}, ...]
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'merged', 'created', 'rejected', 'linked')),
    resolution      TEXT,                     -- "merged:tpc_xxx" | "created:tpc_xxx" | "linked:tpc_a,tpc_b" | "rejected"
    created_at      TEXT NOT NULL,
    resolved_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp);
CREATE INDEX IF NOT EXISTS idx_pending_unresolved ON pending_clarifications(is_resolved);
CREATE INDEX IF NOT EXISTS idx_topic_decisions_pending ON topic_decisions(status);
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

        # In-memory topic-embedding cache. Key: topic_id. Value: (signature, vector).
        # The `signature` is a hash of (name + working_conclusion) — if it
        # differs from the stored signature we re-embed, so stale cache entries
        # self-heal after update_topic() without needing explicit invalidation.
        self._topic_embeddings: dict[str, tuple[str, list[float]]] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _migrate_topic_decisions_check(self) -> None:
        """
        CHECK constraints in SQLite are immutable; if the existing DB was
        created before `episode_linking`/`linked` were legal values, rebuild
        the table. Idempotent: noop when the current schema already allows them.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='topic_decisions'"
        ).fetchone()
        if not row or not row["sql"]:
            return
        if "episode_linking" in row["sql"] and "linked" in row["sql"]:
            return  # already migrated

        self.conn.executescript(
            """
            BEGIN;
            CREATE TABLE topic_decisions_new (
                decision_id     TEXT PRIMARY KEY,
                kind            TEXT NOT NULL CHECK(kind IN ('new_topic', 'conflict', 'episode_linking')),
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

        msg, _ = call_llm(
            [
                SystemMessage(content="You are a memory analysis assistant. Always respond in valid JSON when asked for JSON."),
                HumanMessage(content=prompt),
            ],
            role="structured",
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

    @staticmethod
    def _topic_signature_text(topic: dict) -> str:
        """The text that represents a topic in embedding space."""
        parts = [
            topic.get("name", ""),
            topic.get("working_conclusion") or "",
            topic.get("open_question") or "",
        ]
        return " :: ".join(p.strip() for p in parts if p and p.strip())

    def _embed_topic(self, topic: dict) -> list[float]:
        """Return the cached embedding for a topic, recomputing if stale."""
        import hashlib

        sig_text = self._topic_signature_text(topic)
        sig = hashlib.sha1(sig_text.encode("utf-8")).hexdigest()
        tid = topic["topic_id"]

        cached = self._topic_embeddings.get(tid)
        if cached and cached[0] == sig:
            return cached[1]

        vec = call_embedding([sig_text], provider=self.TOPIC_EMBED_PROVIDER)[0]
        self._topic_embeddings[tid] = (sig, vec)
        return vec

    def find_matching_topic(
        self,
        query_text: str,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> tuple[str | None, list[dict]]:
        """
        Find the topic most semantically similar to `query_text`.

        Args:
            query_text: natural-language text describing the new proposal.
            top_k: candidates to return when no auto-match is confident enough.
            threshold: cosine similarity above which we auto-merge. None uses
                       MATCH_THRESHOLD.

        Returns:
            (auto_match_topic_id_or_None, ranked_candidates_with_scores)

            Candidates list: [{topic_id, name, status, score}, ...] sorted by
            score desc, length min(top_k, N). Always returned even when an
            auto-match is found, so UI can show runners-up.
        """
        if threshold is None:
            threshold = self.MATCH_THRESHOLD

        topics = self.list_topics()
        if not topics or not (query_text and query_text.strip()):
            return (None, [])

        try:
            query_vec = call_embedding([query_text], provider=self.TOPIC_EMBED_PROVIDER)[0]
        except Exception as e:
            # Embedding failure: return no match rather than crashing consolidation.
            # Caller will treat it as "no match" and fall back to create-new.
            print(f"[CME] topic match embedding failed: {e}")
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
        kind: 'new_topic' | 'conflict' | 'episode_linking'. UI surfaces
        pending rows for user action.
        """
        if kind not in ("new_topic", "conflict", "episode_linking"):
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
                    self._topic_embeddings.pop(target_topic_id, None)
            else:  # conflict
                self.promote_topic_to_conflicting(
                    target_topic_id,
                    open_question=proposal.get("question_for_user", ""),
                    conflict_context={
                        "old_belief": proposal.get("old_belief"),
                        "new_evidence": proposal.get("new_evidence"),
                    },
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
            else:  # conflict → create Conflicting topic from scratch
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
        self._topic_embeddings.pop(topic_id, None)
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
                    self._topic_embeddings.pop(match_tid, None)
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
                self._topic_embeddings.pop(tid, None)

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
