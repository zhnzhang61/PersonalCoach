"""
Tests for the coach-intake capture layer (PROJECT_GUIDE §3.4.5, PR-1):
  - backend.coach_intake taxonomy (PROFILE_SLOTS / CYCLE_SLOTS, lookups)
  - MemoryOS.record_coach_fact (lossless episode + two-threshold write)
  - MemoryOS.find_matching_topic(root_category=...) area scoping
  - MemoryOS.get_coach_profile / get_cycle_config hard coverage
  - resolve_topic_decision links the parked episode (merge + create_new)

Pure data layer — no agent / MCP / prompt. Embedding calls are stubbed or
the matcher is patched, so no network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend import coach_intake as ci  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_llm_and_embed_caches():
    import backend.llm_provider as llm_provider

    llm_provider._llm_cache.clear()
    llm_provider._embedding_cache.clear()
    yield
    llm_provider._llm_cache.clear()
    llm_provider._embedding_cache.clear()


@pytest.fixture
def mem(tmp_path):
    """MemoryOS with v2 schema applied (mirrors test_cme_v2b.mem) — topics
    rebuilt to carry Conflicting status + open_question + conflict_context so
    tests run against the production-shaped schema."""
    from backend.cognitive_memory_engine import MemoryOS

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
            related_models TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO topics_new SELECT topic_id, root_category, name, status,
            working_conclusion, NULL, NULL, related_episodes, related_models,
            created_at, updated_at
        FROM topics;
        DROP TABLE topics;
        ALTER TABLE topics_new RENAME TO topics;
        CREATE INDEX idx_topics_status ON topics(status);
        COMMIT;
        """
    )
    return m


def _patch_match(mem, auto, candidates):
    """Patch mem.find_matching_topic to deterministically return one branch."""
    return patch.object(mem, "find_matching_topic", return_value=(auto, candidates))


# ==========================================================================
# A. taxonomy module
# ==========================================================================
class TestTaxonomy:
    def test_slot_counts(self):
        assert len(ci.PROFILE_SLOTS) == 8
        assert len(ci.CYCLE_SLOTS) == 11
        assert len(ci.ALL_AREAS) == 19

    def test_areas_are_qualified_and_disjoint(self):
        assert all(s.area.startswith("Profile.") for s in ci.PROFILE_SLOTS)
        assert all(s.area.startswith("Cycle.") for s in ci.CYCLE_SLOTS)
        assert ci.PROFILE_AREAS.isdisjoint(ci.CYCLE_AREAS)

    def test_no_duplicate_areas(self):
        all_areas = [s.area for s in (*ci.PROFILE_SLOTS, *ci.CYCLE_SLOTS)]
        assert len(all_areas) == len(set(all_areas))

    def test_every_slot_has_label_question_and_examples(self):
        for s in (*ci.PROFILE_SLOTS, *ci.CYCLE_SLOTS):
            assert s.label.strip()
            assert s.question.strip()
            assert s.good_example.strip()
            assert s.vague_example.strip()
            # good and vague must actually differ — the contrast is the point
            assert s.good_example != s.vague_example

    def test_slot_by_area_covers_all(self):
        assert set(ci.SLOT_BY_AREA) == ci.ALL_AREAS

    def test_event_type_for_area(self):
        assert ci.event_type_for_area("Profile.injury_history") == "profile"
        assert ci.event_type_for_area("Cycle.goal") == "cycle_config"

    def test_event_type_unknown_raises(self):
        with pytest.raises(ValueError):
            ci.event_type_for_area("Bogus.area")


class TestRenderIntakePromptSection:
    def test_returns_nonempty_str(self):
        text = ci.render_intake_prompt_section()
        assert isinstance(text, str)
        assert text.strip()

    def test_names_the_tools_and_rules(self):
        text = ci.render_intake_prompt_section()
        for marker in (
            "get_coach_profile",
            "get_cycle_config",
            "record_coach_fact",
            "运动员档案（A）",
            "本周期配置（B）",
        ):
            assert marker in text

    def test_includes_every_slot_label_and_both_examples(self):
        text = ci.render_intake_prompt_section()
        for s in (*ci.PROFILE_SLOTS, *ci.CYCLE_SLOTS):
            assert s.label in text
            assert s.good_example in text
            assert s.vague_example in text
        # the ✅/❌ standard is what teaches "specific enough"
        assert "✅" in text and "❌" in text


