"""
Tests for CME Phase 2b:
  - llm_provider.call_embedding and cosine_similarity
  - MemoryOS.find_matching_topic (embedding match with threshold)
  - MemoryOS.promote_topic_to_conflicting (flip + merge context)
  - consolidate_memory_background v2:
      * rain-like conflict matches existing topic → promoted, no new row
      * new_topic proposals get deduped against existing via embedding
      * episodes get related_topic_ids populated via matcher
      * event_timestamp / event_date_text / timestamp_source are persisted
      * no writes to pending_clarifications (Phase 2b removes that path)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_llm_and_embed_caches():
    import llm_provider

    llm_provider._llm_cache.clear()
    llm_provider._embedding_cache.clear()
    yield
    llm_provider._llm_cache.clear()
    llm_provider._embedding_cache.clear()


@pytest.fixture
def mem(tmp_path):
    """MemoryOS with v2 schema applied (mirrors migrations/v2_cme_schema.py)."""
    from cognitive_memory_engine import MemoryOS

    m = MemoryOS(
        db_path=str(tmp_path / "cme.db"),
        semantic_profile_path=str(tmp_path / "sem.json"),
    )
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


def _stub_embeddings(mapping: dict[str, list[float]]):
    """
    Build a patch side_effect for llm_provider.call_embedding that returns
    deterministic vectors based on input text substrings.

    mapping: substring → vector. First matching substring wins.
    Unknown texts get a default orthogonal-ish vector.
    """
    def fake_call_embedding(texts, provider="gemini"):
        out: list[list[float]] = []
        for t in texts:
            vec = [0.01] * 8  # default low-match vector
            for needle, v in mapping.items():
                if needle in t:
                    vec = list(v)
                    break
            out.append(vec)
        return out

    return fake_call_embedding


# ==========================================================================
# A. llm_provider.call_embedding / cosine_similarity
# ==========================================================================

class TestLLMProviderEmbedding:
    def test_call_embedding_empty_list_returns_empty(self):
        from llm_provider import call_embedding

        assert call_embedding([]) == []

    def test_call_embedding_unknown_provider_raises(self):
        from llm_provider import call_embedding

        with pytest.raises(ValueError, match="not registered"):
            call_embedding(["hi"], provider="nonexistent")

    def test_call_embedding_uses_configured_builder(self):
        """Verify embedding calls go through _build_embedder and return the
        underlying client's embed_documents result."""
        from unittest.mock import MagicMock
        import llm_provider

        fake_client = MagicMock()
        fake_client.embed_documents.return_value = [[0.1, 0.2, 0.3]]
        with patch("llm_provider._build_embedder", return_value=fake_client):
            vecs = llm_provider.call_embedding(["hello"], provider="gemini")

        assert vecs == [[0.1, 0.2, 0.3]]
        fake_client.embed_documents.assert_called_once_with(["hello"])

    def test_cosine_similarity_known_values(self):
        from llm_provider import cosine_similarity

        assert cosine_similarity([1, 0], [1, 0]) == pytest.approx(1.0)
        assert cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)
        assert cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)
        # Zero vector short-circuits to 0 (no NaN)
        assert cosine_similarity([0, 0], [1, 1]) == 0.0
        # Mismatched lengths short-circuit to 0
        assert cosine_similarity([1, 2, 3], [1, 2]) == 0.0


# ==========================================================================
# B. find_matching_topic
# ==========================================================================

