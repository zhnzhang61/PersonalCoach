"""
v4_link_episodes.py — interactive backfill for orphan episodes.

Walks every episode in cognition.db that has no row in topic_episode_links,
prints its content + a numbered list of all topics, and asks the user which
topics (if any) it belongs to. Writes the junction rows.

Usage:
    python -m migrations.v4_link_episodes [--db path/to/cognition.db]
    python -m migrations.v4_link_episodes --dry-run

Answer format for each episode:
    - Comma-separated topic numbers (e.g. "1,3,7") to link
    - "s" or empty to skip (leave unlinked for this run)
    - "q" to quit (commits what you've already answered)

Idempotent: running twice only re-prompts for episodes that are still orphans.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _fetch_orphan_episodes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.*
        FROM episodes e
        LEFT JOIN topic_episode_links l ON l.episode_id = e.episode_id
        WHERE l.episode_id IS NULL
        ORDER BY COALESCE(e.event_timestamp, e.timestamp) DESC
        """
    ).fetchall()


def _fetch_all_topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT topic_id, name, status, root_category FROM topics ORDER BY created_at"
    ).fetchall()


def _render_episode(ep: sqlite3.Row) -> str:
    ctx = json.loads(ep["context_json"] or "{}")
    lines = [
        f"episode_id: {ep['episode_id']}",
        f"event_type: {ep['event_type']}",
        f"timestamp:  {ep['event_timestamp'] or ep['timestamp']}",
        f"what:       {ctx.get('what', '')}",
    ]
    if ep["lesson_learned"]:
        lines.append(f"lesson:     {ep['lesson_learned']}")
    return "\n".join(lines)


def _render_topics(topics: list[sqlite3.Row]) -> str:
    return "\n".join(
        f"  [{i}] {t['topic_id']}  {t['name']} ({t['status']}) — {t['root_category']}"
        for i, t in enumerate(topics, 1)
    )


def _parse_answer(answer: str, n_topics: int) -> tuple[str, list[int]]:
    """Return (action, indices). action ∈ {'link', 'skip', 'quit'}."""
    a = answer.strip().lower()
    if not a or a == "s":
        return ("skip", [])
    if a == "q":
        return ("quit", [])
    try:
        nums = [int(x.strip()) for x in a.split(",") if x.strip()]
    except ValueError:
        return ("skip", [])
    valid = [n for n in nums if 1 <= n <= n_topics]
    return ("link", valid)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/cognition.db")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    orphans = _fetch_orphan_episodes(conn)
    topics = _fetch_all_topics(conn)

    if not orphans:
        print("No orphan episodes. Nothing to do.")
        return 0
    if not topics:
        print("No topics in DB; cannot link. Aborting.")
        return 1

    print(f"Found {len(orphans)} orphan episode(s) and {len(topics)} topic(s).\n")
    print("Available topics:")
    print(_render_topics(topics))
    print()

    from cognitive_memory_engine import MemoryOS

    mem = MemoryOS(db_path=args.db)

    linked = skipped = 0
    for i, ep in enumerate(orphans, 1):
        print(f"\n--- [{i}/{len(orphans)}] ---")
        print(_render_episode(ep))
        print()
        answer = input(
            f"Link to which topic(s)? "
            f"(1-{len(topics)} comma-separated / s=skip / q=quit): "
        )
        action, nums = _parse_answer(answer, len(topics))

        if action == "quit":
            print("Quitting.")
            break
        if action == "skip" or not nums:
            skipped += 1
            continue

        picked_tids = [topics[n - 1]["topic_id"] for n in nums]
        if args.dry_run:
            print(f"  [dry-run] would link {ep['episode_id']} → {picked_tids}")
        else:
            for tid in picked_tids:
                mem.add_topic_episode_link(tid, ep["episode_id"])
            print(f"  ✓ linked {ep['episode_id']} → {picked_tids}")
        linked += 1

    print(f"\nDone. linked={linked}  skipped={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