# ==========================================================================
# B. get_coach_profile / get_cycle_config — hard coverage (pure SQL)
# ==========================================================================
class TestCoverage:
    def test_empty_db_all_gaps(self, mem):
        prof = mem.get_coach_profile()
        assert prof["total"] == 8
        assert prof["filled_count"] == 0
        assert len(prof["gaps"]) == 8
        assert len(prof["areas"]) == 8
        # ordered by the canonical importance list — injury first
        assert prof["areas"][0]["area"] == "Profile.injury_history"

        cyc = mem.get_cycle_config()
        assert cyc["total"] == 11
        assert cyc["filled_count"] == 0
        assert cyc["areas"][0]["area"] == "Cycle.goal"

    def test_topic_with_conclusion_fills_area(self, mem):
        mem.create_topic(
            name="ITB 2024",
            root_category="Profile.injury_history",
            status="Resolved",
            working_conclusion="2024 髂胫束综合征，已痊愈",
        )
        prof = mem.get_coach_profile()
        assert prof["filled_count"] == 1
        injury = next(a for a in prof["areas"] if a["area"] == "Profile.injury_history")
        assert injury["filled"] is True
        assert injury["conclusion"] == "2024 髂胫束综合征，已痊愈"
        assert injury["updated_at"]
        assert "Profile.injury_history" not in {g["area"] for g in prof["gaps"]}

    def test_blank_conclusion_is_still_a_gap(self, mem):
        # A topic exists in the area but its conclusion is NULL → not covered.
        mem.create_topic(
            name="injury placeholder",
            root_category="Profile.injury_history",
            status="Open",
            working_conclusion=None,
        )
        prof = mem.get_coach_profile()
        assert prof["filled_count"] == 0
        injury = next(a for a in prof["areas"] if a["area"] == "Profile.injury_history")
        assert injury["filled"] is False
        assert injury["conclusion"] is None

    def test_multiple_topics_most_recent_conclusion_wins(self, mem):
        t1 = mem.create_topic(
            name="left foot",
            root_category="Profile.injury_history",
            status="Resolved",
            working_conclusion="左足底筋膜炎",
        )
        t2 = mem.create_topic(
            name="right achilles",
            root_category="Profile.injury_history",
            status="Resolved",
            working_conclusion="右跟腱炎",
        )
        # bump t2's updated_at so it's unambiguously the most recent
        mem.update_topic(t2, working_conclusion="右跟腱炎（最新）")
        injury = next(
            a for a in mem.get_coach_profile()["areas"]
            if a["area"] == "Profile.injury_history"
        )
        assert injury["filled"] is True
        assert injury["conclusion"] == "右跟腱炎（最新）"
        assert set(injury["topic_ids"]) == {t1, t2}

    def test_unrelated_topic_does_not_fill_any_area(self, mem):
        mem.create_topic(
            name="rainy day pref",
            root_category="Running/Preferences",
            status="Resolved",
            working_conclusion="下雨天也愿意跑",
        )
        assert mem.get_coach_profile()["filled_count"] == 0
        assert mem.get_cycle_config()["filled_count"] == 0