class TestFindMatchingTopic:
    def test_returns_nothing_when_no_topics_exist(self, mem):
        with patch("cognitive_memory_engine.call_embedding") as m:
            m.side_effect = AssertionError("should not be called when topics empty")
            auto, cands = mem.find_matching_topic("anything")
        assert auto is None
        assert cands == []

    def test_auto_matches_above_threshold(self, mem):
        tid = mem.create_topic(name="下雨天跑步偏好", root_category="Preference")
        # Identical vectors → similarity 1.0 → above threshold
        with patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({"下雨天跑步": [1.0, 0.0, 0.0]}),
        ):
            auto, cands = mem.find_matching_topic("下雨天跑步偏好")
        assert auto == tid
        assert cands[0]["topic_id"] == tid
        assert cands[0]["score"] == pytest.approx(1.0)

    def test_no_match_below_threshold_returns_candidates_only(self, mem):
        t1 = mem.create_topic(name="心率区间", root_category="Running")
        with patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({
                "心率区间": [0.0, 1.0, 0.0],   # topic vec
                "下雨":      [1.0, 0.0, 0.0],   # query vec (orthogonal → sim 0)
            }),
        ):
            auto, cands = mem.find_matching_topic("下雨天怎么办")
        assert auto is None
        assert len(cands) == 1
        assert cands[0]["topic_id"] == t1
        assert cands[0]["score"] == pytest.approx(0.0)

    def test_ranks_multiple_candidates_by_score(self, mem):
        t_rain = mem.create_topic(name="下雨天跑步偏好", root_category="Pref")
        t_hr = mem.create_topic(name="心率区间", root_category="Running")
        with patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({
                "下雨天跑步": [1.0, 0.0, 0.0],     # topic 1
                "心率区间":    [0.5, 0.87, 0.0],    # topic 2 (partial overlap)
                "下雨":        [1.0, 0.0, 0.0],     # query (exact match to topic 1)
            }),
        ):
            _auto, cands = mem.find_matching_topic("下雨你觉得怎么样")
        # topic 1 should be first (score 1.0), topic 2 second (score 0.5)
        assert cands[0]["topic_id"] == t_rain
        assert cands[1]["topic_id"] == t_hr
        assert cands[0]["score"] > cands[1]["score"]

    def test_embed_failure_returns_no_match_gracefully(self, mem):
        mem.create_topic(name="anything", root_category="General")
        with patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=RuntimeError("gemini down"),
        ):
            auto, cands = mem.find_matching_topic("query")
        assert auto is None
        assert cands == []


# ==========================================================================
# C. promote_topic_to_conflicting
# ==========================================================================

class TestPromoteToConflicting:
    def test_flips_status_and_populates_open_question(self, mem):
        tid = mem.create_topic(name="rain", root_category="Pref")
        ok = mem.promote_topic_to_conflicting(
            tid,
            open_question="改变了吗？",
            conflict_context={"old_belief": "A", "new_evidence": "B"},
        )
        assert ok is True
        t = mem.get_topic(tid)
        assert t["status"] == "Conflicting"
        assert t["open_question"] == "改变了吗？"
        cc = json.loads(t["conflict_context"])
        assert cc["old_belief"] == "A"
        assert cc["new_evidence"] == "B"

    def test_merges_prior_conflict_context(self, mem):
        tid = mem.create_topic(name="rain", root_category="Pref")
        mem.promote_topic_to_conflicting(
            tid,
            open_question="v1?",
            conflict_context={"target_node": "pref_weather_rain", "old_belief": "v1"},
        )
        mem.promote_topic_to_conflicting(
            tid,
            open_question="v2?",
            conflict_context={"new_evidence": "v2_evidence"},
        )
        t = mem.get_topic(tid)
        cc = json.loads(t["conflict_context"])
        assert cc["target_node"] == "pref_weather_rain"  # preserved from v1
        assert cc["new_evidence"] == "v2_evidence"       # from v2

    def test_nonexistent_topic_returns_false(self, mem):
        assert mem.promote_topic_to_conflicting("tpc_missing", "q?", {}) is False


# ==========================================================================
# D. consolidate_memory_background v2
# ==========================================================================

