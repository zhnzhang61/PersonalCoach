"""PersonalCoach backend package.

Every server-side Python module lives here:

  • api_server.py             FastAPI HTTP entry
  • agentic_coach.py          LangGraph + MCP agent
  • cognitive_memory_engine.py Topic / episode CME
  • data_processor.py         Garmin / health data layer
  • llm_provider.py           LLM routing (Gemini / Groq)
  • personal_coach_mcp.py     MCP stdio server
  • garmin_sync.py            Garmin Connect pulls
  • garmin_ticket_login.py    Garmin SSO ticket exchange
  • google_calendar.py        Google Calendar OAuth + reads

Import absolute as `from backend.X import Y` so the package
boundary is obvious from any call site. The CLI / script entries
under scripts/ and the test suite under tests/ all do this.

Entry points:
  • `uv run uvicorn backend.api_server:app --port 8765`   FastAPI
  • `uv run python -m backend.personal_coach_mcp`         MCP stdio
  • `uv run python -m backend.garmin_sync`                Garmin pull
  • `uv run python -m backend.garmin_ticket_login`        SSO refresh
"""