# ==========================================================================
# C. record_coach_fact — branches + lossless storage
# ==========================================================================
class TestRecordCoachFact:
    def test_unknown_area_raises(self, mem):
        with pytest.raises(ValueError):
            mem.record_coach_fact("Bogus.area", "x")

    def test_empty_raw_text_raises(self, mem):
        with pytest.raises(ValueError):
            mem.record_coach_fact("Profile.injury_history", "   ")

    def test_create_branch_new_topic_when_no_match(self, mem):
        with _patch_match(mem, None, []):
            res = mem.record_coach_fact(
                "Cycle.goal", "Berlin 2026-09-21, sub-3:30, 硬目标"
            )
        assert res["action"] == "created"
        assert res["topic_id"]
        # area now covered, conclusion defaults to raw_text
        cyc = mem.get_cycle_config()
        goal = next(a for a in cyc["areas"] if a["area"] == "Cycle.goal")
        assert goal["filled"] is True
        assert goal["conclusion"] == "Berlin 2026-09-21, sub-3:30, 硬目标"
        # lossless episode linked + raw text preserved verbatim
        eps = mem.get_topic_episodes(res["topic_id"])
        assert len(eps) == 1
        assert eps[0]["event_type"] == "cycle_config"
        assert eps[0]["context"]["raw_text"] == "Berlin 2026-09-21, sub-3:30, 硬目标"
        assert eps[0]["context"]["area"] == "Cycle.goal"

    def test_low_score_creates_new_even_with_candidates(self, mem):
        # candidate exists but below LOW → distinct fact, new topic
        cand = [{"topic_id": "tpc_other", "name": "x", "status": "Resolved", "score": 0.40}]
        with _patch_match(mem, None, cand):
            res = mem.record_coach_fact("Profile.devices", "Garmin 965 + HRM-Pro")
        assert res["action"] == "created"
        assert res["score"] == 0.40

    def test_update_branch_when_auto_match(self, mem):
        existing = mem.create_topic(
            name="goal",
            root_category="Cycle.goal",
            status="Resolved",
            working_conclusion="旧目标",
        )
        cand = [{"topic_id": existing, "name": "goal", "status": "Resolved", "score": 0.93}]
        with _patch_match(mem, existing, cand):
            res = mem.record_coach_fact("Cycle.goal", "改成 Chicago 2026, sub-3:25")
        assert res["action"] == "updated"
        assert res["topic_id"] == existing
        # conclusion rewritten in place, no new topic row
        assert mem.get_topic(existing)["working_conclusion"] == "改成 Chicago 2026, sub-3:25"
        assert len([t for t in mem.list_topics() if t["root_category"] == "Cycle.goal"]) == 1
        # episode linked to the existing topic
        assert len(mem.get_topic_episodes(existing)) == 1

    def test_park_branch_when_ambiguous(self, mem):
        existing = mem.create_topic(
            name="injury old",
            root_category="Profile.injury_history",
            status="Resolved",
            working_conclusion="2023 应力性骨折",
        )
        cand = [{"topic_id": existing, "name": "injury old", "status": "Resolved", "score": 0.70}]
        with _patch_match(mem, None, cand):
            res = mem.record_coach_fact(
                "Profile.injury_history", "最近右膝有点酸"
            )
        assert res["action"] == "parked"
        assert res["decision_id"]
        # parked, so the area is NOT auto-filled yet
        injury = next(
            a for a in mem.get_coach_profile()["areas"]
            if a["area"] == "Profile.injury_history"
        )
        assert injury["conclusion"] == "2023 应力性骨折"  # unchanged
        # the decision is on the queue and carries the lossless episode
        pend = mem.list_pending_decisions()
        assert len(pend) == 1
        assert pend[0]["proposal"]["episode_id"] == res["episode_id"]
        assert pend[0]["proposal"]["raw_text"] == "最近右膝有点酸"

    def test_conclusion_distinct_from_raw_text(self, mem):
        with _patch_match(mem, None, []):
            res = mem.record_coach_fact(
                "Profile.background",
                raw_text="我跑了大概五年吧，跑过两个全马，最好 3 小时 40",
                conclusion="训练年龄 5 年，2 个全马，PB 3:40",
            )
        topic = mem.get_topic(res["topic_id"])
        assert topic["working_conclusion"] == "训练年龄 5 年，2 个全马，PB 3:40"
        # but the episode keeps the verbatim raw text
        eps = mem.get_topic_episodes(res["topic_id"])
        assert eps[0]["context"]["raw_text"] == "我跑了大概五年吧，跑过两个全马，最好 3 小时 40"

    def test_custom_name_used_when_provided(self, mem):
        with _patch_match(mem, None, []):
            res = mem.record_coach_fact(
                "Profile.medical", "哮喘，吸入剂", name="哮喘史"
            )
        assert mem.get_topic(res["topic_id"])["name"] == "哮喘史"


