# Prompt Changelog

Every edit to what the LLM actually sees as system text — that's
both `_SYSTEM_PROMPT` AND the `_HEADER_TEMPLATE` wrapper in
`backend/agentic_coach.py` — bumps `PROMPT_VERSION` and adds a row
here. The version label + a short sha1 hash of the prompt content
land in every structured trace row under
`data/traces/YYYY-MM-DD.jsonl`, so when the agent does something
unexpected we can ask "which prompt version produced this turn?"
without guessing. The hash is computed against
`f"{_HEADER_TEMPLATE.format(sentinel_date)}\n\n{_SYSTEM_PROMPT}"`,
i.e. the daily-changing date doesn't churn the hash but any wrapper
or persona edit does.

| Version | Date       | What changed | PR / commit |
|---------|------------|--------------|-------------|
| v8      | 2026-05-27 | Added a per-turn date-header wrapper in front of `_SYSTEM_PROMPT` (`_HEADER_TEMPLATE` rendered every call by `_build_prompt`). Pins "Today is YYYY-MM-DD (Weekday)" + a relative-time directive covering English and Chinese (`今天 / 明天 / 后天 / 这周`) + "Never schedule planned workouts in the past." Today is computed via `datetime.now(_user_tz()).date()` where `_user_tz()` honors `PERSONAL_COACH_TZ` (IANA name, e.g. `Asia/Shanghai`) and falls back to process-local tz. Hash now covers `_HEADER_TEMPLATE` + `_SYSTEM_PROMPT` (with a sentinel date so daily churn doesn't move it). Fixes a real bug where the agent wrote a "今天 easy run" to 2026-05-14 because it had no date anchor. | [#84](https://github.com/zhnzhang61/PersonalCoach/pull/84) |
| v7      | 2026-05-13 | Codex P2 clarification: explicit listing of which Garmin per-run interpretive labels are filtered at MCP boundary (`aerobicTrainingEffect`, `anaerobicTrainingEffect`, `activityTrainingLoad`, `trainingEffectLabel`, `aerobicTrainingEffectMessage`) AND which long-term baselines are NOT filtered (`vo2max_running`, `lactate_threshold_hr`, `lactate_threshold_pace`) and why. Replaces the v6 vague "you won't see them" wording that contradicted what `get_athlete_profile` actually returns. | [#68](https://github.com/zhnzhang61/PersonalCoach/pull/68) (143f081) |
| v6      | 2026-05-13 | §2 main: removed the "SILENTLY IGNORE..." block + 7-line bullet list of forbidden Garmin field names from the prompt — those fields are now filtered at the MCP data layer (`_trim_run_summary`, `get_run_detail`, `get_athlete_profile`) so prompt rules are no longer load-bearing. Renamed `hr_zones` → `medium_term_hr_effort_map` in the Vocabulary Trap + Medium-term mapping sections to match the new MCP-projected key. Dropped trailing "not in Garmin labels" from `review_workout` Step 6. | [#68](https://github.com/zhnzhang61/PersonalCoach/pull/68) (7f0662a) |
| ≤ v5    | pre-2026-05-13 | History before structured tracing landed. Six earlier iterations existed (Phase 2 session-based Coach, Gemini 3.1 Flash Lite swap, archive divider, etc.) but exact prompt diffs weren't tracked. If a trace row carries `prompt_version` ≤ "v5" the content hash is the source of truth. | n/a |

## How to add a row

When editing `_SYSTEM_PROMPT` **or `_HEADER_TEMPLATE`** in
`backend/agentic_coach.py` (both contribute to what the LLM actually
sees):

1. Bump `PROMPT_VERSION` in `backend/agentic_coach.py` (e.g. `"v7"` → `"v8"`).
2. Add a row to the top of the table here with date + ONE paragraph
   describing the change + the PR or commit ref.
3. Commit prompt edit + version bump + changelog row in the SAME
   commit. Pre-commit reviewers (codex / human) should reject a
   prompt edit that doesn't bump the version — otherwise traces
   stamp v7 onto a prompt that's actually different.

## Reading traces

```bash
# All turns on v7 today
jq -c 'select(.prompt_version == "v7")' data/traces/$(date +%F).jsonl

# Detect prompt drift — content hash doesn't match the version label
jq -c 'select(.prompt_version == "v7" and .prompt_hash != "REPLACE_WITH_CURRENT_HASH")' \
  data/traces/$(date +%F).jsonl
```

The current hash is logged at AgenticCoach init time; grep server
startup output if you need the canonical value for the running
prompt. (A `/api/debug/prompt_info` endpoint could expose this — not
done yet, IMPROVEMENTS §5 candidate.)
