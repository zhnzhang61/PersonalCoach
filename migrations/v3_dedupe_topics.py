"""
v3_dedupe_topics.py — One-off cleanup of duplicate topics.

Background: before v2b introduced embedding-based topic matching, the old
consolidation path happily created multiple topic rows for the same subject
(e.g. 3 rows of "右膝外侧下坡时刺痛"). This script scans all topics,
computes pairwise cosine similarity on their embeddings, and merges groups
that cross DUPLICATE_THRESHOLD.

Merge policy for a cluster:
  - Primary = earliest created_at (preserves the ID that's already referenced
    elsewhere; usually the seed data).
  - For each duplicate:
      * UPDATE topic_episode_links SET topic_id = primary WHERE topic_id = dup  (ON CONFLICT IGNORE so pre-existing links are kept)
      * If primary has no working_conclusion and dup has one, copy it up.
      * If both have working_conclusion, keep primary's and stash dup's
        into a merge audit trail inside conflict_context.merged_from[].
      * DELETE dup from topics (the junction CASCADE would also nuke
        its links, but we already re-pointed them).

Idempotent: re-running after merge finds nothing to do.

Usage:
    uv run python migrations/v3_dedupe_topics.py                      # live
    uv run python migrations/v3_dedupe_topics.py --dry-run            # preview only
    uv run python migrations/v3_dedupe_topics.py --threshold 0.93     # looser
    uv run python migrations/v3_dedupe_topics.py --db /path/to.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from llm_provider import call_embedding, cosine_similarity  # noqa: E402


DUPLICATE_THRESHOLD = 0.95


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def signature_text(topic: sqlite3.Row) -> str:
    parts = [
        topic["name"] or "",
        topic["working_conclusion"] or "",
        topic["open_question"] or "",
    ]
    return " :: ".join(p.strip() for p in parts if p and p.strip())


def fetch_topics(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM topics ORDER BY created_at ASC"
    ).fetchall()


def find_duplicate_clusters(
    topics: list[sqlite3.Row], threshold: float
) -> list[list[int]]:
    """
    Union-find clustering by pairwise cosine similarity. Returns list of index
    groups (size >= 2); indexes reference the input `topics` list.
    """
    if len(topics) < 2:
        return []

    texts = [signature_text(t) for t in topics]
    print(f"  Embedding {len(texts)} topics (1 batched API call)...")
    vecs = call_embedding(texts)

    parent = list(range(len(topics)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra  # prefer lower index (earlier-created)

    for i in range(len(topics)):
        for j in range(i + 1, len(topics)):
            score = cosine_similarity(vecs[i], vecs[j])
            if score >= threshold:
                print(
                    f"    {vecs and topics[i]['topic_id']} ↔ {topics[j]['topic_id']}  "
                    f"score={score:.4f}  ({topics[i]['name']!r} vs {topics[j]['name']!r})"
                )
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(len(topics)):
        clusters.setdefault(find(i), []).append(i)

    return [idxs for idxs in clusters.values() if len(idxs) >= 2]


def merge_cluster(
    conn: sqlite3.Connection,
    primary: sqlite3.Row,
    duplicates: list[sqlite3.Row],
    dry_run: bool,
) -> None:
    primary_id = primary["topic_id"]
    dup_ids = [d["topic_id"] for d in duplicates]
    print(f"\n  Cluster primary = {primary_id} ({primary['name']!r}, {primary['status']})")
    for d in duplicates:
        print(f"    duplicate = {d['topic_id']} ({d['name']!r}, {d['status']})")

    if dry_run:
        for d in duplicates:
            link_count = conn.execute(
                "SELECT COUNT(*) FROM topic_episode_links WHERE topic_id = ?",
                (d["topic_id"],),
            ).fetchone()[0]
            print(f"    [DRY-RUN] would move {link_count} junction row(s) from {d['topic_id']} → {primary_id}")
        return

    now = now_iso()

    # Collect merge audit into primary's conflict_context.merged_from[]
    try:
        primary_ctx = json.loads(primary["conflict_context"] or "{}")
    except (TypeError, json.JSONDecodeError):
        primary_ctx = {}
    merged_from = primary_ctx.get("merged_from", [])
    for d in duplicates:
        merged_from.append(
            {
                "topic_id": d["topic_id"],
                "name": d["name"],
                "working_conclusion": d["working_conclusion"],
                "status_at_merge": d["status"],
                "merged_at": now,
            }
        )
    primary_ctx["merged_from"] = merged_from

    new_conclusion = primary["working_conclusion"]
    if not new_conclusion:
        for d in duplicates:
            if d["working_conclusion"]:
                new_conclusion = d["working_conclusion"]
                break

    for d in duplicates:
        # Re-point junction rows; IGNORE on composite PK conflict (already linked)
        conn.execute(
            """UPDATE OR IGNORE topic_episode_links
               SET topic_id = ? WHERE topic_id = ?""",
            (primary_id, d["topic_id"]),
        )
        # Any rows that collided get removed (they're now redundant)
        conn.execute(
            "DELETE FROM topic_episode_links WHERE topic_id = ?",
            (d["topic_id"],),
        )
        conn.execute("DELETE FROM topics WHERE topic_id = ?", (d["topic_id"],))

    conn.execute(
        """UPDATE topics
           SET working_conclusion = ?,
               conflict_context = ?,
               updated_at = ?
           WHERE topic_id = ?""",
        (
            new_conclusion,
            json.dumps(primary_ctx, ensure_ascii=False),
            now,
            primary_id,
        ),
    )
    conn.commit()
    print(f"  ✓ merged {len(duplicates)} dup(s) into {primary_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/cognition.db")
    parser.add_argument(
        "--threshold", type=float, default=DUPLICATE_THRESHOLD,
        help=f"cosine similarity above which topics are merged (default: {DUPLICATE_THRESHOLD})",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"❌ DB not found: {db_path}", file=sys.stderr)
        return 1

    print(f"Target DB: {db_path}")
    print(f"Threshold: {args.threshold}")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'LIVE'}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    topics = fetch_topics(conn)
    print(f"\n{len(topics)} topics in DB.\n")

    print("── Computing pairwise similarity ──")
    clusters = find_duplicate_clusters(topics, args.threshold)

    if not clusters:
        print("\n✓ No duplicate clusters found above threshold. Nothing to do.")
        return 0

    print(f"\nFound {len(clusters)} duplicate cluster(s).")

    for idxs in clusters:
        primary = topics[min(idxs)]  # earliest created_at (list was sorted ASC)
        duplicates = [topics[i] for i in idxs if i != min(idxs)]
        merge_cluster(conn, primary, duplicates, args.dry_run)

    print("\n── Final state ──")
    remaining = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    print(f"topics count: {len(topics)} → {remaining}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