# ==========================================================================
# D. find_matching_topic root_category scoping
# ==========================================================================
def _stub_embeddings(mapping: dict[str, list[float]]):
    def fake_call_embedding(texts, provider="gemini"):
        out = []
        for t in texts:
            vec = [0.01] * 8
            for needle, v in mapping.items():
                if needle in t:
                    vec = list(v)
                    break
            out.append(vec)
        return out

    return fake_call_embedding


class TestAreaScopedMatch:
    def test_root_category_filter_excludes_other_areas(self, mem):
        # A topic in a DIFFERENT area that would otherwise score high.
        mem.create_topic(
            name="同一个向量",
            root_category="Cycle.goal",
            status="Resolved",
            working_conclusion="同一个向量",
        )
        stub = _stub_embeddings({"同一个向量": [1.0, 0, 0, 0, 0, 0, 0, 0]})
        with patch("backend.cognitive_memory_engine.call_embedding", side_effect=stub):
            # query identical to the goal topic, but scoped to injury_history
            auto, cands = mem.find_matching_topic(
                "同一个向量", root_category="Profile.injury_history"
            )
        assert auto is None
        assert cands == []  # the goal topic was filtered out by category

    def test_record_coach_fact_does_not_cross_area(self, mem):
        # Real path (no patched matcher): an identical-text topic in goal must
        # not be matched when recording into injury_history.
        mem.create_topic(
            name="重复文本",
            root_category="Cycle.goal",
            status="Resolved",
            working_conclusion="重复文本",
        )
        stub = _stub_embeddings({"重复文本": [1.0, 0, 0, 0, 0, 0, 0, 0]})
        with patch("backend.cognitive_memory_engine.call_embedding", side_effect=stub):
            res = mem.record_coach_fact("Profile.injury_history", "重复文本")
        assert res["action"] == "created"  # new topic in injury_history, no cross-merge
        assert mem.get_topic(res["topic_id"])["root_category"] == "Profile.injury_history"


# ==========================================================================
# E. resolve_topic_decision links the parked coach-fact episode
# ==========================================================================
class TestResolveLinksEpisode:
    def _park(self, mem):
        existing = mem.create_topic(
            name="injury old",
            root_category="Profile.injury_history",
            status="Resolved",
            working_conclusion="2023 应力性骨折",
        )
        cand = [{"topic_id": existing, "name": "injury old", "status": "Resolved", "score": 0.70}]
        with _patch_match(mem, None, cand):
            res = mem.record_coach_fact("Profile.injury_history", "右膝酸痛")
        return existing, res

    def test_merge_links_episode_to_target(self, mem):
        existing, res = self._park(mem)
        dec = mem.list_pending_decisions()[0]
        mem.resolve_topic_decision(dec["decision_id"], "merge", target_topic_id=existing)
        ep_ids = {e["episode_id"] for e in mem.get_topic_episodes(existing)}
        assert res["episode_id"] in ep_ids
        # conclusion folded in from the proposal
        assert mem.get_topic(existing)["working_conclusion"] == "右膝酸痛"

    def test_create_new_links_episode_to_new_topic(self, mem):
        _existing, res = self._park(mem)
        dec = mem.list_pending_decisions()[0]
        new_tid = mem.resolve_topic_decision(dec["decision_id"], "create_new")
        assert new_tid
        ep_ids = {e["episode_id"] for e in mem.get_topic_episodes(new_tid)}
        assert res["episode_id"] in ep_ids
        # the new topic fills the area
        injury = next(
            a for a in mem.get_coach_profile()["areas"]
            if a["area"] == "Profile.injury_history"
        )
        assert new_tid in injury["topic_ids"]
