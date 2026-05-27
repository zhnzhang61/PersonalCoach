"""Tests for the PR P2 model proposal pipeline.

Three classes:
1. `propose_model_from_topic` — handles the four flow branches:
   topic missing, too few episodes, LLM declines, LLM proposes.
2. `resolve_topic_decision` with `kind='new_model'` — confirm path
   creates a model + links to source topic; reject path is a no-op.
3. `park_topic_decision` validation now allows `'new_model'` kind.

LLM is mocked at `MemoryOS._llm_invoke` — same pattern existing CME
tests use to keep the unit-under-test the orchestration, not the LLM.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.cognitive_memory_engine import MemoryOS


@pytest.fixture
def mem(tmp_cme_db, tmp_path):
    return MemoryOS(
        db_path=tmp_cme_db,
        semantic_profile_path=str(tmp_path / "profile.json"),
    )


def _topic_with_episodes(mem: MemoryOS, n_episodes: int) -> str:
    """Create a topic + N episodes linked to it. Returns topic_id."""
    tid = mem.create_topic(
        name="HRV 长跑后恢复",
        root_category="Health/Recovery",
        status="Testing",
        working_conclusion="长跑后 HRV 通常 2 天回升",
    )
    for i in range(n_episodes):
        eid = mem.create_episode(
            event_type="long_run_recovery",
            context={
                "what": f"5/{i+1} long run aftermath",
                "where": "Weehawken",
                "emotion": "tired",
                "details": f"HRV dropped {i+5}% the day after",
            },
            lesson_learned=f"day-{i+2} nadir was {65+i}ms",
        )
        mem.add_topic_episode_link(tid, eid)
    return tid


# ---------------------------------------------------------------------------
# propose_model_from_topic
# ---------------------------------------------------------------------------


class TestProposeModelFromTopic:
    def test_topic_missing_returns_skipped(self, mem):
        result = mem.propose_model_from_topic("tpc_does_not_exist")
        assert result == {"status": "skipped", "reason": "topic_missing"}

    def test_too_few_episodes_returns_skipped(self, mem):
        # 2 episodes < MIN_EPISODES_TO_PROPOSE (3)
        tid = _topic_with_episodes(mem, n_episodes=2)
        result = mem.propose_model_from_topic(tid)
        assert result["status"] == "skipped"
        assert result["reason"] == "too_few_episodes"
        assert result["n_episodes"] == 2
        assert result["min_required"] == 3

    def test_llm_declines_returns_skipped_with_reason(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=4)
        decline_payload = json.dumps({
            "propose": False,
            "reason": "episodes describe distinct injuries, not a curve",
        }, ensure_ascii=False)
        with patch.object(mem, "_llm_invoke", return_value=decline_payload):
            result = mem.propose_model_from_topic(tid)
        assert result["status"] == "skipped"
        assert "distinct injuries" in result["reason"]
        # Nothing parked when LLM declines.
        assert mem.list_pending_decisions() == []

    def test_llm_proposes_parks_decision(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=5)
        propose_payload = json.dumps({
            "propose": True,
            "model_key": "recovery.hrv_curve_post_long_run",
            "name": "长跑后 HRV 恢复曲线",
            "category": "Health/Recovery",
            "model_type": "decay",
            "params": {
                "peak_drop_day": 2,
                "peak_drop_pct": -8.2,
                "return_to_baseline_day": 4,
            },
            "n_samples": 5,
            "confidence": "medium",
            "rationale": "5 events all show day-2 nadir pattern",
        }, ensure_ascii=False)
        with patch.object(mem, "_llm_invoke", return_value=propose_payload):
            result = mem.propose_model_from_topic(tid)

        assert result["status"] == "parked"
        assert result["decision_id"].startswith("dec_")
        prop = result["proposal"]
        # Wrapped with topic_id + trigger for resolve to use
        assert prop["topic_id"] == tid
        assert prop["trigger"] == "manual"
        # LLM fields preserved verbatim
        assert prop["model_key"] == "recovery.hrv_curve_post_long_run"
        assert prop["model_type"] == "decay"
        assert prop["params"]["peak_drop_day"] == 2

        # Queue now has exactly one pending decision with kind='new_model'
        pending = mem.list_pending_decisions()
        assert len(pending) == 1
        assert pending[0]["kind"] == "new_model"

    def test_trigger_label_recorded(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=3)
        propose_payload = json.dumps({
            "propose": True,
            "model_key": "x.test",
            "name": "test",
            "category": "Test",
            "model_type": "fixed_obs",
            "params": {},
            "n_samples": 3,
        }, ensure_ascii=False)
        with patch.object(mem, "_llm_invoke", return_value=propose_payload):
            result = mem.propose_model_from_topic(tid, trigger="cron")
        assert result["proposal"]["trigger"] == "cron"

    def test_unparseable_llm_response_returns_llm_error(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=3)
        with patch.object(mem, "_llm_invoke", return_value="not json"):
            result = mem.propose_model_from_topic(tid)
        assert result["status"] == "llm_error"
        assert "JSON" in result["reason"]

    def test_markdown_fence_wrapped_json_still_parses(self, mem):
        """LLMs often wrap JSON in ```json fences. The strip helper
        should handle that transparently."""
        tid = _topic_with_episodes(mem, n_episodes=3)
        propose_payload = (
            "```json\n"
            + json.dumps({
                "propose": True, "model_key": "x.fenced", "name": "x",
                "category": "Test", "model_type": "fixed_obs",
                "params": {}, "n_samples": 3,
            }, ensure_ascii=False)
            + "\n```"
        )
        with patch.object(mem, "_llm_invoke", return_value=propose_payload):
            result = mem.propose_model_from_topic(tid)
        assert result["status"] == "parked"
        assert result["proposal"]["model_key"] == "x.fenced"

    def test_leading_prose_before_json_still_parses(self, mem):
        """LLM occasionally adds explanatory text before the JSON.
        Strip helper slices from first `{` to last `}` as fallback."""
        tid = _topic_with_episodes(mem, n_episodes=3)
        wrapped = (
            "Sure, here's my proposal based on the episodes:\n\n"
            + json.dumps({
                "propose": True, "model_key": "x.prosey", "name": "x",
                "category": "Test", "model_type": "fixed_obs",
                "params": {}, "n_samples": 3,
            }, ensure_ascii=False)
        )
        with patch.object(mem, "_llm_invoke", return_value=wrapped):
            result = mem.propose_model_from_topic(tid)
        assert result["status"] == "parked"
        assert result["proposal"]["model_key"] == "x.prosey"

    def test_missing_required_field_returns_llm_error(self, mem):
        """LLM says propose=true but forgets `params`. We must NOT park
        a half-baked proposal — fail loud with llm_error."""
        tid = _topic_with_episodes(mem, n_episodes=3)
        broken = json.dumps({
            "propose": True,
            "model_key": "x",
            "name": "x",
            "model_type": "decay",
            # missing: params
        })
        with patch.object(mem, "_llm_invoke", return_value=broken):
            result = mem.propose_model_from_topic(tid)
        assert result["status"] == "llm_error"
        assert "params" in result["reason"]
        # Nothing parked.
        assert mem.list_pending_decisions() == []

    def test_existing_models_included_in_prompt(self, mem):
        """The LLM gets a list of already-linked models so it can
        avoid proposing duplicates. Verify the prompt actually sees
        them."""
        tid = _topic_with_episodes(mem, n_episodes=3)
        # Add an existing model linked to this topic
        mid = mem.create_model(
            model_key="recovery.existing_curve",
            name="existing",
            category="Health/Recovery",
            model_type="decay",
            params_json={"x": 1},
            derivation_method="stat",
        )
        mem.link_topic_to_model(tid, mid)

        captured = {}

        def _capture(prompt):
            captured["prompt"] = prompt
            return json.dumps({"propose": False, "reason": "duplicate"})

        with patch.object(mem, "_llm_invoke", side_effect=_capture):
            mem.propose_model_from_topic(tid)

        # The existing model_key should appear in the prompt
        assert "recovery.existing_curve" in captured["prompt"]


