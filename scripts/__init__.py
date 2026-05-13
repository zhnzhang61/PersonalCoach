"""CLI tools + one-off scripts.

Layout:

  scripts/
    ├── manual_mcp_smoke.py        dev tool: spawn personal_coach_mcp
    │                              over stdio + dump each tool's reply
    ├── migrate_garmin_token.py    one-off CLI: migrate a pirate-garmin
    │                              native-oauth2.json into the garth
    │                              tokens that backend/garmin_sync uses
    └── migrations/                versioned DB migrations for CME

Usage:
  • `uv run python -m scripts.migrate_garmin_token`
  • `uv run python -m scripts.migrations.v4_link_episodes`
  • `uv run python scripts/manual_mcp_smoke.py`  (still works as a
    bare script invocation — it's just stdio against MCP, no package-
    relative imports)
"""
