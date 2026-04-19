"""
Tests for CME Phase 2a changes:
  - create_episode dual-writes to topic_episode_links
  - add_topic_episode_link is idempotent
  - get_topic_episodes reads from the junction table
  - retrieve_working_context surfaces status='Conflicting' topics
  - get_active_concierge_prompts surfaces status='Conflicting' topics

Everything runs against a fresh tmp_path sqlite DB — no external state touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# Fixture: MemoryOS with v2 schema already applied
# --------------------------------------------------------------------------

@pytest.fixture
def mem(tmp_path):
    """Fresh MemoryOS instance; then apply v2 schema changes to match production."""
    from cognitive_memory_engine import MemoryOS

    db_path = tmp_path / "cme.db"
    profile_path = tmp_path / "semantic.json"
    m = MemoryOS(db_path=str(db_path), semantic_profile_path=str(profile_path))

    # Apply v2 schema changes (mirrors migrations/v2_cme_schema.py)
    m.conn.executescript(
        """
        BEGIN;
        CREATE TABLE topics_new (
            topic_id TEXT PRIMARY KEY,
            root_category TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'Open'
                   CHECK(status IN ('Open','Testing','Resolved','Conflicting')),
            working_conclusion TEXT,
            open_question TEXT,
            conflict_context TEXT,
            related_episodes TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO topics_new SELECT topic_id, root_category, name, status,
            working_conclusion, NULL, NULL, related_episodes, created_at, updated_at
        FROM topics;
        DROP TABLE topics;
        ALTER TABLE topics_new RENAME TO topics;
        CREATE INDEX idx_topics_status ON topics(status);

        ALTER TABLE episodes ADD COLUMN event_timestamp TEXT;
        ALTER TABLE episodes ADD COLUMN event_date_text TEXT;
        ALTER TABLE episodes ADD COLUMN timestamp_source TEXT;

        CREATE TABLE topic_episode_links (
            topic_id TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (topic_id, episode_id),
            FOREIGN KEY (topic_id) REFERENCES topics(topic_id) ON DELETE CASCADE,
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_tel_episode ON topic_episode_links(episode_id);
        CREATE INDEX idx_tel_topic ON topic_episode_links(topic_id);
        COMMIT;
        """
    )
    return m


# --------------------------------------------------------------------------
# Junction dual-write
# --------------------------------------------------------------------------

class TestEpisodeJunctionDualWrite:
    def test_create_episode_writes_junction_rows(self, mem):
        t1 = mem.create_topic(name="心率区间", root_category="Running")
        t2 = mem.create_topic(name="耐力瓶颈", root_category="Running")

        eid = mem.create_episode(
            event_type="Race_Performance",
            context={"what": "半马 85 分钟心率飙到 214"},
            lesson_learned="高强度下存在生理极限",
            related_topic_ids=[t1, t2],
        )

        rows = mem.conn.execute(
            "SELECT topic_id, episode_id FROM topic_episode_links WHERE episode_id = ?",
            (eid,),
        ).fetchall()
        links = {(r["topic_id"], r["episode_id"]) for r in rows}
        assert links == {(t1, eid), (t2, eid)}

    def test_create_episode_with_no_topics_writes_no_junction(self, mem):
        eid = mem.create_episode(
            event_type="Training_Insight",
            context={"what": "随便一条"},
        )
        n = mem.conn.execute(
            "SELECT COUNT(*) FROM topic_episode_links WHERE episode_id = ?", (eid,)
        ).fetchone()[0]
        assert n == 0

    def test_junction_write_is_idempotent_via_add_link(self, mem):
        tid = mem.create_topic(name="test", root_category="General")
        eid = mem.create_episode(event_type="General", context={"what": "x"})

        assert mem.add_topic_episode_link(tid, eid) is True   # first insert
        assert mem.add_topic_episode_link(tid, eid) is False  # already exists
        n = mem.conn.execute(
            "SELECT COUNT(*) FROM topic_episode_links WHERE topic_id=? AND episode_id=?",
            (tid, eid),
        ).fetchone()[0]
        assert n == 1


# --------------------------------------------------------------------------
# get_topic_episodes reads from junction
# --------------------------------------------------------------------------

class TestGetTopicEpisodes:
    def test_returns_only_linked_episodes(self, mem):
        t = mem.create_topic(name="rain", root_category="Preference")
        e_linked = mem.create_episode(
            event_type="Observation", context={"what": "rain run"},
            related_topic_ids=[t],
        )
        e_unlinked = mem.create_episode(
            event_type="Observation", context={"what": "sunny run"},
        )

        eps = mem.get_topic_episodes(t)
        ids = {e["episode_id"] for e in eps}
        assert e_linked in ids
        assert e_unlinked not in ids

    def test_returns_empty_for_topic_with_no_links(self, mem):
        t = mem.create_topic(name="lonely", root_category="General")
        assert mem.get_topic_episodes(t) == []


# --------------------------------------------------------------------------
# Conflicting topic surfacing
# --------------------------------------------------------------------------

class TestConflictingTopicSurfacing:
    def _make_conflict(self, mem, name="下雨天跑步偏好"):
        # create an Open topic first, then promote it to Conflicting with
        # open_question + conflict_context filled in (simulating migration or 2b)
        tid = mem.create_topic(name=name, root_category="Preference/Weather")
        mem.conn.execute(
            """UPDATE topics
               SET status = 'Conflicting',
                   open_question = ?,
                   conflict_context = ?
               WHERE topic_id = ?""",
            (
                "偏好彻底改变了，还是仅限夏天？",
                json.dumps({"old_belief": "讨厌雨天", "new_evidence": "雨天太爽"}),
                tid,
            ),
        )
        mem.conn.commit()
        return tid

    def test_retrieve_working_context_includes_conflicting_section(self, mem):
        tid = self._make_conflict(mem)
        ctx = mem.retrieve_working_context(user_query="今天我又跑了雨天")

        assert "冲突待澄清" in ctx
        assert tid in ctx
        assert "偏好彻底改变了" in ctx

    def test_conflicting_precedes_legacy_pending_in_context(self, mem):
        # If both a Conflicting topic AND a legacy pending exist, Conflicting
        # must render FIRST so the LLM prioritizes it.
        tid = self._make_conflict(mem)
        mem.create_pending(
            trigger_type="Preference_Conflict",
            question_for_user="legacy question",
            resolution_callback={"action": "refine_preference_rule"},
        )
        ctx = mem.retrieve_working_context(user_query="rain")

        idx_conflict = ctx.find("冲突待澄清")
        idx_legacy = ctx.find("legacy pending_clarifications")
        assert idx_conflict != -1
        assert idx_legacy != -1
        assert idx_conflict < idx_legacy

    def test_concierge_prompts_includes_conflicting(self, mem):
        tid = self._make_conflict(mem)
        prompt = mem.get_active_concierge_prompts()

        assert "冲突" in prompt
        assert tid in prompt
        assert "偏好彻底改变了" in prompt

    def test_concierge_empty_when_no_conflicts_or_topics(self, mem):
        # Nothing in the DB → empty prompt
        assert mem.get_active_concierge_prompts() == ""
