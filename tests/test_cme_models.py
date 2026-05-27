"""Tests for the PR P1 pattern-store schema + CRUD on MemoryOS.

Three classes of test:
1. Migration safety — the schema migrations (topic_decisions CHECK
   extension, topics.related_models ALTER, models CREATE) are
   idempotent and don't damage existing rows.
2. CRUD round-trips — create_model / get_model / list_models /
   update_model_params / link_topic_to_model do what they say.
3. Validation — bad enums (model_type, status, etc.) raise; UNIQUE
   on model_key fires; missing model returns None / False.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.cognitive_memory_engine import MemoryOS


@pytest.fixture
def mem(tmp_cme_db, tmp_path):
    """Fresh MemoryOS pointed at a per-test cognition.db."""
    profile = tmp_path / "profile.json"
    return MemoryOS(db_path=tmp_cme_db, semantic_profile_path=str(profile))


# ---------------------------------------------------------------------------
# Migration safety
# ---------------------------------------------------------------------------


class TestSchemaMigrations:
    def test_fresh_db_has_models_table_and_related_models_column(self, mem):
        # models table exists with all the canonical columns
        cols = {r["name"] for r in mem.conn.execute("PRAGMA table_info(models)")}
        expected = {
            "model_id", "model_key", "name", "category", "model_type",
            "params_json", "n_samples", "confidence", "evidence_json",
            "derivation_method", "status", "created_at", "updated_at",
            "last_verified_at",
        }
        assert expected.issubset(cols)

        # topics gets the related_models column
        topic_cols = {r["name"] for r in mem.conn.execute("PRAGMA table_info(topics)")}
        assert "related_models" in topic_cols

        # topic_decisions.kind CHECK includes 'new_model'
        sql = mem.conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='topic_decisions'"
        ).fetchone()["sql"]
        assert "new_model" in sql

    def test_running_migrations_twice_is_idempotent(self, tmp_cme_db, tmp_path):
        """Construction implicitly migrates. A second construction on
        the same file must not re-run destructive table rebuilds."""
        profile = str(tmp_path / "profile.json")
        m1 = MemoryOS(db_path=tmp_cme_db, semantic_profile_path=profile)
        m1.create_model(
            model_key="x.test",
            name="test",
            category="Test",
            model_type="mean_std",
            params_json={"mean": 1.0, "sd": 0.1},
            derivation_method="stat",
        )
        m1.conn.close()

        # Second pass on same DB
        m2 = MemoryOS(db_path=tmp_cme_db, semantic_profile_path=profile)
        # Existing row survived
        assert m2.get_model("x.test") is not None
        # Schema still has the expected fields (didn't get re-dropped)
        cols = {r["name"] for r in m2.conn.execute("PRAGMA table_info(models)")}
        assert "params_json" in cols

    def test_legacy_topic_decisions_check_extended(self, tmp_cme_db, tmp_path):
        """Simulate a pre-P1 DB: topic_decisions CHECK doesn't include
        'new_model'. Migration should rebuild the CHECK and preserve
        existing rows."""
        # Build the legacy schema by hand
        con = sqlite3.connect(tmp_cme_db)
        con.executescript(
            """
            CREATE TABLE topic_decisions (
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
            INSERT INTO topic_decisions (decision_id, kind, proposal_json, created_at)
                VALUES ('did_legacy', 'new_topic', '{}', '2026-01-01T00:00:00Z');
            """
        )
        con.commit()
        con.close()

        # Migration runs in __init__
        m = MemoryOS(db_path=tmp_cme_db, semantic_profile_path=str(tmp_path / "p.json"))

        # Legacy row survived
        row = m.conn.execute(
            "SELECT * FROM topic_decisions WHERE decision_id='did_legacy'"
        ).fetchone()
        assert row is not None
        assert row["kind"] == "new_topic"

        # CHECK now accepts 'new_model'
        m.conn.execute(
            """INSERT INTO topic_decisions
               (decision_id, kind, proposal_json, created_at)
               VALUES (?, ?, ?, ?)""",
            ("did_new", "new_model", "{}", "2026-05-27T00:00:00Z"),
        )
        m.conn.commit()
        assert m.conn.execute(
            "SELECT kind FROM topic_decisions WHERE decision_id='did_new'"
        ).fetchone()["kind"] == "new_model"

    def test_legacy_topics_table_gets_related_models_column(self, tmp_cme_db, tmp_path):
        """Pre-P1 topics table lacks related_models. ALTER ADD COLUMN
        runs idempotently and preserves existing data."""
        con = sqlite3.connect(tmp_cme_db)
        con.executescript(
            """
            CREATE TABLE topics (
                topic_id          TEXT PRIMARY KEY,
                root_category     TEXT NOT NULL,
                name              TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'Open',
                working_conclusion TEXT,
                related_episodes  TEXT DEFAULT '[]',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            );
            INSERT INTO topics
                (topic_id, root_category, name, status, related_episodes, created_at, updated_at)
                VALUES ('tpc_legacy', 'Health', 'legacy topic', 'Open', '[]',
                        '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z');
            """
        )
        con.commit()
        con.close()

        m = MemoryOS(db_path=tmp_cme_db, semantic_profile_path=str(tmp_path / "p.json"))
        cols = {r["name"] for r in m.conn.execute("PRAGMA table_info(topics)")}
        assert "related_models" in cols
        # Existing row survived AND the new column defaults to '[]'
        legacy = m.get_topic("tpc_legacy")
        assert legacy is not None
        assert legacy["related_models"] == []


# ---------------------------------------------------------------------------
# CRUD round-trips
# ---------------------------------------------------------------------------


class TestModelCRUD:
    def _make_model_args(self, **overrides) -> dict:
        defaults = dict(
            model_key="recovery.hrv_curve_post_long_run",
            name="长跑后 HRV 恢复曲线",
            category="Health/Recovery",
            model_type="decay",
            params_json={
                "peak_drop_day": 2, "peak_drop_pct": -8.2,
                "return_to_baseline_day": 4,
            },
            n_samples=7,
            confidence="medium",
            evidence_json={"activities": [22833575003], "dates": ["2026-04-29"]},
            derivation_method="stat",
            status="Forming",
        )
        defaults.update(overrides)
        return defaults

    def test_create_get_round_trip(self, mem):
        args = self._make_model_args()
        mid = mem.create_model(**args)
        assert mid.startswith("mdl_")

        got = mem.get_model(args["model_key"])
        assert got is not None
        assert got["model_id"] == mid
        assert got["model_key"] == args["model_key"]
        assert got["name"] == args["name"]
        assert got["params_json"] == args["params_json"]  # deserialized
        assert got["evidence_json"] == args["evidence_json"]
        assert got["n_samples"] == 7
        assert got["confidence"] == "medium"
        assert got["status"] == "Forming"
        # Timestamps populated by helper.
        assert got["created_at"]
        assert got["updated_at"]
        assert got["last_verified_at"]

    def test_get_missing_returns_none(self, mem):
        assert mem.get_model("does.not.exist") is None

    def test_unique_model_key_enforced(self, mem):
        mem.create_model(**self._make_model_args())
        with pytest.raises(sqlite3.IntegrityError):
            mem.create_model(**self._make_model_args())  # same model_key

    def test_list_filters_by_category_prefix(self, mem):
        mem.create_model(**self._make_model_args(model_key="a.one", category="Health/Recovery"))
        mem.create_model(**self._make_model_args(model_key="a.two", category="Health/Sleep"))
        mem.create_model(**self._make_model_args(model_key="a.three", category="Running/Performance"))

        health = mem.list_models(category="Health")
        assert {m["model_key"] for m in health} == {"a.one", "a.two"}

        running = mem.list_models(category="Running")
        assert {m["model_key"] for m in running} == {"a.three"}

    def test_list_filters_by_status(self, mem):
        mem.create_model(**self._make_model_args(model_key="a.one", status="Forming"))
        mem.create_model(**self._make_model_args(model_key="a.two", status="Stable"))

        stable = mem.list_models(status="Stable")
        assert [m["model_key"] for m in stable] == ["a.two"]

    def test_update_params_changes_fields_and_bumps_verified(self, mem):
        mem.create_model(**self._make_model_args())
        before = mem.get_model("recovery.hrv_curve_post_long_run")

        ok = mem.update_model_params(
            "recovery.hrv_curve_post_long_run",
            params_json={"peak_drop_day": 3, "peak_drop_pct": -9.5},
            n_samples=10,
            confidence="high",
            status="Stable",
        )
        assert ok is True

        after = mem.get_model("recovery.hrv_curve_post_long_run")
        assert after["params_json"] == {"peak_drop_day": 3, "peak_drop_pct": -9.5}
        assert after["n_samples"] == 10
        assert after["confidence"] == "high"
        assert after["status"] == "Stable"
        # updated_at and last_verified_at both bumped
        assert after["updated_at"] > before["updated_at"]
        assert after["last_verified_at"] > before["last_verified_at"]
        # model_id stable (no row recreation)
        assert after["model_id"] == before["model_id"]

    def test_update_missing_returns_false(self, mem):
        assert mem.update_model_params("not.exist", n_samples=99) is False

    def test_link_topic_to_model_idempotent(self, mem):
        mid = mem.create_model(**self._make_model_args())
        tid = mem.create_topic(name="t", root_category="Health/Recovery")

        assert mem.link_topic_to_model(tid, mid) is True
        assert mem.get_topic(tid)["related_models"] == [mid]

        # Second link is a noop, NOT a duplicate
        assert mem.link_topic_to_model(tid, mid) is True
        assert mem.get_topic(tid)["related_models"] == [mid]

    def test_link_to_missing_topic_returns_false(self, mem):
        mid = mem.create_model(**self._make_model_args())
        assert mem.link_topic_to_model("tpc_does_not_exist", mid) is False


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestModelValidation:
    def _args(self, **overrides):
        defaults = dict(
            model_key="x.test",
            name="t",
            category="Test",
            model_type="mean_std",
            params_json={"mean": 1.0, "sd": 0.1},
            derivation_method="stat",
        )
        defaults.update(overrides)
        return defaults

    def test_bad_model_type_rejected(self, mem):
        with pytest.raises(ValueError, match="model_type"):
            mem.create_model(**self._args(model_type="nonsense"))

    def test_bad_status_rejected(self, mem):
        with pytest.raises(ValueError, match="status"):
            mem.create_model(**self._args(status="Mythical"))

    def test_bad_derivation_method_rejected(self, mem):
        with pytest.raises(ValueError, match="derivation_method"):
            mem.create_model(**self._args(derivation_method="vibes"))

    def test_bad_confidence_rejected(self, mem):
        with pytest.raises(ValueError, match="confidence"):
            mem.create_model(**self._args(confidence="vibes"))

    def test_non_dict_params_rejected(self, mem):
        with pytest.raises(ValueError, match="params_json"):
            mem.create_model(**self._args(params_json="not a dict"))

    def test_update_bad_status_rejected(self, mem):
        mem.create_model(**self._args())
        with pytest.raises(ValueError, match="status"):
            mem.update_model_params("x.test", status="Bogus")


# ---------------------------------------------------------------------------
# Seed model (recovery.hrv_14d_baseline) — refit logic
# ---------------------------------------------------------------------------


class TestSeedModel:
    """The first stat-derived model. Exercises the refit-or-create
    pattern P6 will follow for the rest of the batch."""

    def test_skips_when_insufficient_data(self, mem):
        from unittest.mock import MagicMock

        from backend.seed_models import refit_hrv_14d_baseline

        dp = MagicMock()
        # 5 rows, all with HRV — below the 7-day floor
        dp.get_health_stats.return_value = [
            {"date": f"2026-05-{i:02d}", "hrv": 70.0 + i, "rhr": 50,
             "sleep_score": 80, "sleep_hours": 7.0, "stress": 20,
             "run_miles": 0, "run_mins": 0}
            for i in range(20, 25)
        ]
        assert refit_hrv_14d_baseline(mem, dp) is None
        assert mem.get_model("recovery.hrv_14d_baseline") is None

    def test_creates_model_with_correct_shape(self, mem):
        from unittest.mock import MagicMock

        from backend.seed_models import refit_hrv_14d_baseline

        dp = MagicMock()
        # 14 rows, 12 non-null HRV → Stable, medium confidence
        rows = []
        for i in range(1, 15):
            hrv = None if i in (3, 7) else 70.0 + (i % 5)  # 2 nulls, sd of remaining
            rows.append({
                "date": f"2026-05-{i:02d}", "hrv": hrv, "rhr": 50,
                "sleep_score": 80, "sleep_hours": 7.0, "stress": 20,
                "run_miles": 0, "run_mins": 0,
            })
        dp.get_health_stats.return_value = rows

        key = refit_hrv_14d_baseline(mem, dp)
        assert key == "recovery.hrv_14d_baseline"

        got = mem.get_model(key)
        assert got["model_type"] == "mean_std"
        assert got["derivation_method"] == "stat"
        assert got["status"] == "Stable"      # n_used == 12 ≥ 10
        assert got["confidence"] == "medium"  # 10 <= 12 < 13
        assert got["n_samples"] == 12
        # params shape
        params = got["params_json"]
        assert set(params.keys()) == {
            "mean", "sd", "window_days", "n_used",
            "low_warning", "high_warning",
        }
        # low/high warnings bracket the mean by ~2 SD. Exact value
        # comparison is brittle (the stored mean/sd are rounded but
        # low_warning is computed from un-rounded inputs), so check
        # the bracket relationship instead.
        assert params["low_warning"] < params["mean"] < params["high_warning"]
        # Roughly 2 SD on each side, with rounding slack.
        assert abs((params["mean"] - params["low_warning"]) - 2 * params["sd"]) <= 0.2
        assert abs((params["high_warning"] - params["mean"]) - 2 * params["sd"]) <= 0.2

    def test_second_call_updates_in_place(self, mem):
        from unittest.mock import MagicMock

        from backend.seed_models import refit_hrv_14d_baseline

        dp = MagicMock()
        # First call: 8 rows
        dp.get_health_stats.return_value = [
            {"date": f"2026-05-{i:02d}", "hrv": 70.0 + i, "rhr": 50,
             "sleep_score": 80, "sleep_hours": 7.0, "stress": 20,
             "run_miles": 0, "run_mins": 0}
            for i in range(1, 9)
        ]
        refit_hrv_14d_baseline(mem, dp)
        first = mem.get_model("recovery.hrv_14d_baseline")
        assert first["status"] == "Forming"  # 8 < 10

        # Second call: more data → status flips to Stable
        dp.get_health_stats.return_value = [
            {"date": f"2026-05-{i:02d}", "hrv": 70.0 + i, "rhr": 50,
             "sleep_score": 80, "sleep_hours": 7.0, "stress": 20,
             "run_miles": 0, "run_mins": 0}
            for i in range(1, 15)
        ]
        refit_hrv_14d_baseline(mem, dp)
        second = mem.get_model("recovery.hrv_14d_baseline")
        assert second["model_id"] == first["model_id"]   # update, not new row
        assert second["status"] == "Stable"