class TestConsolidateV2:
    CHAT = [{"role": "human", "content": "下雨天跑步太爽了"}]

    def test_conflict_matches_existing_topic_promotes_instead_of_creating(self, mem):
        """The rain regression: same conflict re-detected should NOT add a new row."""
        rain_tid = mem.create_topic(
            name="下雨天跑步偏好",
            root_category="Preference/Weather",
            status="Open",
        )

        llm_json = {
            "new_topics": [],
            "topic_updates": [],
            "new_episodes": [],
            "conflicts": [
                {
                    "subject_summary": "下雨天跑步偏好",
                    "question_for_user": "偏好变了还是夏天例外？",
                    "old_belief": "讨厌下雨跑步",
                    "new_evidence": "说太爽了",
                }
            ],
        }

        topics_before = len(mem.list_topics())
        pending_before = len(mem.list_pending(resolved=False))

        # Align LLM and embeddings so rain-query matches rain-topic
        with patch(
            "cognitive_memory_engine.call_llm",
            return_value=(AIMessage(content=json.dumps(llm_json)), "gemini"),
        ), patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({"下雨天跑步": [1.0, 0.0, 0.0]}),
        ):
            mem.consolidate_memory_background("t1", self.CHAT)

        assert len(mem.list_topics()) == topics_before  # no duplicate row
        t = mem.get_topic(rain_tid)
        assert t["status"] == "Conflicting"
        assert "偏好变了" in t["open_question"]
        # And nothing leaked into pending_clarifications
        assert len(mem.list_pending(resolved=False)) == pending_before

    def test_new_topic_proposal_matching_existing_updates_instead_of_duplicating(self, mem):
        existing = mem.create_topic(
            name="心率区间不匹配",
            root_category="Running",
            status="Open",
            working_conclusion=None,
        )

        llm_json = {
            "new_topics": [
                {
                    "name": "训练心率区间偏高",  # different wording, same topic
                    "root_category": "Running",
                    "status": "Testing",
                    "working_conclusion": "需要重测最大心率",
                }
            ],
            "topic_updates": [],
            "new_episodes": [],
            "conflicts": [],
        }

        with patch(
            "cognitive_memory_engine.call_llm",
            return_value=(AIMessage(content=json.dumps(llm_json)), "gemini"),
        ), patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({"心率": [1.0, 0.0, 0.0]}),
        ):
            mem.consolidate_memory_background("t2", self.CHAT)

        assert len(mem.list_topics()) == 1  # no new row created
        t = mem.get_topic(existing)
        assert t["status"] == "Testing"
        assert "最大心率" in (t["working_conclusion"] or "")

    def test_episode_gets_related_topic_ids_via_match(self, mem):
        hr_tid = mem.create_topic(name="心率区间", root_category="Running")
        pace_tid = mem.create_topic(name="马拉松配速", root_category="Running")

        llm_json = {
            "new_topics": [],
            "topic_updates": [],
            "new_episodes": [
                {
                    "event_type": "Race_Performance",
                    "what": "半马 85 分钟心率飙到 214",
                    "emotion": "体感差",
                    "lesson_learned": "75-85 分钟有瓶颈",
                    "related_topic_names": ["心率区间", "马拉松配速"],
                    "event_date_text": "今天",
                    "event_timestamp": "2026-04-19T10:00:00Z",
                }
            ],
            "conflicts": [],
        }

        with patch(
            "cognitive_memory_engine.call_llm",
            return_value=(AIMessage(content=json.dumps(llm_json)), "gemini"),
        ), patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({
                "心率区间": [1.0, 0.0, 0.0],
                "马拉松配速": [0.0, 1.0, 0.0],
            }),
        ):
            mem.consolidate_memory_background("t3", self.CHAT)

        # Both topics should now have the episode linked in the junction table
        hr_eps = mem.get_topic_episodes(hr_tid)
        pace_eps = mem.get_topic_episodes(pace_tid)
        assert len(hr_eps) == 1
        assert len(pace_eps) == 1
        assert hr_eps[0]["episode_id"] == pace_eps[0]["episode_id"]

        # event_timestamp / event_date_text / timestamp_source persisted
        ep_row = mem.conn.execute(
            "SELECT event_timestamp, event_date_text, timestamp_source FROM episodes"
        ).fetchone()
        assert ep_row["event_timestamp"] == "2026-04-19T10:00:00Z"
        assert ep_row["event_date_text"] == "今天"
        assert ep_row["timestamp_source"] == "user_explicit"

    def test_new_unmatched_conflict_creates_conflicting_topic(self, mem):
        """When no existing topic matches, a brand-new Conflicting topic is born."""
        llm_json = {
            "new_topics": [],
            "topic_updates": [],
            "new_episodes": [],
            "conflicts": [
                {
                    "subject_summary": "新发现的冲突主题",
                    "question_for_user": "是这样吗？",
                    "old_belief": "x",
                    "new_evidence": "y",
                }
            ],
        }

        with patch(
            "cognitive_memory_engine.call_llm",
            return_value=(AIMessage(content=json.dumps(llm_json)), "gemini"),
        ), patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({}),  # no matches
        ):
            mem.consolidate_memory_background("t4", self.CHAT)

        conflicting = mem.list_topics(status="Conflicting")
        assert len(conflicting) == 1
        assert "是这样吗？" in (conflicting[0]["open_question"] or "")
        # pending_clarifications stays empty — 2b removed that write path
        assert mem.list_pending(resolved=False) == []

    def test_empty_chat_is_noop(self, mem):
        with patch("cognitive_memory_engine.call_llm") as mc, patch(
            "cognitive_memory_engine.call_embedding"
        ) as me:
            mem.consolidate_memory_background("t5", [])
            assert not mc.called
            assert not me.called


# ==========================================================================
# E. Decision queue (low-confidence proposals → UI confirmation)
# ==========================================================================

