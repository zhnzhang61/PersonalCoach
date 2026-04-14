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

# ---------------------------------------------------------------------------
# LLM helper – uses the same Gemini setup as the rest of the project
# ---------------------------------------------------------------------------
_llm_instance = None


def _get_llm(api_key: str | None = None):
    """Lazy-init a lightweight Gemini model for memory consolidation."""
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    from langchain_google_genai import ChatGoogleGenerativeAI

    if api_key is None:
        from dotenv import load_dotenv

        load_dotenv()
        api_key = os.getenv("GEMINI_KEY") or os.getenv("GEMINI_API_KEY")

    _llm_instance = ChatGoogleGenerativeAI(
        model="gemini-2.5-flash", temperature=0.1, api_key=api_key
    )
    return _llm_instance


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

CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);
CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp);
CREATE INDEX IF NOT EXISTS idx_pending_unresolved ON pending_clarifications(is_resolved);
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
        api_key: str | None = None,
    ):
        self.db_path = db_path
        self.semantic_profile_path = semantic_profile_path
        self._api_key = api_key

        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
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

        llm = _get_llm(self._api_key)
        response = llm.invoke(
            [
                SystemMessage(content="You are a memory analysis assistant. Always respond in valid JSON when asked for JSON."),
                HumanMessage(content=prompt),
            ]
        )
        content = response.content
        if isinstance(content, list):
            content = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and "text" in block
            )
        return content.strip()

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
        self.conn.commit()
        return episode_id

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

        # Pending questions take highest priority
        if pending:
            pq_lines = ["⚠️ **待确认事项 (请优先向用户提问以下问题):**"]
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
    # API 3: consolidate_memory_background
    # ==================================================================
    def consolidate_memory_background(
        self, thread_id: str, chat_history: list[dict]
    ) -> None:
        """
        Post-conversation memory consolidation.  Runs after a conversation ends.

        Steps:
          1. Analyse chat_history with LLM to extract topics, episodes, conflicts
          2. Update topic statuses
          3. Create new episodes with lessons
          4. Generate pending clarifications for conflicts
        """
        if not chat_history:
            return

        # Build a text representation of the chat
        chat_text = "\n".join(
            f"{msg.get('role', msg.get('type', 'unknown'))}: {msg.get('content', '')}"
            for msg in chat_history
        )

        # Get existing topics for context
        active_topics = self.list_topics(status="Open") + self.list_topics(
            status="Testing"
        )
        topics_ctx = json.dumps(
            [
                {"topic_id": t["topic_id"], "name": t["name"], "status": t["status"],
                 "working_conclusion": t["working_conclusion"]}
                for t in active_topics
            ],
            ensure_ascii=False,
        )

        # Load semantic profile for conflict detection
        profile = self._load_semantic_profile()
        profile_ctx = json.dumps(profile, ensure_ascii=False)

        prompt = f"""分析以下教练-运动员对话，提取结构化记忆更新。

**当前活跃话题:**
{topics_ctx}

**用户档案摘要:**
{profile_ctx}

**对话记录 (thread: {thread_id}):**
{chat_text}

请输出严格的 JSON，格式如下 (所有字段可选，如无则给空数组):
{{
  "new_topics": [
    {{
      "name": "话题简称",
      "root_category": "Running/Injury 或 Running/Performance 或 Health/Sleep 等",
      "status": "Open 或 Testing",
      "working_conclusion": "阶段性结论或 null"
    }}
  ],
  "topic_updates": [
    {{
      "topic_id": "已有的 topic_id",
      "new_status": "Testing 或 Resolved",
      "updated_conclusion": "更新后的结论"
    }}
  ],
  "new_episodes": [
    {{
      "event_type": "Training_Insight 或 Health_Observation 或 Race_Performance 等",
      "what": "发生了什么",
      "emotion": "用户的情绪/感受",
      "lesson_learned": "经验教训"
    }}
  ],
  "conflicts": [
    {{
      "trigger_type": "Entity_Conflict 或 Preference_Conflict",
      "question_for_user": "需要向用户确认的问题",
      "old_belief": "旧记录",
      "new_evidence": "新证据"
    }}
  ]
}}

重要规则:
- 只提取对话中有实质内容的部分，不要编造
- 如果对话太短或没有有价值的信息，返回所有字段为空数组
- 冲突检测：如果新信息与用户档案或已有话题矛盾，生成 conflict
- topic_updates 只针对 topic_id 在"当前活跃话题"中已存在的话题
"""

        try:
            raw = self._llm_invoke(prompt)
            # Strip markdown code fences if present
            raw = raw.replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
        except (json.JSONDecodeError, Exception) as e:
            print(f"[CME] consolidate parse error: {e}")
            return

        # --- Apply extractions ---

        # New topics
        for t in result.get("new_topics", []):
            self.create_topic(
                name=t["name"],
                root_category=t.get("root_category", "General"),
                status=t.get("status", "Open"),
                working_conclusion=t.get("working_conclusion"),
            )

        # Topic updates
        for u in result.get("topic_updates", []):
            tid = u.get("topic_id")
            if not tid:
                continue
            updates: dict[str, Any] = {}
            if u.get("new_status"):
                updates["status"] = u["new_status"]
            if u.get("updated_conclusion"):
                updates["working_conclusion"] = u["updated_conclusion"]
            if updates:
                self.update_topic(tid, **updates)

        # New episodes
        for ep in result.get("new_episodes", []):
            self.create_episode(
                event_type=ep.get("event_type", "General"),
                context={
                    "what": ep.get("what", ""),
                    "emotion": ep.get("emotion"),
                    "source_thread": thread_id,
                },
                lesson_learned=ep.get("lesson_learned"),
            )

        # Conflicts → pending clarifications
        for c in result.get("conflicts", []):
            trigger = c.get("trigger_type", "Preference_Conflict")
            if trigger not in ("Entity_Conflict", "Preference_Conflict"):
                trigger = "Preference_Conflict"
            self.create_pending(
                trigger_type=trigger,
                question_for_user=c["question_for_user"],
                resolution_callback={
                    "action": "refine_preference_rule"
                    if trigger == "Preference_Conflict"
                    else "merge_nodes",
                    "conflict_context": {
                        "old_belief": c.get("old_belief"),
                        "new_evidence": c.get("new_evidence"),
                    },
                },
            )

    # ==================================================================
    # API 4: get_active_concierge_prompts
    # ==================================================================
    def get_active_concierge_prompts(self) -> str:
        """
        Generate proactive greeting prompts for the start of a new session.
        Checks for:
          - Unresolved pending clarifications
          - Topics in 'Testing' status that may need follow-up
          - Topics in 'Open' status that still lack conclusions
        """
        sections: list[str] = []

        # Pending questions
        pending = self.list_pending(resolved=False)
        if pending:
            lines = ["有一些之前遗留的问题需要确认:"]
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
