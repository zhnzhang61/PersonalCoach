"""Coaching-tips store (PR #115) — CRUD + the concurrent-write fix.

The store is the same JSON-list-in-manual_inputs pattern as planned
workouts, so basic CRUD mirrors test_planned_workouts.py. The extra
concern here is the Codex P2 from #115: FastAPI runs sync handlers in
a threadpool, so concurrent add/delete calls interleaving their
load→modify→dump used to silently drop writes. The threaded test
below is deterministic-pass WITH the lock; without it, it flaked red
on most runs (lost tips).
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.data_processor import DataProcessor


@pytest.fixture
def dp(tmp_path):
    return DataProcessor(data_dir=str(tmp_path))


def test_add_returns_row_with_defaults(dp):
    tip = dp.add_coaching_tip(title="  T  ", body="B", topic=None)
    assert tip["id"].startswith("tip_")
    assert tip["title"] == "T"
    assert tip["topic"] is None
    assert len(tip["date"]) == 10  # defaulted to today


def test_list_newest_first(dp):
    dp.add_coaching_tip(title="old", body="b", date="2026-01-01")
    dp.add_coaching_tip(title="new", body="b", date="2026-07-01")
    titles = [t["title"] for t in dp.list_coaching_tips()]
    assert titles == ["new", "old"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "", "body": "b"},
        {"title": "t", "body": "   "},
        {"title": "t", "body": "b", "date": "07/19/2026"},
    ],
)
def test_add_rejects_bad_input(dp, kwargs):
    with pytest.raises(ValueError):
        dp.add_coaching_tip(**kwargs)


def test_delete_roundtrip(dp):
    tip = dp.add_coaching_tip(title="t", body="b")
    assert dp.delete_coaching_tip(tip["id"]) is True
    assert dp.delete_coaching_tip(tip["id"]) is False
    assert dp.list_coaching_tips() == []


def test_concurrent_adds_lose_nothing(dp):
    n = 40
    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(
            lambda i: dp.add_coaching_tip(title=f"t{i}", body=f"b{i}"),
            range(n),
        ))
    assert len(dp.list_coaching_tips()) == n


def test_concurrent_adds_and_deletes_stay_consistent(dp):
    keep = [dp.add_coaching_tip(title=f"k{i}", body="b") for i in range(10)]
    doomed = [dp.add_coaching_tip(title=f"d{i}", body="b") for i in range(10)]

    def work(i):
        if i < 10:
            dp.add_coaching_tip(title=f"n{i}", body="b")
        else:
            assert dp.delete_coaching_tip(doomed[i - 10]["id"]) is True

    with ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(work, range(20)))

    ids = {t["id"] for t in dp.list_coaching_tips()}
    assert len(ids) == 20  # 10 kept + 10 new; all deletes landed
    assert {t["id"] for t in keep} <= ids
    assert ids.isdisjoint({t["id"] for t in doomed})