class TestDecisionQueue:
    @pytest.fixture(autouse=True)
    def _install_decisions_table(self, mem):
        """The v2 fixture predates topic_decisions; create it per-test here."""
        mem.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS topic_decisions (
                decision_id     TEXT PRIMARY KEY,
                kind            TEXT NOT NULL CHECK(kind IN ('new_topic', 'conflict')),
                proposal_json   TEXT NOT NULL,
                candidates_json TEXT NOT NULL DEFAULT '[]',
                status          TEXT NOT NULL DEFAULT 'pending'
                                CHECK(status IN ('pending', 'merged', 'created', 'rejected')),
                resolution      TEXT,
                created_at      TEXT NOT NULL,
                resolved_at     TEXT
            );
            """
        )

    def test_park_and_list_pending(self, mem):
        did = mem.park_topic_decision(
            kind="new_topic",
            proposal={"name": "fatigue pattern", "status": "Open"},
            candidates=[{"topic_id": "tpc_x", "score": 0.71, "name": "fatigue", "status": "Open"}],
        )
        items = mem.list_pending_decisions()
        assert len(items) == 1
        assert items[0]["decision_id"] == did
        assert items[0]["kind"] == "new_topic"
        assert items[0]["proposal"]["name"] == "fatigue pattern"
        assert items[0]["candidates"][0]["score"] == 0.71

    def test_resolve_merge_new_topic(self, mem):
        target = mem.create_topic(name="existing", root_category="X", working_conclusion=None)
        did = mem.park_topic_decision(
            "new_topic",
            {"name": "merged-in", "working_conclusion": "new info", "status": "Testing"},
            [{"topic_id": target, "score": 0.72, "name": "existing", "status": "Open"}],
        )
        tid = mem.resolve_topic_decision(did, "merge", target_topic_id=target)
        assert tid == target
        t = mem.get_topic(target)
        assert t["working_conclusion"] == "new info"
        assert t["status"] == "Testing"
        # Decision is no longer pending
        assert mem.list_pending_decisions() == []

    def test_resolve_create_new_topic(self, mem):
        mem.create_topic(name="unrelated", root_category="X")
        did = mem.park_topic_decision(
            "new_topic",
            {"name": "brand new", "root_category": "Y", "status": "Open", "working_conclusion": "fresh"},
            [{"topic_id": "tpc_unrelated", "score": 0.60, "name": "unrelated", "status": "Open"}],
        )
        tid = mem.resolve_topic_decision(did, "create_new")
        assert tid and tid.startswith("tpc_")
        created = mem.get_topic(tid)
        assert created["name"] == "brand new"
        assert created["working_conclusion"] == "fresh"

    def test_resolve_merge_conflict_promotes_target(self, mem):
        target = mem.create_topic(name="下雨跑步", root_category="Pref")
        did = mem.park_topic_decision(
            "conflict",
            {
                "question_for_user": "变了吗？",
                "old_belief": "hate",
                "new_evidence": "love",
                "subject_summary": "rain",
            },
            [{"topic_id": target, "score": 0.75, "name": "下雨跑步", "status": "Open"}],
        )
        tid = mem.resolve_topic_decision(did, "merge", target_topic_id=target)
        assert tid == target
        t = mem.get_topic(target)
        assert t["status"] == "Conflicting"
        assert t["open_question"] == "变了吗？"

    def test_resolve_reject_leaves_no_topic(self, mem):
        topics_before = len(mem.list_topics())
        did = mem.park_topic_decision(
            "new_topic",
            {"name": "spurious"},
            [],
        )
        res = mem.resolve_topic_decision(did, "reject")
        assert res is None
        assert len(mem.list_topics()) == topics_before
        # Still not pending anymore
        assert mem.list_pending_decisions() == []

    def test_resolve_nonexistent_returns_empty_string(self, mem):
        assert mem.resolve_topic_decision("dec_nope", "reject") == ""

    def test_consolidation_parks_below_threshold_when_candidates_exist(self, mem):
        mem.create_topic(name="心率区间不匹配", root_category="Running")
        llm_json = {
            "new_topics": [
                {"name": "训练量与疲劳", "root_category": "Running", "status": "Open"}
            ],
            "topic_updates": [],
            "new_episodes": [],
            "conflicts": [],
        }
        # Orthogonal vectors → best score = 0.0, well below 0.80 threshold
        with patch(
            "cognitive_memory_engine.call_llm",
            return_value=(AIMessage(content=json.dumps(llm_json)), "gemini"),
        ), patch(
            "cognitive_memory_engine.call_embedding",
            side_effect=_stub_embeddings({"心率": [1, 0, 0], "训练量": [0, 1, 0]}),
        ):
            mem.consolidate_memory_background("t_park", [{"role": "human", "content": "x"}])

        # The proposal should be parked, NOT immediately turned into a topic
        assert len(mem.list_topics()) == 1  # still just the seed
        pending = mem.list_pending_decisions()
        assert len(pending) == 1
        assert pending[0]["proposal"]["name"] == "训练量与疲劳"
