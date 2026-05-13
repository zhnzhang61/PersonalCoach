"""Versioned CME schema migrations.

Each `vN_<topic>.py` is a one-off, idempotent script. Run from the
repo root:

  uv run python -m scripts.migrations.v2_cme_schema
  uv run python -m scripts.migrations.v3_dedupe_topics
  uv run python -m scripts.migrations.v4_link_episodes [--db path] [--dry-run]

History (newest first):
  v4_link_episodes  — interactive backfill of orphan-episode → topic links
  v3_dedupe_topics  — cosine-merge duplicate topic rows (embedding-based)
  v2_cme_schema     — add `open_question` / `conflict_context` to topics,
                      expand status CHECK to allow 'Conflicting'
"""