# ---------------------------------------------------------------------------
# resolve_topic_decision with kind='new_model'
# ---------------------------------------------------------------------------


class TestResolveNewModelDecision:
    def _park_a_proposal(self, mem: MemoryOS, topic_id: str) -> str:
        """Park a representative new_model decision; return decision_id."""
        return mem.park_topic_decision(
            kind="new_model",
            proposal={
                "topic_id": topic_id,
                "trigger": "manual",
                "propose": True,
                "model_key": "recovery.hrv_curve_post_long_run",
                "name": "长跑后 HRV 恢复曲线",
                "category": "Health/Recovery",
                "model_type": "decay",
                "params": {"peak_drop_day": 2, "peak_drop_pct": -8.2},
                "n_samples": 5,
                "confidence": "medium",
                "rationale": "...",
            },
            candidates=[],
        )

    def test_create_new_creates_model_and_links_to_topic(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=5)
        did = self._park_a_proposal(mem, tid)

        result = mem.resolve_topic_decision(did, action="create_new")

        # Returns the model_id
        assert isinstance(result, str)
        assert result.startswith("mdl_")
        model_id = result

        # Model row exists with the right shape + llm derivation
        got = mem.get_model("recovery.hrv_curve_post_long_run")
        assert got is not None
        assert got["model_id"] == model_id
        assert got["model_type"] == "decay"
        assert got["params_json"] == {"peak_drop_day": 2, "peak_drop_pct": -8.2}
        assert got["derivation_method"] == "llm"
        assert got["status"] == "Forming"  # llm-derived starts Forming
        # Rationale + trigger captured in evidence_json
        assert got["evidence_json"]["trigger"] == "manual"
        assert got["evidence_json"]["proposal_rationale"] == "..."

        # Topic now points at the new model
        topic = mem.get_topic(tid)
        assert model_id in topic["related_models"]

        # Decision marked resolved with the right format
        row = mem.conn.execute(
            "SELECT status, resolution FROM topic_decisions WHERE decision_id = ?",
            (did,),
        ).fetchone()
        assert row["status"] == "created"
        assert row["resolution"] == f"created:{model_id}"

    def test_reject_marks_resolved_no_side_effects(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=3)
        did = self._park_a_proposal(mem, tid)

        result = mem.resolve_topic_decision(did, action="reject")
        assert result is None

        # No model created
        assert mem.get_model("recovery.hrv_curve_post_long_run") is None
        # Topic unchanged
        assert mem.get_topic(tid)["related_models"] == []
        # Decision marked rejected
        row = mem.conn.execute(
            "SELECT status, resolution FROM topic_decisions WHERE decision_id = ?",
            (did,),
        ).fetchone()
        assert row["status"] == "rejected"
        assert row["resolution"] == "rejected"

    def test_already_resolved_decision_returns_empty(self, mem):
        tid = _topic_with_episodes(mem, n_episodes=3)
        did = self._park_a_proposal(mem, tid)
        mem.resolve_topic_decision(did, action="reject")

        # Re-resolving the same decision: not found
        again = mem.resolve_topic_decision(did, action="create_new")
        assert again == ""

    def test_proposal_missing_topic_id_raises(self, mem):
        """A new_model proposal without topic_id (shouldn't happen via
        propose_model_from_topic but possible if a future caller
        forgets the wrap step) raises on resolve — better than silently
        creating an orphan model row."""
        did = mem.park_topic_decision(
            kind="new_model",
            proposal={
                # missing: topic_id
                "model_key": "x", "name": "x", "category": "X",
                "model_type": "fixed_obs", "params": {},
            },
            candidates=[],
        )
        with pytest.raises(ValueError, match="topic_id"):
            mem.resolve_topic_decision(did, action="create_new")

    def test_merge_action_rejected_for_new_model(self, mem):
        """Codex P2 catch on PR #78. The merge branch's prior
        `else: # conflict` fall-through corrupted target topics when
        callers passed action='merge' on a new_model decision (which
        the shared API enum allows). Verify the engine now refuses
        with a clear error AND leaves the target topic untouched."""
        tid = _topic_with_episodes(mem, n_episodes=3)
        did = self._park_a_proposal(mem, tid)

        # Capture pre-state of target topic
        before = mem.get_topic(tid)
        assert before["status"] == "Testing"  # _topic_with_episodes uses Testing

        with pytest.raises(ValueError, match="merge.*not supported.*new_model"):
            mem.resolve_topic_decision(did, action="merge", target_topic_id=tid)

        # Target topic untouched — NOT corrupted to Conflicting
        after = mem.get_topic(tid)
        assert after["status"] == "Testing"
        assert after["status"] != "Conflicting"
        # Decision still pending (not silently resolved as merged)
        row = mem.conn.execute(
            "SELECT status FROM topic_decisions WHERE decision_id = ?", (did,),
        ).fetchone()
        assert row["status"] == "pending"

    def test_create_new_rejected_for_episode_linking(self, mem):
        """Mirror catch on the create_new branch: a parked
        episode_linking decision should NOT silently fall through to
        the conflict-create path. Forces caller to use the 'link'
        action."""
        # Park a synthetic episode_linking decision
        eid = mem.create_episode(
            event_type="test_event",
            context={"what": "test"},
        )
        did = mem.park_topic_decision(
            kind="episode_linking",
            proposal={
                "episode_id": eid,
                "event_type": "test_event",
                "what": "test",
            },
            candidates=[],
        )
        with pytest.raises(
            ValueError, match="create_new.*not supported.*episode_linking"
        ):
            mem.resolve_topic_decision(did, action="create_new")

        # No phantom Conflicting topic created
        all_topics = mem.list_topics()
        assert all(t["status"] != "Conflicting" for t in all_topics)
        # Decision still pending
        row = mem.conn.execute(
            "SELECT status FROM topic_decisions WHERE decision_id = ?", (did,),
        ).fetchone()
        assert row["status"] == "pending"


# ---------------------------------------------------------------------------
# park_topic_decision now accepts 'new_model'
# ---------------------------------------------------------------------------


class TestParkValidation:
    def test_park_accepts_new_model(self, mem):
        did = mem.park_topic_decision(
            kind="new_model",
            proposal={"model_key": "x"},
            candidates=[],
        )
        assert did.startswith("dec_")
        # Verify the row stores kind='new_model'
        row = mem.conn.execute(
            "SELECT kind FROM topic_decisions WHERE decision_id = ?",
            (did,),
        ).fetchone()
        assert row["kind"] == "new_model"

    def test_park_still_rejects_unknown_kinds(self, mem):
        with pytest.raises(ValueError, match="Unknown decision kind"):
            mem.park_topic_decision(
                kind="not_a_real_kind",
                proposal={},
                candidates=[],
            )
