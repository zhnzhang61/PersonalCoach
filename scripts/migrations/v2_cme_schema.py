"""
v2_cme_schema.py — One-off migration for CME schema v2.

What this does (all steps idempotent; safe to re-run):

  1. Backup topics / episodes / pending_clarifications to JSON
  2. Swap `topics` to a new schema that:
       - adds `open_question TEXT` (nullable)
       - adds `conflict_context TEXT` (nullable, JSON)
       - expands status CHECK to allow 'Conflicting'
  3. Add `event_timestamp`, `event_date_text`, `timestamp_source` to `episodes`
     (all nullable; historical rows stay NULL with `timestamp_source='unknown'`)
  4. Create `topic_episode_links` junction table (replaces the two JSON
     array columns; arrays kept for now, dropped later in Phase 4)
  5. Backfill junction from existing `episodes.related_topic_ids` JSON arrays
  6. Merge the 3 rain-related pending_clarifications into a single new topic
     with status='Conflicting'; mark those pendings resolved with a pointer
     to the new topic_id

Usage:
    uv run python migrations/v2_cme_schema.py                       # run against data/cognition.db
    uv run python migrations/v2_cme_schema.py --db /path/to.db      # custom DB
    uv run python migrations/v2_cme_schema.py --dry-run             # show what would happen
    uv run python migrations/v2_cme_schema.py --backup-dir path/    # custom backup location

The script prints a summary at the end. Nothing is deleted — `pending_clarifications`
rows are only marked resolved, the table itself is dropped in Phase 4 later.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def get_status_check(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row[0] if row else ""


# --------------------------------------------------------------------------
# Phase 1 — Backup
# --------------------------------------------------------------------------

def backup_tables(conn: sqlite3.Connection, backup_dir: Path, dry_run: bool) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = backup_dir / f"cme_backup_{stamp}"
    if dry_run:
        print(f"[DRY-RUN] would write backup to {out_dir}/")
        return out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    conn.row_factory = sqlite3.Row
    for table in ("topics", "episodes", "pending_clarifications"):
        if not table_exists(conn, table):
            continue
        rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}").fetchall()]
        (out_dir / f"{table}.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2)
        )
        print(f"  ✓ backed up {table}: {len(rows)} rows → {out_dir / (table + '.json')}")
    return out_dir


# --------------------------------------------------------------------------
# Phase 2 — Topics table swap (expand CHECK + add columns)
# --------------------------------------------------------------------------

def upgrade_topics_table(conn: sqlite3.Connection, dry_run: bool) -> bool:
    """Returns True if a change was made, False if already up-to-date."""
    if column_exists(conn, "topics", "open_question"):
        print("  ✓ topics already has new columns — skipping swap")
        return False

    if dry_run:
        print("  [DRY-RUN] would swap topics table: add open_question, conflict_context, expand status CHECK to allow 'Conflicting'")
        return True

    conn.executescript(
        """
        BEGIN;

        CREATE TABLE topics_new (
            topic_id           TEXT PRIMARY KEY,
            root_category      TEXT NOT NULL,
            name               TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'Open'
                               CHECK(status IN ('Open', 'Testing', 'Resolved', 'Conflicting')),
            working_conclusion TEXT,
            open_question      TEXT,                 -- nullable; set when status='Conflicting'
            conflict_context   TEXT,                 -- nullable JSON; {old_belief, new_evidence, ...}
            related_episodes   TEXT DEFAULT '[]',    -- legacy JSON array, retained for rollback; canonical source is topic_episode_links
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL
        );

        INSERT INTO topics_new
            (topic_id, root_category, name, status, working_conclusion,
             related_episodes, created_at, updated_at)
        SELECT topic_id, root_category, name, status, working_conclusion,
               related_episodes, created_at, updated_at
        FROM topics;

        DROP TABLE topics;
        ALTER TABLE topics_new RENAME TO topics;

        CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);

        COMMIT;
        """
    )
    print("  ✓ topics swapped: +open_question +conflict_context, CHECK now allows 'Conflicting'")
    return True


# --------------------------------------------------------------------------
# Phase 3 — Episodes columns for event_timestamp
# --------------------------------------------------------------------------

def upgrade_episodes_table(conn: sqlite3.Connection, dry_run: bool) -> bool:
    added = []
    specs = [
        ("event_timestamp", "TEXT"),
        ("event_date_text", "TEXT"),
        ("timestamp_source", "TEXT"),
    ]
    for col, typ in specs:
        if column_exists(conn, "episodes", col):
            continue
        if dry_run:
            added.append(col)
            continue
        conn.execute(f"ALTER TABLE episodes ADD COLUMN {col} {typ}")
        added.append(col)

    if added:
        if dry_run:
            print(f"  [DRY-RUN] would ADD COLUMN on episodes: {', '.join(added)}")
        else:
            # Historical rows: timestamp_source defaults to 'unknown' (legacy data)
            conn.execute(
                "UPDATE episodes SET timestamp_source = 'unknown' WHERE timestamp_source IS NULL"
            )
            conn.commit()
            print(f"  ✓ episodes: added {', '.join(added)}; historical rows flagged timestamp_source='unknown'")
    else:
        print("  ✓ episodes columns already present — skipping")
    return bool(added)


# --------------------------------------------------------------------------
# Phase 4 — topic_episode_links junction
# --------------------------------------------------------------------------

def create_junction_table(conn: sqlite3.Connection, dry_run: bool) -> bool:
    if table_exists(conn, "topic_episode_links"):
        print("  ✓ topic_episode_links already exists — skipping")
        return False
    if dry_run:
        print("  [DRY-RUN] would CREATE TABLE topic_episode_links")
        return True
    conn.executescript(
        """
        CREATE TABLE topic_episode_links (
            topic_id   TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (topic_id, episode_id),
            FOREIGN KEY (topic_id)   REFERENCES topics(topic_id)   ON DELETE CASCADE,
            FOREIGN KEY (episode_id) REFERENCES episodes(episode_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_tel_episode ON topic_episode_links(episode_id);
        CREATE INDEX idx_tel_topic   ON topic_episode_links(topic_id);
        """
    )
    conn.commit()
    print("  ✓ topic_episode_links created (+ indexes)")
    return True


def backfill_junction_from_json_arrays(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Copy existing episodes.related_topic_ids JSON arrays into the junction table."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT episode_id, related_topic_ids FROM episodes").fetchall()
    to_insert: list[tuple[str, str]] = []
    for r in rows:
        try:
            ids = json.loads(r["related_topic_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        for tid in ids:
            to_insert.append((tid, r["episode_id"]))

    if not to_insert:
        print("  ✓ no episode→topic JSON links to backfill")
        return 0

    if dry_run:
        print(f"  [DRY-RUN] would backfill {len(to_insert)} junction rows from episodes.related_topic_ids")
        for tid, eid in to_insert:
            print(f"      {eid} ↔ {tid}")
        return len(to_insert)

    now = now_iso()
    inserted = 0
    for tid, eid in to_insert:
        # INSERT OR IGNORE so re-runs don't double-insert (PK is composite)
        cur = conn.execute(
            "INSERT OR IGNORE INTO topic_episode_links (topic_id, episode_id, created_at) VALUES (?, ?, ?)",
            (tid, eid, now),
        )
        inserted += cur.rowcount
    conn.commit()
    print(f"  ✓ backfilled {inserted} junction rows ({len(to_insert) - inserted} already present)")
    return inserted


# --------------------------------------------------------------------------
# Phase 5 — Merge rain-themed pending_clarifications into a single Conflicting topic
# --------------------------------------------------------------------------

RAIN_TOPIC_NAME = "下雨天跑步偏好"
RAIN_TOPIC_CATEGORY = "Preference/Weather"


def find_rain_pendings(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Identify unresolved pendings whose question mentions rainy-day running."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT * FROM pending_clarifications
           WHERE is_resolved = 0
             AND (question_for_user LIKE '%下雨%' OR question_for_user LIKE '%雨天%')"""
    ).fetchall()
    return rows


def merge_rain_pendings(conn: sqlite3.Connection, dry_run: bool) -> str | None:
    if not table_exists(conn, "pending_clarifications"):
        print("  ✓ pending_clarifications table already gone — skipping merge")
        return None

    rows = find_rain_pendings(conn)
    if not rows:
        # Check if we've already merged (rain topic already exists)
        existing = conn.execute(
            "SELECT topic_id FROM topics WHERE name = ? AND status = 'Conflicting'",
            (RAIN_TOPIC_NAME,),
        ).fetchone()
        if existing:
            print(f"  ✓ rain conflict already merged into topic {existing['topic_id']}")
            return existing["topic_id"]
        print("  ✓ no rain pendings found; nothing to merge")
        return None

    # Pick the richest resolution_callback (prefer one with target_node)
    best = None
    best_score = -1
    for r in rows:
        try:
            cb = json.loads(r["resolution_callback"])
        except json.JSONDecodeError:
            cb = {}
        score = 0
        if cb.get("target_node"):
            score += 2
        cc = cb.get("conflict_context", {})
        if cc.get("old_belief") and "(" in (cc.get("old_belief") or ""):
            score += 1  # richer wording with date tag
        if score > best_score:
            best = r
            best_score = score
    assert best is not None
    best_cb = json.loads(best["resolution_callback"])

    topic_id = f"tpc_{uuid.uuid4().hex[:8]}"
    open_question = best["question_for_user"]
    conflict_context = json.dumps(
        {
            "target_node": best_cb.get("target_node"),
            "old_belief": best_cb.get("conflict_context", {}).get("old_belief"),
            "new_evidence": best_cb.get("conflict_context", {}).get("new_evidence"),
            "merged_from_pending_ids": [r["pending_id"] for r in rows],
        },
        ensure_ascii=False,
    )
    now = now_iso()

    if dry_run:
        print(f"  [DRY-RUN] would create topic {topic_id} (name='{RAIN_TOPIC_NAME}', status='Conflicting')")
        print(f"            merging {len(rows)} pendings: {[r['pending_id'] for r in rows]}")
        print(f"            conflict_context = {conflict_context}")
        return topic_id

    conn.execute(
        """INSERT INTO topics
           (topic_id, root_category, name, status, working_conclusion,
            open_question, conflict_context, related_episodes, created_at, updated_at)
           VALUES (?, ?, ?, 'Conflicting', NULL, ?, ?, '[]', ?, ?)""",
        (topic_id, RAIN_TOPIC_CATEGORY, RAIN_TOPIC_NAME, open_question, conflict_context, now, now),
    )

    for r in rows:
        conn.execute(
            """UPDATE pending_clarifications
               SET is_resolved = 1,
                   resolution_answer = ?,
                   resolved_at = ?
               WHERE pending_id = ?""",
            (f"migrated_to_topic:{topic_id}", now, r["pending_id"]),
        )
    conn.commit()
    print(f"  ✓ created topic {topic_id} from {len(rows)} rain pendings ({[r['pending_id'] for r in rows]})")
    return topic_id


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def report_state(conn: sqlite3.Connection, label: str) -> None:
    print(f"\n[{label}]")
    for tbl in ("topics", "episodes", "pending_clarifications", "topic_episode_links"):
        if table_exists(conn, tbl):
            n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"  {tbl}: {n} rows")
        else:
            print(f"  {tbl}: (missing)")
    unresolved = 0
    if table_exists(conn, "pending_clarifications"):
        unresolved = conn.execute(
            "SELECT COUNT(*) FROM pending_clarifications WHERE is_resolved = 0"
        ).fetchone()[0]
    print(f"  pending_clarifications (unresolved): {unresolved}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/cognition.db", help="path to cognition sqlite DB")
    parser.add_argument("--backup-dir", default="data/backups", help="where to write backup JSON")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"❌ DB not found: {db_path}", file=sys.stderr)
        return 1

    print(f"Target DB: {db_path}")
    print(f"Mode: {'DRY-RUN (no writes)' if args.dry_run else 'LIVE'}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    report_state(conn, "BEFORE")

    print("\n── Phase 1: Backup ──")
    backup_tables(conn, Path(args.backup_dir).resolve(), args.dry_run)

    print("\n── Phase 2: Upgrade topics table ──")
    upgrade_topics_table(conn, args.dry_run)

    print("\n── Phase 3: Add episode timestamp columns ──")
    upgrade_episodes_table(conn, args.dry_run)

    print("\n── Phase 4: Create junction table + backfill ──")
    create_junction_table(conn, args.dry_run)
    backfill_junction_from_json_arrays(conn, args.dry_run)

    print("\n── Phase 5: Merge rain pendings → Conflicting topic ──")
    merge_rain_pendings(conn, args.dry_run)

    report_state(conn, "AFTER")

    conn.close()
    print("\n✓ Migration complete." if not args.dry_run else "\n(dry run — no writes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
