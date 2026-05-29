"""Endpoint smoke — TestClient hits every route, asserts no 500.

Goal of this file: the routing layer works end-to-end on every
documented endpoint. Side-effect-heavy dependencies (DataProcessor,
GoogleCalendar, MemoryOS, AgenticCoach, subprocess.run) are mocked in
conftest.py. We assert that each endpoint returns a "well-formed"
status code (i.e., NOT 500) for a reasonable input.

What this catches:
  • A typo in a route declaration that 404s
  • A handler that misuses a pydantic model and 500s on parse
  • A handler that imports something that doesn't exist
  • A handler that calls a dependency method we forgot to mock and
    blows up with AttributeError

What this does NOT catch (deliberately — out of scope for smoke):
  • Wrong business logic (handler returns 200 but the value is wrong)
  • Schema drift between handler and frontend types
  • Auth / OAuth edge cases

Per-endpoint behavioral assertions go in test_api_server_*.py files
(Phase 3 — one focused file per endpoint group).
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Smoke table.
#
# Format: (method, path, body, params, ok_codes)
#   - body / params: passed to TestClient.request
#   - ok_codes: set of acceptable status codes. The key invariant is
#     "no 500" — so we put 200 as the happy path and add 404 / 422 /
#     400 / 307 where the endpoint naturally returns those on the
#     reasonable input we're sending.
#
# For endpoints whose handlers call subprocess.run (garmin_sync,
# garmin_ticket_login), we patch subprocess.run at the test session
# level to return a benign "exit 0, empty stdout" result so the
# handlers don't actually invoke the real scripts.
# ---------------------------------------------------------------------------

ENDPOINTS: list[tuple] = [
    # --- Status / health ---
    # (`GET /` removed in this PR — webapp/index.html doesn't exist.)
    ("GET",    "/healthz",                                None, None,                  {200}),

    # --- Sync (subprocess-mocked) ---
    ("GET",    "/api/sync/garmin/status",                 None, None,                  {200}),
    ("POST",   "/api/sync/garmin",                        None, None,                  {200}),
    ("POST",   "/api/sync/garmin/refresh-token",          {"ticket": "ST-mock"}, None, {200}),
    ("POST",   "/api/sync/health-ledger",                 None, None,                  {200}),
    ("GET",    "/api/health/ledger",                      None, None,                  {200}),

    # --- Profile / athlete ---
    ("GET",    "/api/profile",                            None, None,                  {200}),
    ("GET",    "/api/athlete/profile",                    None, None,                  {200}),

    # --- Health ---
    ("GET",    "/api/health/readiness",                   None, None,                  {200}),
    ("GET",    "/api/health/today",                       None, None,                  {200}),
    ("GET",    "/api/health/timeline",                    None, None,                  {200}),
    ("GET",    "/api/health/sleep",                       None, None,                  {200}),
    ("GET",    "/api/health/snapshot",                    None, None,                  {200}),

    # --- Training ---
    ("GET",    "/api/training/load",                      None, None,                  {200}),
    ("GET",    "/api/training/blocks",                    None, None,                  {200}),
    ("POST",   "/api/training/blocks",                    {"name": "X", "start_date": "2026-05-01", "end_date": "2026-06-01"}, None, {200, 422}),
    ("PUT",    "/api/training/blocks/block_999",          {"name": "Y"}, None,           {200, 404}),
    ("DELETE", "/api/training/blocks/block_999",          None, None,                  {200, 404}),
    # 404 when there are no blocks (our mock returns []) — that's
    # the expected "no data yet" path, not an error.
    ("GET",    "/api/training/weeks",                     None, {"block_id": "block_001"}, {200, 404, 422}),
    ("GET",    "/api/training/cycle-stats",               None, {"block_id": "block_001", "week_start": "2026-05-01", "week_end": "2026-05-08"}, {200, 422}),
    ("GET",    "/api/training/monthly-stats",             None, None,                  {200}),

    # --- Google OAuth ---
    ("GET",    "/api/oauth/google/status",                None, None,                  {200}),
    # /oauth/google/start returns RedirectResponse — TestClient sees 307.
    ("GET",    "/oauth/google/start",                     None, None,                  {302, 307}),
    # /oauth/google/callback requires `state` query param (handler uses
    # Query(...) on it). 200 is the success-redirect path, 422 is the
    # missing-param path.
    ("GET",    "/oauth/google/callback",                  None, {"state": "mock_state", "code": "test"}, {200, 302, 307, 400, 422}),
    ("POST",   "/api/oauth/google/disconnect",            None, None,                  {200}),
    ("GET",    "/api/calendar/events",                    None, {"start": "2026-05-01T00:00:00", "end": "2026-05-08T00:00:00"}, {200, 422}),

    # --- Manual activities ---
    ("GET",    "/api/manual-activities",                  None, {"start": "2026-05-01", "end": "2026-05-08"}, {200}),
    ("GET",    "/api/manual-activities/9999",             None, None,                  {200, 404}),
    ("POST",   "/api/manual-activities",                  {"date": "2026-05-11", "type": "run", "duration_min": 30}, None, {200, 422}),
    ("PUT",    "/api/manual-activities/9999",             {"duration_min": 35}, None, {200, 404, 422}),
    ("DELETE", "/api/manual-activities/9999",             None, None,                  {200, 404}),

    # --- Runs ---
    ("GET",    "/api/runs",                               None, {"start": "2026-05-01", "end": "2026-05-08"}, {200}),
    ("GET",    "/api/runs/9999",                          None, None,                  {200, 404}),
    ("GET",    "/api/runs/9999/telemetry",                None, None,                  {200, 404}),
    ("GET",    "/api/runs/9999/weather",                  None, None,                  {200, 404}),
    ("GET",    "/api/runs/9999/route",                    None, None,                  {200, 404}),
    ("GET",    "/api/runs/9999/laps",                     None, None,                  {200, 404}),
    ("PUT",    "/api/runs/9999/laps",                     {"week_num": 1, "run_name": "smoke", "categories": [], "notes": ""}, None, {200, 404, 422}),

    # --- AI: chat / actions ---
    ("POST",   "/api/ai/run-analysis",                    {"activity_id": 9999}, None,   {200, 404, 422}),
    ("POST",   "/api/ai/health-analysis",                 None, None,                  {200, 422}),
    ("POST",   "/api/ai/chat",                            {"thread_id": "coach_20260511T000000Z", "message": "hi"}, None, {200}),
    ("POST",   "/api/ai/action/review_workout",           {"thread_id": "coach_20260511T000000Z", "activity_id": 9999}, None, {200, 400, 422}),
    ("POST",   "/api/ai/action/make_plan",                {"thread_id": "coach_20260511T000000Z"}, None, {200}),
    ("POST",   "/api/ai/action/review_health",            {"thread_id": "coach_20260511T000000Z"}, None, {200}),
    ("POST",   "/api/ai/action/follow_up_memory",         {"thread_id": "coach_20260511T000000Z"}, None, {200}),
    ("POST",   "/api/ai/action/summarize_and_archive",    {"thread_id": "coach_20260511T000000Z"}, None, {200}),
    ("POST",   "/api/ai/action/unknown_action",           {"thread_id": "coach_20260511T000000Z"}, None, {404}),

    # --- AI: sessions ---
    ("GET",    "/api/ai/sessions",                        None, None,                  {200}),
    ("POST",   "/api/ai/sessions",                        None, None,                  {200}),
    ("DELETE", "/api/ai/sessions/coach_20260511T000000Z", None, None,                  {200}),
    ("DELETE", "/api/ai/sessions/not-a-coach-thread",     None, None,                  {400}),
    ("GET",    "/api/ai/history/coach_20260511T000000Z",  None, None,                  {200}),

    # --- Memory ---
    ("GET",    "/api/memory/stats",                       None, None,                  {200}),
    ("GET",    "/api/memory/context",                     None, None,                  {200}),
    ("GET",    "/api/memory/concierge",                   None, None,                  {200}),
    ("GET",    "/api/memory/topics",                      None, None,                  {200}),
    ("GET",    "/api/memory/topics/tpc_mock",             None, None,                  {200, 404}),
    ("POST",   "/api/memory/topics",                      {"name": "test", "root_category": "Test"}, None, {200, 422}),
    ("PUT",    "/api/memory/topics/tpc_mock",             {"name": "renamed"}, None,     {200, 404, 422}),
    ("GET",    "/api/memory/episodes",                    None, None,                  {200}),
    ("POST",   "/api/memory/episodes",                    {"event_type": "Test", "what": "test event"}, None, {200, 422}),
    ("GET",    "/api/memory/episodes/search",             None, {"q": "test"},          {200, 422}),
    ("GET",    "/api/memory/pending",                     None, None,                  {200}),
    ("POST",   "/api/memory/pending/pnd_mock/resolve",    {"answer": "yes"}, None,       {200, 404, 422}),
    ("POST",   "/api/memory/consolidate",                 {"thread_id": "coach_20260511T000000Z"}, None, {200, 422}),
    # §3.4.5 — coach intake (profile A + cycle config B)
    ("GET",    "/api/memory/coach-profile",               None, None,                  {200}),
    ("GET",    "/api/memory/cycle-config",                None, None,                  {200}),
    ("POST",   "/api/memory/coach-fact",                  {"area": "Cycle.goal", "raw_text": "Berlin sub-3:30"}, None, {200, 400, 422}),
    ("GET",    "/api/memory/topics/tpc_mock/episodes",    None, None,                  {200}),
]


def _slug(s: str) -> str:
    """Friendly param id: turn /api/foo/{id}/bar → api_foo_bar."""
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


@pytest.fixture(autouse=True)
def mock_subprocess(monkeypatch):
    """Two endpoints (`POST /api/sync/garmin`, `POST /api/sync/garmin/
    refresh-token`) spawn subprocesses. Mock so tests don't fork real
    sync scripts."""
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)


@pytest.mark.parametrize(
    "method,path,body,params,ok_codes",
    ENDPOINTS,
    ids=[f"{m}_{_slug(p)}" for m, p, *_ in ENDPOINTS],
)
def test_no_500(client, method, path, body, params, ok_codes):
    """Hit every documented endpoint with a reasonable input. The
    invariant is "no 500" — handler exists, dependencies resolve,
    response shape JSON-serializes. Specific business-logic correctness
    is out of scope for smoke."""
    kwargs: dict = {}
    if body is not None:
        kwargs["json"] = body
    if params is not None:
        kwargs["params"] = params

    # follow_redirects=False so 302/307 surface as-is (some endpoints
    # like /oauth/google/start are expected redirects).
    resp = client.request(method, path, follow_redirects=False, **kwargs)
    body_preview = resp.text[:300].replace("\n", " ")
    assert resp.status_code in ok_codes, (
        f"{method} {path} → got {resp.status_code} "
        f"(expected one of {sorted(ok_codes)})\nbody: {body_preview}"
    )
    # Belt-and-suspenders: 5xx is never OK in a smoke run, even if it
    # happened to slip into ok_codes by accident.
    assert resp.status_code < 500, (
        f"{method} {path} returned 5xx — endpoint is broken.\n"
        f"body: {body_preview}"
    )
